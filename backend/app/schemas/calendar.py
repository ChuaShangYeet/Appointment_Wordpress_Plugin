"""Google Calendar connections (admin)."""
from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field


class CalendarConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: A staff member's own calendar; left out: the business's calendar, which receives every booking.
    resource_id: uuid.UUID | None = None
    #: ``primary`` (the signed-in account's main calendar) or the id of another calendar that account can write to.
    calendar_id: str = Field(default="primary", min_length=1, max_length=254)
    #: Put the customer's name, phone, party size, request and address in the events (otherwise: services, staff, status and a booking reference only).
    share_customer_details: bool = False


class CalendarConnectOut(BaseModel):
    #: Send the owner's browser here; Google asks them to allow access, then sends the browser to the server's callback.
    authorization_url: str


class CalendarConnectionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    share_customer_details: bool


class CalendarConnectionOut(BaseModel):
    id: uuid.UUID
    resource_id: uuid.UUID | None
    resource_name: str | None
    calendar_id: str
    account_label: str | None
    #: ``active``, or ``needs_reconnect`` (Google stopped accepting the saved sign-in, or the calendar kept refusing events: sync is paused until it is connected again).
    status: str
    share_customer_details: bool
    last_sync_at: dt.datetime | None
    last_error: str | None
    #: Booking events waiting to be written, written, and given up on.
    pending: int
    synced: int
    failed: int


class ResyncOut(BaseModel):
    queued: int
