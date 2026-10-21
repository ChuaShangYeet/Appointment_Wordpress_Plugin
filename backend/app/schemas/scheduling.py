"""Admin scheduling schemas: policies, resources, shifts, days off, bookings."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, model_validator

BookingStatus = Literal[
    "pending", "confirmed", "in-progress", "waiting-for-parts", "completed", "cancelled", "rejected", "no-show"
]


class PolicyCreate(BaseModel):
    """Durations accept ISO-8601 (``PT2H``, ``P60D``) or seconds."""

    name: str = Field(min_length=1, max_length=100)
    default_status: Literal["pending", "confirmed"] = "confirmed"
    min_notice_booking: dt.timedelta = dt.timedelta(hours=2)
    min_notice_cancel: dt.timedelta = dt.timedelta(hours=24)
    booking_window: dt.timedelta = dt.timedelta(days=60)
    pending_timeout: dt.timedelta = dt.timedelta(minutes=30)
    make_default: bool = False


class PolicyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    default_status: str
    min_notice_booking: dt.timedelta
    min_notice_cancel: dt.timedelta
    booking_window: dt.timedelta
    pending_timeout: dt.timedelta


def check_table(capacity: int | None, attributes: dict[str, Any]) -> None:
    """Tables need a capacity; VIP tables may carry a minimum spend; location is indoor/outdoor. Raises ``ValueError``."""
    if not capacity:
        raise ValueError("tables require a capacity of at least 1")
    a = attributes
    if "is_vip" in a and not isinstance(a["is_vip"], bool):
        raise ValueError("attributes.is_vip must be a boolean")
    if "location" in a and a["location"] not in ("indoor", "outdoor"):
        raise ValueError("attributes.location must be 'indoor' or 'outdoor'")
    if "minimum_spend" in a:
        try:
            spend = Decimal(str(a["minimum_spend"]))
        except Exception as exc:  # noqa: BLE001
            raise ValueError("attributes.minimum_spend must be a number") from exc
        if spend < 0 or not a.get("is_vip", False):
            raise ValueError("minimum_spend must be >= 0 and is only valid on VIP tables")


class ResourceBase(BaseModel):
    type: Literal["staff", "table", "bay"] = "staff"
    name: str = Field(min_length=1, max_length=150)
    status: Literal["active", "inactive"] = "active"
    capacity: int | None = Field(default=None, ge=1)
    attributes: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_table(self) -> ResourceBase:
        if self.type == "table":
            check_table(self.capacity, self.attributes)
        return self


class ResourceCreate(ResourceBase):
    #: Staff only: where the person is told when their schedule collides (an add-on or an overrun runs into their next appointment).
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)  # (too short a number is refused when it is written the standard way)
    #: Staff only: their own video room, used for their online appointments.
    meeting_link: str | None = Field(default=None, min_length=8, max_length=2000, pattern=r"^https?://")

    @model_validator(mode="after")
    def _contacts_are_for_people(self) -> ResourceCreate:
        if self.type != "staff" and (self.contact_email or self.contact_phone or self.meeting_link):
            raise ValueError("contact details and a meeting link can only be saved for staff")
        return self


class ResourceUpdate(BaseModel):
    """Partial update of a resource; the type cannot change. ``attributes`` replaces the whole dictionary."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=150)
    status: Literal["active", "inactive"] | None = None
    capacity: int | None = Field(default=None, ge=1)
    attributes: dict[str, Any] | None = None
    #: Staff only; ``null`` removes it.
    contact_email: EmailStr | None = None
    contact_phone: str | None = Field(default=None, max_length=32)  # (too short a number is refused when it is written the standard way)
    #: Staff only; ``null`` removes it. Bookings that already have the old link keep it.
    meeting_link: str | None = Field(default=None, min_length=8, max_length=2000, pattern=r"^https?://")

    @model_validator(mode="after")
    def _no_nulls(self) -> ResourceUpdate:
        sent = self.model_dump(exclude_unset=True)
        if any(v is None for k, v in sent.items() if k not in ("contact_email", "contact_phone", "meeting_link")):
            raise ValueError("send only the fields to change; none of them can be null")
        return self


class ResourceOut(ResourceBase):
    """What the public staff / table list shows: no contact details."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class ResourceAdminOut(ResourceOut):
    """The admin view; the contact details are filled in for managers and owners only (staff always see ``null``)."""

    contact_email: str | None = None
    contact_phone: str | None = None
    meeting_link: str | None = None


def seconds_after(start: dt.time, t: dt.time) -> int:
    """Seconds from ``start`` forward (past midnight if needed) until the clock reads ``t``; ``0`` .. 86399."""
    return ((t.hour * 3600 + t.minute * 60 + t.second) - (start.hour * 3600 + start.minute * 60 + start.second)) % 86400


class ShiftIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0=Sunday .. 6=Saturday")
    start_time: dt.time
    end_time: dt.time
    break_start: dt.time | None = None
    break_end: dt.time | None = None

    @model_validator(mode="after")
    def _check(self) -> ShiftIn:
        """``end_time`` before ``start_time`` is an overnight shift (it ends the next day); equal times are not a shift."""
        if self.end_time == self.start_time:
            raise ValueError("end_time must differ from start_time (an end before the start means an overnight shift)")
        if (self.break_start is None) != (self.break_end is None):
            raise ValueError("break_start and break_end must be set together")
        if self.break_start is not None and self.break_end is not None:
            # measured from the start of the shift, so a break after midnight in an overnight shift is fine
            length, lo, hi = (seconds_after(self.start_time, t) for t in (self.end_time, self.break_start, self.break_end))
            if not (lo < hi <= length):
                raise ValueError("break must lie within the shift")
        return self


class ShiftOut(ShiftIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class DayOffCreate(BaseModel):
    resource_id: uuid.UUID | None = None
    name: str = Field(min_length=1, max_length=150)
    start_date: dt.date
    end_date: dt.date
    repeat_yearly: bool = False

    @model_validator(mode="after")
    def _check(self) -> DayOffCreate:
        if self.end_date < self.start_date:
            raise ValueError("end_date before start_date")
        return self


class DayOffOut(DayOffCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class BookingAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    customer_id: uuid.UUID
    #: Who booked, for every admin role (staff call a customer who is late or missing). An erased customer shows as "Erased customer" with no phone.
    customer_name: str | None = None
    customer_phone: str | None = None
    #: The email given with *this* booking (``bookings.contact_email``), not the customer's: anyone can type any address into a booking. Cleared by erasure.
    contact_email: str | None = None
    status: str
    start_at: dt.datetime
    end_at: dt.datetime
    party_size: int | None
    special_request: str | None
    hold_expires_at: dt.datetime | None
    source: str
    resource_ids: list[uuid.UUID] = Field(default_factory=list)
    service_ids: list[uuid.UUID] = Field(default_factory=list)
    has_health_notes: bool = False
    table_type: str | None = None
    allow_separate_seating: bool | None = None
    minimum_spend_agreed: Decimal | None = None
    vehicle_id: uuid.UUID | None = None
    lead_time_acknowledged_at: dt.datetime | None = None
    address_id: uuid.UUID | None = None
    meeting_link: str | None = None
    parent_booking_id: uuid.UUID | None = None
    actual_duration_minutes: int | None = None
    schedule_conflict: bool = False
    #: Why the booking needs a human look after a settings/shift/service change (see ``FLAG_REASONS``).
    flags: list[str] = Field(default_factory=list)


class TicketItemOut(BaseModel):
    service_id: uuid.UUID
    name: str
    duration_minutes: int
    #: Added on the spot after the booking was made.
    is_upsell: bool


class TicketProductOut(BaseModel):
    product_id: uuid.UUID
    name: str
    quantity: int
    unit_price: Decimal


class VisitAddressOut(BaseModel):
    """Where an on-site visit goes: the address to drive to, and its map pin (free times to move the visit are looked up from it)."""

    model_config = ConfigDict(from_attributes=True)
    line1: str
    city: str | None = None
    postcode: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class BookingDetailOut(BookingAdminOut):
    """Detail view; ``health_notes`` is only populated for owner/manager (PDPA)."""

    health_notes: str | None = None
    #: Spa and salon: what the customer said (``female`` / ``male`` / ``other``), else ``None``. Visible to every admin role; never in events or messages.
    customer_gender: str | None = None
    policy_snapshot: dict[str, Any]
    items: list[TicketItemOut] = Field(default_factory=list)
    products: list[TicketProductOut] = Field(default_factory=list)
    #: On-site visits: where to go (every admin role: staff see their own jobs only). ``None`` for other bookings, and once the customer is erased.
    visit_address: VisitAddressOut | None = None


class StatusChangeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    #: ``customer`` (actor_id = customer id), ``staff`` (actor_id = user id) or ``system`` (no id).
    actor_type: str
    actor_id: str | None
    changed_at: dt.datetime


class StatusPatch(BaseModel):
    status: BookingStatus


class OperatingHoursIn(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0=Sunday .. 6=Saturday")
    open_time: dt.time
    close_time: dt.time

    @model_validator(mode="after")
    def _check(self) -> OperatingHoursIn:
        """A ``close_time`` before ``open_time`` means the hours run past midnight (they belong to the day they open)."""
        if self.close_time == self.open_time:
            raise ValueError("close_time must differ from open_time (a close before the open means overnight hours)")
        return self


class OperatingHoursOut(OperatingHoursIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class TimeSlotSettingIn(BaseModel):
    booking_mode: Literal["flexible", "fixed"] = "flexible"
    slot_duration_minutes: int = Field(gt=0, le=720)
    slot_interval_minutes: int = Field(gt=0, le=720)
    first_slot_time: dt.time
    last_slot_time: dt.time
    buffer_minutes: int = Field(default=0, ge=0, le=240)
    #: Let customers choose their own table instead of being auto-assigned (default off).
    allow_table_selection: bool = False
    policy_id: uuid.UUID | None = None

    #: In fixed mode a ``last_slot_time`` before ``first_slot_time`` means the last slot is after midnight
    #: (a bar taking reservations from 18:00 to 01:00); nothing to validate.


class TimeSlotSettingOut(TimeSlotSettingIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class TableOpenRuleIn(BaseModel):
    """Tables open for online reservation on a weekday (0=Sun..6=Sat) or a specific date."""

    weekday: int | None = Field(default=None, ge=0, le=6)
    specific_date: dt.date | None = None
    table_type: Literal["normal", "vip"] = "normal"
    open_tables: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> TableOpenRuleIn:
        if (self.weekday is None) == (self.specific_date is None):
            raise ValueError("provide exactly one of weekday or specific_date")
        return self


class TableOpenRuleOut(TableOpenRuleIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class ResumeRequest(BaseModel):
    """Reschedule a job that was waiting for parts (automotive)."""

    start_at: AwareDatetime
    #: Named mechanic; omit to auto-assign.
    resource_id: uuid.UUID | None = None
    #: Services still to do (default: every service on the job order).
    service_ids: list[uuid.UUID] | None = None


class FollowUpRequest(BaseModel):
    """Book the standard service that follows an inspection (field service two-step workflow)."""

    service_id: uuid.UUID
    start_at: AwareDatetime
    resource_id: uuid.UUID | None = None


class ActualDurationIn(BaseModel):
    minutes: int = Field(gt=0, le=1440)


class OverrunResult(BaseModel):
    booking: BookingAdminOut
    #: Appointments the overrun runs into; they are flagged with ``schedule_conflict``.
    conflicting_booking_ids: list[uuid.UUID] = Field(default_factory=list)


class MeetingLinkIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000, pattern=r"^https?://")


class BusinessLocationIn(BaseModel):
    """Where the business is and how far its on-site services reach: the coordinates, the address to look up, or a Google Maps link (one of them)."""

    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    address: str | None = Field(default=None, min_length=3, max_length=300)
    #: The link Google Maps gives for the business (a share link ``https://maps.app.goo.gl/...`` or a full ``google.com/maps`` link): the point is read
    #: from it (``app.services.maps_links``), and it is saved as the profile's ``google_maps_url`` so the booking page can offer directions.
    maps_url: str | None = Field(default=None, min_length=12, max_length=2000)
    service_radius_km: float = Field(gt=0, le=20000)

    @model_validator(mode="after")
    def _one_way_to_say_where(self) -> "BusinessLocationIn":
        pin = self.latitude is not None or self.longitude is not None
        if pin and (self.latitude is None or self.longitude is None):
            raise ValueError("send both latitude and longitude")
        if [pin, self.address is not None, self.maps_url is not None].count(True) != 1:
            raise ValueError("send one of: latitude and longitude, an address to look up, or a Google Maps link")
        return self


class BusinessLocationOut(BaseModel):
    latitude: float | None
    longitude: float | None
    service_radius_km: float | None


class BusinessLocationSaved(BusinessLocationOut):
    """The answer to a save: what was stored, and, when the address was looked up, how the provider wrote it."""

    formatted_address: str | None = None
    #: The Google Maps link the location was read from (also saved as the profile's directions link), when one was sent.
    maps_url: str | None = None


class TicketProductIn(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(default=1, ge=1, le=1000)


class PartLineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: uuid.UUID
    quantity: Annotated[int, Field(ge=1, le=1000, strict=True)]


class PartsIn(BaseModel):
    """The whole list of parts on a job order: what is sent replaces what was there (an empty list clears it)."""

    model_config = ConfigDict(extra="forbid")

    parts: list[PartLineIn] = Field(max_length=50)


class TicketPartOut(BaseModel):
    product_id: uuid.UUID
    name: str
    quantity: int
    #: The price when the part was put on the job order.
    unit_price: Decimal
    #: What the shelf holds right now.
    in_stock: int
    #: The shelf holds less than the job needs: order it (and mark the job waiting-for-parts), or correct the stock count.
    needs_ordering: bool
    estimated_lead_time_days: int | None


class PartsOut(BaseModel):
    parts: list[TicketPartOut]
    #: True when any part needs ordering.
    needs_ordering: bool
    #: Quantity times the price each part was added at.
    total: Decimal


class UpsellRequest(BaseModel):
    """Add-on services and/or retail products to put on a running appointment's ticket."""

    service_ids: list[uuid.UUID] = Field(default_factory=list)
    products: list[TicketProductIn] = Field(default_factory=list)


class ClashAlert(BaseModel):
    """The stylist's next appointment that the upsell runs into."""

    booking_id: uuid.UUID
    status: str
    start_at: dt.datetime
    end_at: dt.datetime
    customer_name: str
    service_names: list[str]


class UpsellResult(BaseModel):
    booking: BookingAdminOut
    #: True when the reservation was stretched to fit the add-ons.
    extended: bool
    added_minutes: int
    #: Non-empty = alert: the add-on does not fit before these appointments (both sides are flagged).
    conflicts: list[ClashAlert] = Field(default_factory=list)
