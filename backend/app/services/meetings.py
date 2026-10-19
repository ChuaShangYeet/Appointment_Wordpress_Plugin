"""Meeting-link generation for virtual appointments (blueprint §11.3).

The blueprint calls for a Google Meet link created through the Google Calendar API when a virtual
appointment is confirmed. Online meetings were cancelled by the product owner (2026-10-06), so it is **not built**;
what exists is the seam it plugs into: a :class:`MeetingProvider` that is invoked on confirmation.
The default provider creates nothing, so ``meeting_link`` stays empty until an admin sets it
(``PATCH /admin/bookings/{id}/meeting-link``) or a real provider is registered.

A failing provider never fails the booking: the error is logged and the link stays empty (the
blueprint retries failed integrations in the background).
"""
from __future__ import annotations

import logging
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Booking, BookingItem, Service, Tenant

log = logging.getLogger(__name__)


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
    """Create a meeting link for a confirmed virtual professional appointment that has none yet."""
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
    try:
        link = await get_meeting_provider().create_meeting(booking=booking, tenant=tenant)
    except Exception:  # noqa: BLE001 - an integration outage must not undo a valid booking
        log.exception("meeting provider failed for booking %s", booking.id)
        return
    if link:
        booking.meeting_link = link
