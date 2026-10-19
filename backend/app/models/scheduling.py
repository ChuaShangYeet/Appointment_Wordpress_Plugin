"""Scheduling core: policies, resources, shifts, days off, bookings and allocations."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Interval,
    LargeBinary,
    SmallInteger,
    String,
    Text,
    Time,
    Boolean,
    Date,
    Numeric,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSTZRANGE, UUID, ExcludeConstraint, Range
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

RESOURCE_TYPES = ("staff", "table", "bay")
BOOKING_STATUSES = (
    "pending", "confirmed", "in-progress", "waiting-for-parts", "completed", "cancelled", "rejected", "no-show",
)
ALLOCATION_STATES = ("held", "active", "released")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class BookingPolicy(UUIDMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "booking_policies"
    __table_args__ = (CheckConstraint("default_status IN ('pending','confirmed')", name="default_status"),)

    name: Mapped[str] = mapped_column(String(100))
    default_status: Mapped[str] = mapped_column(String(20), default="confirmed")
    min_notice_booking: Mapped[dt.timedelta] = mapped_column(Interval)
    min_notice_cancel: Mapped[dt.timedelta] = mapped_column(Interval)
    booking_window: Mapped[dt.timedelta] = mapped_column(Interval)
    #: How long a ``pending`` booking holds its slot before auto-rejection.
    pending_timeout: Mapped[dt.timedelta] = mapped_column(Interval)


class OperatingHours(UUIDMixin, TenantMixin, Base):
    """Tenant-wide opening hours per weekday (0=Sun..6=Sat). No rows = unrestricted (shifts decide).

    ``close_time`` before ``open_time`` means the hours run past midnight; they belong to the weekday they open on."""

    __tablename__ = "operating_hours"
    __table_args__ = (CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday"),)

    weekday: Mapped[int] = mapped_column(SmallInteger)
    open_time: Mapped[dt.time] = mapped_column(Time)
    close_time: Mapped[dt.time] = mapped_column(Time)


class TimeSlotSetting(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """Restaurant slot generation rules (one row per tenant).

    ``flexible``: a start every ``slot_interval`` from opening until ``closing - slot_duration``.
    ``fixed``: starts from ``first_slot_time`` every ``slot_interval`` up to ``last_slot_time``.
    A table is occupied for ``slot_duration + buffer``.
    """

    __tablename__ = "time_slot_settings"
    __table_args__ = (
        CheckConstraint("booking_mode IN ('flexible','fixed')", name="booking_mode"),
        CheckConstraint("slot_duration_minutes > 0 AND slot_interval_minutes > 0 AND buffer_minutes >= 0", name="positive"),
        UniqueConstraint("tenant_id", name="uq_time_slot_settings_tenant"),
    )

    booking_mode: Mapped[str] = mapped_column(String(10), default="flexible")
    slot_duration_minutes: Mapped[int] = mapped_column(Integer)
    slot_interval_minutes: Mapped[int] = mapped_column(Integer)
    first_slot_time: Mapped[dt.time] = mapped_column(Time)
    last_slot_time: Mapped[dt.time] = mapped_column(Time)
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: When true, customers may pick a specific table (``resource_id``); otherwise the system assigns.
    allow_table_selection: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    #: Optional policy override for reservations (else the tenant default).
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_policies.id", ondelete="SET NULL")
    )


class TableOpenRule(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """How many tables of a class are open for online reservation on a weekday or a specific date.

    A date-specific rule overrides a weekday rule. No matching rule = every table is open.
    """

    __tablename__ = "table_open_rules"
    __table_args__ = (
        CheckConstraint("(weekday IS NULL) <> (specific_date IS NULL)", name="weekday_xor_date"),
        CheckConstraint("weekday IS NULL OR weekday BETWEEN 0 AND 6", name="weekday"),
        CheckConstraint("table_type IN ('normal','vip')", name="table_type"),
        CheckConstraint("open_tables >= 0", name="open_tables"),
    )

    weekday: Mapped[int | None] = mapped_column(SmallInteger)
    specific_date: Mapped[dt.date | None] = mapped_column(Date)
    table_type: Mapped[str] = mapped_column(String(10), default="normal", server_default="normal")
    open_tables: Mapped[int] = mapped_column(Integer)


class Resource(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """Polymorphic bookable thing: staff member, restaurant table or workshop bay."""

    __tablename__ = "resources"
    __table_args__ = (
        CheckConstraint(_in("type", RESOURCE_TYPES), name="type"),
        CheckConstraint("status IN ('active','inactive')", name="status"),
    )

    type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    capacity: Mapped[int | None] = mapped_column(Integer)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    #: Staff only, set by a manager, never shown publicly: where the backend reaches this person when their schedule collides.
    contact_email: Mapped[str | None] = mapped_column(String(254))
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    #: Staff only: the person's own video room (Google Meet, Zoom, Teams...), given to their online appointments when they are confirmed (migration 0032).
    meeting_link: Mapped[str | None] = mapped_column(Text)

    shifts: Mapped[list[ResourceShift]] = relationship(cascade="all, delete-orphan", lazy="raise")


def _secs_after_start(col: str) -> str:
    return f"((EXTRACT(EPOCH FROM ({col} - start_time))::int % 86400) + 86400) % 86400"


#: The break lies inside the shift, measured forward from its start so overnight shifts (and breaks after midnight) work.
BREAK_WITHIN_SHIFT = (
    "(break_start IS NULL AND break_end IS NULL) OR (break_start IS NOT NULL AND break_end IS NOT NULL AND "
    f"{_secs_after_start('break_start')} < {_secs_after_start('break_end')} AND "
    f"{_secs_after_start('break_end')} <= {_secs_after_start('end_time')})"
)


class ResourceShift(UUIDMixin, TenantMixin, Base):
    __tablename__ = "resource_shifts"
    __table_args__ = (
        CheckConstraint("weekday BETWEEN 0 AND 6", name="weekday"),
        CheckConstraint("end_time <> start_time", name="span"),  # an end before the start is an overnight shift
        CheckConstraint(BREAK_WITHIN_SHIFT, name="break_within_shift"),
    )

    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), index=True
    )
    weekday: Mapped[int] = mapped_column(SmallInteger)
    start_time: Mapped[dt.time] = mapped_column(Time)
    end_time: Mapped[dt.time] = mapped_column(Time)
    break_start: Mapped[dt.time | None] = mapped_column(Time)
    break_end: Mapped[dt.time | None] = mapped_column(Time)


class DayOff(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """Company holiday (``resource_id`` NULL) or personal leave."""

    __tablename__ = "days_off"
    __table_args__ = (CheckConstraint("end_date >= start_date", name="span"),)

    resource_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(150))
    start_date: Mapped[dt.date] = mapped_column(Date)
    end_date: Mapped[dt.date] = mapped_column(Date)
    repeat_yearly: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class Booking(UUIDMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint(_in("status", BOOKING_STATUSES), name="status"),
        CheckConstraint("end_at > start_at", name="span"),
        CheckConstraint("table_type IS NULL OR table_type IN ('normal','vip')", name="table_type"),
        #: what the periodic flag sweep still has to look at (see ``app.services.sweep``)
        Index("ix_bookings_unchecked", "created_at", postgresql_where=text("flags_checked_at IS NULL")),
        #: one booking per ``Idempotency-Key`` and business: sending the same request twice cannot make two
        Index("uq_bookings_idempotency_key", "tenant_id", "idempotency_key", unique=True, postgresql_where=text("idempotency_key IS NOT NULL")),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), index=True)
    #: The email given with *this* booking, lower-cased: its messages go there, and an emailed sign-in link shows the bookings made with it. It is not the customer's
    #: email: anyone can type any address into a booking (see ``app.services.booking._upsert_customer``).
    contact_email: Mapped[str | None] = mapped_column(String(254))
    #: The ``Idempotency-Key`` header the booking was made with and a hash of the request, so that the same request sent again returns this booking instead of making another.
    idempotency_key: Mapped[str | None] = mapped_column(String(100))
    idempotency_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    start_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    #: Immutable copy of the effective policy at booking time.
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    party_size: Mapped[int | None] = mapped_column(Integer)
    vehicle_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("vehicles.id"))
    address_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customer_addresses.id"))
    meeting_link: Mapped[str | None] = mapped_column(Text)
    special_request: Mapped[str | None] = mapped_column(Text)
    #: AES-256-GCM ciphertext (PDPA). Never selected into list responses.
    health_notes_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    hold_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(20), default="headless", server_default="headless")
    #: Restaurant only: 'normal' | 'vip', whether tables may be combined, and the total minimum
    #: spend the customer agreed to (sum over the assigned VIP tables).
    table_type: Mapped[str | None] = mapped_column(String(10))
    allow_separate_seating: Mapped[bool | None] = mapped_column(Boolean)
    minimum_spend_agreed: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    #: Automotive: when the customer accepted the lead-time disclaimer for on-order parts.
    lead_time_acknowledged_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Field service: a follow-up standard service links back to its inspection.
    parent_booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="SET NULL"), index=True)
    #: Field service: minutes the job really took (overrun handling).
    actual_duration_minutes: Mapped[int | None] = mapped_column(Integer)
    #: Set when an overrun collides with the next appointment; cleared by an admin.
    schedule_conflict: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    #: Review flags raised when a settings, shift or service change no longer fits this booking
    #: (blueprint §7.3/§8.3/§13.5: flag only, never auto-cancel). See ``app.services.flagging``.
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    #: When every review-flag rule was last checked against this booking by the periodic sweep; ``NULL`` means it
    #: still has to be (new bookings, and resumed jobs whose allocations changed).
    flags_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    items: Mapped[list[BookingItem]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    allocations: Mapped[list[BookingAllocation]] = relationship(
        back_populates="booking", cascade="all, delete-orphan", lazy="selectin"
    )


#: the emailed sign-in link finds bookings by their contact address, case-insensitively
Index("ix_bookings_tenant_lower_contact_email", Booking.tenant_id, func.lower(Booking.contact_email))


class BookingItem(UUIDMixin, TenantMixin, Base):
    """A service on a booking, with duration frozen at booking time."""

    __tablename__ = "booking_items"

    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    service_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id"))
    duration_minutes: Mapped[int] = mapped_column(Integer)
    buffer_minutes: Mapped[int] = mapped_column(Integer)
    #: Position on the booking (the order the services were chosen in; segments of a stacked job run in this order).
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    #: True for add-ons added to the ticket after the booking was made (on-the-spot upsell).
    is_upsell: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")


class BookingProduct(UUIDMixin, TenantMixin, Base):
    """A retail product on an appointment's ticket; stock is deducted when the appointment completes."""

    __tablename__ = "booking_products"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="quantity"),
        UniqueConstraint("booking_id", "product_id", name="uq_booking_products_booking_product"),
    )

    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("products.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    #: Price when it was added to the ticket (the catalog price may change later).
    unit_price: Mapped[Decimal] = mapped_column(Numeric(10, 2))


class BookingAllocation(UUIDMixin, TenantMixin, Base):
    """Reservation of one resource over ``period``.

    The exclusion constraint is the ultimate anti-double-booking guarantee:
    two ``held``/``active`` rows for the same resource can never overlap.
    Requires the ``btree_gist`` extension (created in the first migration).
    """

    __tablename__ = "booking_allocations"
    __table_args__ = (
        CheckConstraint(_in("state", ALLOCATION_STATES), name="state"),
        ExcludeConstraint(
            ("resource_id", "="),
            ("period", "&&"),
            using="gist",
            where=text("state IN ('held', 'active')"),
            name="booking_allocations_no_overlap",
        ),
    )

    booking_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), index=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("resources.id"), index=True)
    #: The service this reservation serves, for bookings made of segments (workshop job orders). ``NULL``: the resource serves the whole
    #: booking (a spa treatment, a salon cart), or the row predates this column on a multi-service job order.
    service_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("services.id", ondelete="SET NULL"))
    period: Mapped[Range[dt.datetime]] = mapped_column(TSTZRANGE)
    state: Mapped[str] = mapped_column(String(20))

    booking: Mapped[Booking] = relationship(back_populates="allocations")
