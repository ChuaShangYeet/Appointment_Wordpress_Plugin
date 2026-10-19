"""Field-service workflows that go beyond making a booking (blueprint §11.2-11.3).

* **Two-step workflow** - a quick *inspection* on site, then, for major work, a *follow-up* standard
  service booked against the same customer and address and linked to the inspection.
* **Overruns** - the professional reports the job's ``actual_duration``. If it runs past the plan and
  the professional is free afterwards, the reservation is stretched to match; if it runs into the next
  appointment, nothing is moved and both appointments are flagged with a schedule conflict for a human
  to resolve (the blueprint says flag, never auto-cancel).
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidTransition, ValidationFailed
from app.models import Booking, BookingAllocation, BookingItem, CustomerAddress, Service, Tenant
from app.services import events, plans
from app.services.allocators import Assignment, get_allocator
from app.services.availability import AvailabilityRequest, load_services
from app.services.booking import (
    ANY_AVAILABLE_ATTEMPTS,
    _tz,
    get_booking,
    policy_snapshot,
    release_expired_holds,
    reserve_with_retry,
    try_extend,
)
from app.services.meetings import ensure_meeting_link


def _require_professional(tenant: Tenant) -> None:
    if tenant.business_type != "professional":
        raise InvalidTransition("This action applies to professional (field-service) bookings only")


async def _booking_service(db: AsyncSession, booking: Booking) -> Service:
    """The (single) service of a field-service booking."""
    svc = await db.scalar(
        select(Service).join(BookingItem, BookingItem.service_id == Service.id).where(BookingItem.booking_id == booking.id).limit(1)
    )
    if svc is None:
        raise ValidationFailed("Booking has no service")
    return svc


async def create_follow_up(
    db: AsyncSession,
    tenant: Tenant,
    parent_id: uuid.UUID,
    service_id: uuid.UUID,
    start_at: dt.datetime,
    *,
    resource_id: uuid.UUID | None = None,
    actor_id: str | None = None,
    now: dt.datetime | None = None,
) -> Booking:
    """Book a standard service after an inspection, linked through ``parent_booking_id``.

    The parent must be an *inspection* that is in progress or completed. The follow-up reuses the
    parent's customer and address (an on-site follow-up is radius-checked again), is confirmed
    immediately (it is created by staff) and skips minimum-notice rules, but must start in the future.
    """
    _require_professional(tenant)
    now = now or dt.datetime.now(dt.UTC)
    await release_expired_holds(db, now)
    await plans.ensure_can_book(db, tenant, now)
    parent = await get_booking(db, parent_id)
    parent_service = await _booking_service(db, parent)
    if parent_service.service_category != "inspection":
        raise ValidationFailed("Only an inspection/consultation can have a follow-up service")
    if parent.status not in ("in-progress", "completed"):
        raise InvalidTransition(f"A follow-up can be booked once the inspection is under way (status is '{parent.status}')")
    if start_at <= now:
        raise ValidationFailed("start_at must be in the future")

    services = await load_services(db, [service_id])
    follow_up = services[0]
    if follow_up.service_category != "standard":
        raise ValidationFailed("A follow-up must be a standard service, not another inspection")

    allocator = get_allocator(tenant.business_type)
    policy = await allocator.resolve_policy(db, tenant, services)
    req = AvailabilityRequest(
        tenant=tenant, services=services, policy=policy, day=start_at.astimezone(_tz(tenant)).date(),
        resource_id=resource_id, now=now, ignore_notice=True,
    )
    address_id: uuid.UUID | None = None
    if follow_up.location_type == "on_site":
        address = await db.get(CustomerAddress, parent.address_id) if parent.address_id else None
        if address is None or address.latitude is None or address.longitude is None:
            raise ValidationFailed("The inspection has no mapped address to visit for on-site work")
        address_id, req.latitude, req.longitude = address.id, address.latitude, address.longitude

    def build(assignments: list[Assignment]) -> list[Any]:
        return [Booking(
            tenant_id=tenant.id, customer_id=parent.customer_id, status="confirmed",
            start_at=min(a.service_start for a in assignments), end_at=max(a.service_end for a in assignments),
            policy_snapshot=policy_snapshot(policy), address_id=address_id, parent_booking_id=parent.id, source="admin",
            items=[BookingItem(tenant_id=tenant.id, service_id=follow_up.id,
                               duration_minutes=follow_up.duration_minutes, buffer_minutes=follow_up.buffer_minutes)],
            allocations=[
                BookingAllocation(tenant_id=tenant.id, resource_id=a.resource_id, period=Range(a.start, a.end, bounds="[)"), state="active")
                for a in assignments
            ],
        )]

    attempts = 1 if resource_id else ANY_AVAILABLE_ATTEMPTS
    _, rows = await reserve_with_retry(db, allocator, req, start_at, attempts=attempts, build=build)
    booking: Booking = rows[0]
    events.record_created(db, booking, events.Actor("staff", actor_id))
    await ensure_meeting_link(db, booking, tenant)
    return booking


async def record_actual_duration(
    db: AsyncSession, tenant: Tenant, booking_id: uuid.UUID, minutes: int
) -> tuple[Booking, list[uuid.UUID]]:
    """Record how long an in-progress job really took; returns the booking and any conflicting bookings.

    * not longer than planned - only the figure is stored;
    * longer, and nothing else is booked in the extra time - the professional's reservation is
      stretched (and ``end_at`` moved) so the calendar stays truthful;
    * longer, and it runs into another appointment - the reservation is left alone (the exclusion
      constraint would refuse it anyway); this booking and the ones it collides with are flagged.
    """
    _require_professional(tenant)
    booking = await get_booking(db, booking_id, lock=True)
    if booking.status != "in-progress":
        raise InvalidTransition(f"Only a job in progress can overrun (status is '{booking.status}')")
    service = await _booking_service(db, booking)

    booking.actual_duration_minutes = minutes
    planned_end = booking.start_at + dt.timedelta(minutes=service.duration_minutes)
    actual_end = booking.start_at + dt.timedelta(minutes=minutes)
    overrun = actual_end - planned_end
    if overrun <= dt.timedelta(0):
        await db.flush()
        return booking, []

    extended, conflicts = await try_extend(db, booking, overrun)
    if extended:
        booking.end_at = booking.end_at + overrun
    elif conflicts:
        booking.schedule_conflict = True
        for other in (await db.scalars(select(Booking).where(Booking.id.in_(conflicts)))).all():
            other.schedule_conflict = True
        events.record_schedule_conflict(db, booking, conflicts, booking.end_at + overrun, "overrun")
    await db.flush()
    await db.refresh(booking, attribute_names=["allocations", "updated_at"])
    return booking, conflicts


async def resolve_schedule_conflict(db: AsyncSession, booking_id: uuid.UUID) -> Booking:
    """Clear the schedule-conflict flag once a human has dealt with the collision."""
    booking = await get_booking(db, booking_id)
    booking.schedule_conflict = False
    await db.flush()
    return booking


async def set_meeting_link(db: AsyncSession, tenant: Tenant, booking_id: uuid.UUID, url: str) -> Booking:
    """Attach a join link by hand (until a video-conferencing provider is integrated)."""
    _require_professional(tenant)
    booking = await get_booking(db, booking_id)
    booking.meeting_link = url
    await db.flush()
    return booking
