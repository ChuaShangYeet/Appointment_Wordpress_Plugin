"""Google Calendar sync: the calendars a business connected, and the calendar event kept for each booking (blueprint §12.3, §15)."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

CONNECTION_STATUSES = ("active", "needs_reconnect")
EVENT_STATES = ("pending", "synced", "failed")


class CalendarConnection(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """One Google calendar bookings are copied to: the business's own (``resource_id`` empty - every booking) or one staff member's (their bookings)."""

    __tablename__ = "calendar_connections"
    __table_args__ = (
        CheckConstraint("status IN ('active','needs_reconnect')", name="status"),
        #: one calendar for the business as a whole, and one per staff member
        Index("uq_calendar_connections_business", "tenant_id", unique=True, postgresql_where=text("resource_id IS NULL")),
        Index("uq_calendar_connections_staff", "tenant_id", "resource_id", unique=True, postgresql_where=text("resource_id IS NOT NULL")),
    )

    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"))
    #: ``primary`` (the account's main calendar) or the id of another calendar the account can write to.
    calendar_id: Mapped[str] = mapped_column(String(254), default="primary", server_default="primary")
    #: The calendar's own name, read from Google when it was connected (to tell connections apart in the admin).
    account_label: Mapped[str | None] = mapped_column(String(200))
    #: AES-256-GCM ciphertexts (tied to the business): the long-lived refresh token, and the short-lived access token cached from it.
    refresh_token_enc: Mapped[bytes] = mapped_column(LargeBinary)
    access_token_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    access_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
    #: Off: an event carries the services and the booking reference only. On: also the customer's name and phone, the party size, their request and the address.
    share_customer_details: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(300))


class CalendarEvent(UUIDMixin, TenantMixin, Base):
    """The calendar event one booking has (or is to have) in one connected calendar. ``pending`` rows are worked off by the sweep, which looks at the booking *as it
    is then* and makes Google match it - so retries, reordering and repeated nudges all end in the same place."""

    __tablename__ = "calendar_events"
    __table_args__ = (
        CheckConstraint("state IN ('pending','synced','failed')", name="state"),
        UniqueConstraint("connection_id", "booking_id", name="uq_calendar_events_connection_booking"),
        Index("ix_calendar_events_due", "next_attempt_at", postgresql_where=text("state = 'pending'")),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("calendar_connections.id", ondelete="CASCADE"))
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    #: The id the event has (or will have) in Google, chosen by us and stored *before* the first request, so a retry after a crash finds the same event.
    external_event_id: Mapped[str | None] = mapped_column(String(100))
    #: Hash of what was last sent, so an unchanged booking costs no request.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))
    synced_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(300))
