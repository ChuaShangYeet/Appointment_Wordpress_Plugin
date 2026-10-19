"""Recording what happened to a booking: status history and outbox events, always written together.

Both rows are added to the caller's session, so they commit (or roll back) with the change itself: a booking that
lost a race and was rolled back leaves no history and no event, and a committed change can never lack either.
"""
from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Booking
from app.models.events import BookingStatusHistory, OutboxEvent


@dataclass(frozen=True)
class Actor:
    """Who made a change: ``customer`` (id = customer id), ``staff`` (id = user id) or ``system``."""

    type: str
    id: str | None = None


SYSTEM = Actor("system")


def enqueue(
    db: AsyncSession, tenant_id: uuid.UUID, event_type: str, aggregate_type: str, aggregate_id: uuid.UUID, payload: dict[str, Any]
) -> OutboxEvent:
    """Add an event to the outbox (published later, at least once)."""
    event = OutboxEvent(tenant_id=tenant_id, event_type=event_type, aggregate_type=aggregate_type, aggregate_id=aggregate_id, payload=payload)
    db.add(event)
    return event


def status_event_type(status: str) -> str:
    """``in-progress`` -> ``booking.in_progress``."""
    return "booking." + status.replace("-", "_")


def _payload(booking_id: uuid.UUID, status: str, previous: str | None, start_at: dt.datetime, end_at: dt.datetime) -> dict[str, Any]:
    # ids, status and times only: no names, phone numbers, notes or addresses
    return {"booking_id": str(booking_id), "status": status, "previous_status": previous,
            "start_at": start_at.isoformat(), "end_at": end_at.isoformat()}


def _record(
    db: AsyncSession, tenant_id: uuid.UUID, booking_id: uuid.UUID, previous: str | None, status: str, actor: Actor,
    start_at: dt.datetime, end_at: dt.datetime, *, event_types: Iterable[str],
) -> None:
    db.add(BookingStatusHistory(
        tenant_id=tenant_id, booking_id=booking_id, from_status=previous, to_status=status, actor_type=actor.type, actor_id=actor.id,
    ))
    for event_type in event_types:
        enqueue(db, tenant_id, event_type, "booking", booking_id, _payload(booking_id, status, previous, start_at, end_at))


def record_created(db: AsyncSession, booking: Booking, actor: Actor) -> None:
    """A new booking: ``booking.created``, plus ``booking.confirmed`` when it starts out confirmed (no approval step)."""
    types = ["booking.created"] + (["booking.confirmed"] if booking.status == "confirmed" else [])
    _record(db, booking.tenant_id, booking.id, None, booking.status, actor, booking.start_at, booking.end_at, event_types=types)


def record_status_change(db: AsyncSession, booking: Booking, previous: str, actor: Actor) -> None:
    """The booking has just been moved from ``previous`` to ``booking.status``."""
    _record(db, booking.tenant_id, booking.id, previous, booking.status, actor, booking.start_at, booking.end_at,
            event_types=[status_event_type(booking.status)])


def record_rescheduled(
    db: AsyncSession, booking: Booking, previous_start: dt.datetime, previous_end: dt.datetime, actor: Actor
) -> None:
    """The booking moved to a new time, keeping its status. No status-history row (the status did not change): the outbox
    event carries the old and the new time."""
    payload = _payload(booking.id, booking.status, booking.status, booking.start_at, booking.end_at)
    payload.update(previous_start_at=previous_start.isoformat(), previous_end_at=previous_end.isoformat(), actor_type=actor.type)
    enqueue(db, booking.tenant_id, "booking.rescheduled", "booking", booking.id, payload)


SCHEDULE_CONFLICT_EVENT = "booking.schedule_conflict"


def record_schedule_conflict(
    db: AsyncSession, booking: Booking, clash_ids: Iterable[uuid.UUID], wanted_end: dt.datetime, cause: str,
) -> None:
    """A running appointment needs more time (an add-on, an overrun) but the next one is in the way: nothing was moved, both are flagged, and this
    event lets the staff member and the business's own systems know. Ids and times only - the next appointment's details are looked up
    when the stylist's message is written (and by whoever holds the admin API)."""
    enqueue(db, booking.tenant_id, SCHEDULE_CONFLICT_EVENT, "booking", booking.id, {
        "booking_id": str(booking.id), "conflicting_booking_ids": [str(i) for i in clash_ids], "cause": cause,
        "start_at": booking.start_at.isoformat(), "end_at": booking.end_at.isoformat(), "wanted_end_at": wanted_end.isoformat(),
    })


def record_expired(db: AsyncSession, rows: Iterable[tuple[uuid.UUID, uuid.UUID, dt.datetime, dt.datetime]]) -> None:
    """Pending holds that ran out: ``(booking id, tenant id, start, end)`` per booking, each ``pending -> rejected`` by the system."""
    for booking_id, tenant_id, start_at, end_at in rows:
        _record(db, tenant_id, booking_id, "pending", "rejected", SYSTEM, start_at, end_at, event_types=["booking.rejected"])
