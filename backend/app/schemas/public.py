"""Public (headless) request/response schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, EmailStr, Field, model_validator


class SlotOut(BaseModel):
    start_at: dt.datetime
    end_at: dt.datetime
    resource_ids: list[uuid.UUID]
    #: Restaurant: several tables were combined for this party.
    combined: bool = False
    #: Restaurant VIP: minimum spend the customer must accept to book this slot.
    minimum_spend: Decimal | None = None


class PartNotice(BaseModel):
    """A part that must be ordered; shown before the customer confirms."""

    name: str
    estimated_lead_time_days: int | None = None


class AvailabilityResponse(BaseModel):
    date: dt.date
    timezone: str
    slots: list[SlotOut]
    #: Automotive: parts needed by the chosen services that are only available on order.
    lead_time_notice: list[PartNotice] = Field(default_factory=list)


class DaySummary(BaseModel):
    date: dt.date
    #: At least one slot can be booked on this date.
    available: bool
    slot_count: int


class MonthAvailabilityResponse(BaseModel):
    """Which dates of a month have room (to grey out a calendar); ask the day view for the slots themselves."""

    month: str
    timezone: str
    days: list[DaySummary]
    #: Automotive: parts needed by the chosen services that are only available on order.
    lead_time_notice: list[PartNotice] = Field(default_factory=list)


#: Spa and salon customers may say (blueprint §8-9 "Customer Extensions: customer_gender"); optional, personal data.
Gender = Literal["female", "male", "other"]


class CustomerIn(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    phone: str = Field(min_length=6, max_length=32)
    #: The country of the code in front of the number (``SG``), chosen by the customer; left out, the business's country. A number typed with ``+`` keeps its own.
    #: A number new to the business must be a mobile of that country (422 with what it should look like); one already on file is taken as it is.
    phone_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    email: EmailStr | None = None
    #: Spa and salon only (422 elsewhere). Leaving it out keeps what is already known about this customer.
    gender: Gender | None = None


class ProfileOut(BaseModel):
    """What the signed-in customer's own record holds."""

    name: str
    phone: str
    email: str | None
    #: When the customer confirmed ``email`` through the link sent to it; ``None`` while it is unconfirmed (it then does not sign in as this customer).
    email_verified_at: dt.datetime | None = None
    #: ``None`` when never given, and always for businesses that do not collect it.
    gender: Gender | None


class ProfileUpdate(BaseModel):
    """``PATCH /public/me/profile``: only the fields sent change; ``"gender": null`` removes it."""

    model_config = ConfigDict(extra="forbid")

    gender: Gender | None = None

    @model_validator(mode="after")
    def _says_something(self) -> "ProfileUpdate":
        if not self.model_fields_set:
            raise ValueError("send at least one field to change")
        return self


class AddressIn(BaseModel):
    """On-site address: the coordinates of the customer's map pin, or - with both left out - just the address as typed, which the server looks up
    (422 when it cannot find it precisely, or when no geocoding provider is configured)."""

    line1: str = Field(min_length=1, max_length=500)
    city: str | None = Field(default=None, max_length=100)
    postcode: str | None = Field(default=None, max_length=16)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _a_pin_has_both_coordinates(self) -> "AddressIn":
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("send both latitude and longitude, or neither (the address is then looked up)")
        return self


class GeocodeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    address: str = Field(min_length=3, max_length=300)


class GeocodeOut(BaseModel):
    """Where an address is, so a page can show it and let the customer confirm before booking."""

    latitude: float
    longitude: float
    formatted_address: str
    #: False for an area or a partial match: show it, but ask for a fuller address or a pin (bookings refuse it).
    exact: bool
    #: Whether the point is inside this business's service area, and how far from the business it is; ``None`` when the business has set no
    #: service area (or PostGIS is not available).
    within_service_area: bool | None = None
    distance_km: float | None = None


class AddressUpdate(BaseModel):
    """Partial update of a saved address: only the fields sent change."""

    line1: str | None = Field(default=None, min_length=1, max_length=500)
    city: str | None = Field(default=None, max_length=100)
    postcode: str | None = Field(default=None, max_length=16)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)

    @model_validator(mode="after")
    def _required_fields_stay_set(self) -> "AddressUpdate":
        for name in ("line1", "latitude", "longitude"):  # sending null would blank a field every address must have
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be removed")
        if ("latitude" in self.model_fields_set) != ("longitude" in self.model_fields_set):
            raise ValueError("send both latitude and longitude, or neither")
        return self


class SavedAddressOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line1: str
    city: str | None
    postcode: str | None
    latitude: float | None
    longitude: float | None


class VehicleUpdate(BaseModel):
    """Partial update of a saved vehicle: only the fields sent change; the result must still be a model the workshop services."""

    plate_no: str | None = Field(default=None, min_length=1, max_length=20)
    supported_vehicle_id: uuid.UUID | None = None
    year: int | None = Field(default=None, ge=1900, le=2100)

    @model_validator(mode="after")
    def _required_fields_stay_set(self) -> "VehicleUpdate":
        for name in ("plate_no", "supported_vehicle_id"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be removed")
        return self


class SavedVehicleOut(BaseModel):
    id: uuid.UUID
    plate_no: str
    make: str | None
    model: str | None
    year: int | None
    supported_vehicle_id: uuid.UUID | None
    #: Whether the workshop still services this model and year (the supported list can change after a car was saved).
    serviceable: bool


class RescheduleRequest(BaseModel):
    """Move a booking to ``start_at``. Same services; staff stay the same unless one of the two options below is used."""

    start_at: AwareDatetime
    #: A different staff member (or, for a restaurant that lets guests choose, a table).
    resource_id: uuid.UUID | None = None
    #: Take the least-loaded qualified staff member instead of the one already booked.
    any_available: bool = False
    #: Restaurant VIP: accept a minimum spend that differs from the one already agreed.
    accept_minimum_spend: bool = False
    #: Restaurant VIP: the minimum spend the client showed for the new time and the customer accepted; the move is refused if the tables decided need more (see BookingCreate).
    accepted_minimum_spend: Decimal | None = Field(default=None, ge=0)


class VehicleIn(BaseModel):
    """Vehicle registered inline while booking; make/model come from the workshop's supported list."""

    plate_no: str = Field(min_length=1, max_length=20)
    supported_vehicle_id: uuid.UUID
    year: int | None = Field(default=None, ge=1900, le=2100)


class BookingCreate(BaseModel):
    #: Required for staff-based verticals; restaurants book a party and omit it.
    service_ids: list[uuid.UUID] = Field(default_factory=list)
    start_at: AwareDatetime
    #: Omit for "Any Available" (least-loaded qualified staff).
    resource_id: uuid.UUID | None = None
    customer: CustomerIn
    party_size: int | None = Field(default=None, ge=1)
    #: Restaurant only.
    table_type: Literal["normal", "vip"] = "normal"
    allow_separate_seating: bool = False
    #: Must be true when the assigned tables carry a minimum spend (any amount: prefer ``accepted_minimum_spend``).
    accept_minimum_spend: bool = False
    #: The minimum spend the client showed and the customer accepted (the slot's ``minimum_spend``). The tables are decided when the request
    #: arrives, so another booking in between can leave only a dearer room: then 422 ``minimum_spend_not_accepted`` with the new amount,
    #: never a booking at a higher amount than this. A lower one is fine. When given, ``accept_minimum_spend`` is not needed and not used.
    accepted_minimum_spend: Decimal | None = Field(default=None, ge=0)
    #: Automotive: exactly one of vehicle_id (a registered vehicle) or vehicle (register now).
    vehicle_id: uuid.UUID | None = None
    vehicle: VehicleIn | None = None
    #: Automotive: must be true when a needed part is only available on order.
    acknowledge_lead_time: bool = False
    #: Field service, on-site services only: exactly one of address_id (saved) or address (new).
    address_id: uuid.UUID | None = None
    address: AddressIn | None = None
    special_request: str | None = Field(default=None, max_length=2000)
    #: PDPA-sensitive; stored AES-256-GCM encrypted, never echoed back publicly.
    health_notes: str | None = Field(default=None, max_length=5000)
    source: Literal["wordpress", "headless"] = "headless"


class CustomerBookingOut(BaseModel):
    """A booking as its customer sees it: no health notes, policy internals or staff flags."""

    id: uuid.UUID
    status: str
    start_at: dt.datetime
    end_at: dt.datetime
    service_names: list[str] = Field(default_factory=list)
    party_size: int | None = None
    meeting_link: str | None = None
    #: Whether the customer can cancel it right now under the booking's own policy.
    can_cancel: bool
    #: Confirmed bookings only: the last moment cancellation is allowed.
    cancel_deadline: dt.datetime | None = None
    #: Whether the customer can move it right now (the same rule as cancelling: ``cancel_deadline`` is also its deadline).
    can_reschedule: bool


class BookingConfirmation(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: str
    start_at: dt.datetime
    end_at: dt.datetime
    hold_expires_at: dt.datetime | None
    resource_ids: list[uuid.UUID]
    minimum_spend_agreed: Decimal | None = None
    #: Virtual appointments: join link, once generated (may arrive after confirmation).
    meeting_link: str | None = None
