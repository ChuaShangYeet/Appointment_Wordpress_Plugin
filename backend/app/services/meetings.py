"""Meeting-link generation for virtual appointments (blueprint §11.3).

**The business brings its own meeting service** (decided 2026-10-19): the system makes no video rooms. A link comes from
(1) the consultant's own saved room (``resources.meeting_link``: their Google Meet, Zoom or Teams link, set once), filled in when a
virtual appointment is confirmed, or (2) a link set on the booking by hand (``PATCH /admin/bookings/{id}/meeting-link``), which
always wins. A link set by hand on a confirmed booking is sent to the client (``booking.meeting_link_ready``); a link filled in on
confirmation goes out in the confirmation itself. :class:`MeetingProvider` stays as a seam for a video service integrated later
(none ships: the default creates nothing).

A failing provider never fails the booking: the error is logged and the link stays empty (the
blueprint retries failed integrations in the background).
"""
from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Booking, BookingAllocation, BookingItem, Resource, Service, Tenant

log = logging.getLogger(__name__)

#: The outbox event that sends the client a meeting link set (or changed) by hand.
MEETING_READY_EVENT = "booking.meeting_link_ready"


class MeetingProvider(Protocol):
    async def create_meeting(self, *, booking: Booking, tenant: Tenant) -> str | None:
        """Return a join URL for the appointment, or ``None`` if none could be created."""


class NoMeetingProvider:
    """Default: no video-conferencing integration configured."""

    async def create_meeting(self, *, booking: Booking, tenant: Tenant) -> str | None:
        return None


_provider: MeetingProvider = NoMeetingProvider()


def set_meeting_provider(provider: MeetingProvider) -> None:
    """Register the provider used for virtual appointments (tests and future integrations)."""
    global _provider
    _provider = provider


def get_meeting_provider() -> MeetingProvider:
    return _provider


async def ensure_meeting_link(db: AsyncSession, booking: Booking, tenant: Tenant) -> None:
    """Give a confirmed virtual professional appointment that has no link yet its consultant's saved room, else ask the provider."""
    if booking.meeting_link or booking.status != "confirmed" or tenant.business_type != "professional":
        return
    virtual = await db.scalar(
        select(Service.id)
        .join(BookingItem, BookingItem.service_id == Service.id)
        .where(BookingItem.booking_id == booking.id, Service.location_type == "virtual")
        .limit(1)
    )
    if virtual is None:
        return
    saved = await db.scalar(
        select(Resource.meeting_link)
        .join(BookingAllocation, BookingAllocation.resource_id == Resource.id)
        .where(BookingAllocation.booking_id == booking.id, BookingAllocation.state.in_(("held", "active")), Resource.type == "staff", Resource.meeting_link.is_not(None))
        .order_by(Resource.name).limit(1)
    )
    if saved:
        booking.meeting_link = saved
        return
    try:
        link = await get_meeting_provider().create_meeting(booking=booking, tenant=tenant)
    except Exception:  # noqa: BLE001 - an integration outage must not undo a valid booking
        log.exception("meeting provider failed for booking %s", booking.id)
        return
    if link:
        booking.meeting_link = link
