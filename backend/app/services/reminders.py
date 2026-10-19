"""Booking reminders (blueprint §13.5: "send reminders, for example 24 hours and 2 hours before").

The sweep calls :func:`enqueue_due` for every tenant. For each configured lead time (``REMINDER_HOURS``, default 24 and 2) it finds
the *confirmed* bookings whose reminder moment (``start - lead``) has arrived, and for each one writes a ``booking.reminder``
outbox event plus a ``booking_reminders`` row, in the same transaction. The row's primary key ``(booking, kind)`` is what makes a
reminder go out **once**: it is inserted ``ON CONFLICT DO NOTHING`` and the event is only written when the insert happened, so
two sweeps, a retry, or a crash and re-run cannot remind twice. Delivery then goes through the ordinary notification path
(templates, channels, retries, log).

Who is *not* reminded, on purpose:

* bookings that are not ``confirmed`` (pending requests, cancelled, rejected, in progress...);
* a reminder moment that came **before the booking existed** (booked at 20 h ahead: no "24 hours before" message straight after
  the confirmation), and one that is **more than** ``REMINDER_LATENESS_MINUTES`` (60) past - a worker that was down, or a booking approved
  late, does not send a "tomorrow" reminder three hours before the appointment. Such a reminder is simply skipped, never queued for later.

A booking that is moved forgets its reminders (:func:`forget`), so the new time gets its own; a reminder already queued for the
old time is recognised as stale by the notification publisher and not sent.
"""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Booking
from app.models.notifications import BookingReminder
from app.services import events

#: Bookings handled per lead time per tick (a backlog is worked off over the next ticks).
BATCH = 500


def kind_for(hours: int) -> str:
    return f"{hours}h"


async def enqueue_due(db: AsyncSession, tenant_id: uuid.UUID, now: dt.datetime, lead_hours: Sequence[int] | None = None) -> int:
    """Queue the reminders that have come due; returns how many were queued.

    ``lead_hours`` are the business's own lead times; ``None`` means it has not chosen any and the server's ``REMINDER_HOURS`` apply."""
    settings = get_settings()
    late = dt.timedelta(minutes=settings.reminder_lateness_minutes)
    queued = 0
    for hours in sorted(set(settings.reminder_hours if lead_hours is None else lead_hours), reverse=True):
        lead, kind = dt.timedelta(hours=hours), kind_for(hours)
        due = (await db.scalars(
            select(Booking)
            .where(
                Booking.status == "confirmed",
                Booking.start_at <= now + lead,          # the reminder moment has arrived...
                Booking.start_at > now + lead - late,    # ...and is not too long ago
                Booking.start_at > now,                  # (never remind about something already under way)
                Booking.created_at <= Booking.start_at - lead,  # the booking existed at that moment
                ~select(BookingReminder.booking_id).where(BookingReminder.booking_id == Booking.id, BookingReminder.kind == kind).exists(),
            )
            .order_by(Booking.start_at, Booking.id)
            .limit(BATCH)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )).unique().all()
        for booking in due:
            queued += await queue_one(db, tenant_id, booking, hours)
    return queued


async def queue_one(db: AsyncSession, tenant_id: uuid.UUID, booking: Booking, hours: int) -> int:
    """Record and queue one reminder: 1, or 0 if that reminder already exists (the primary key is the last line of defence
    against a second sweep that selected the same booking)."""
    kind = kind_for(hours)
    inserted = await db.scalar(
        pg_insert(BookingReminder)
        .values(tenant_id=tenant_id, booking_id=booking.id, kind=kind)
        .on_conflict_do_nothing()
        .returning(BookingReminder.booking_id)
    )
    if inserted is None:
        return 0
    events.enqueue(
        db, tenant_id, "booking.reminder", "booking", booking.id,
        {"booking_id": str(booking.id), "status": booking.status, "kind": kind, "hours_before": hours,
         "start_at": booking.start_at.isoformat(), "end_at": booking.end_at.isoformat()},
    )
    return 1


async def forget(db: AsyncSession, booking_id: uuid.UUID) -> None:
    """The booking moved: its sent reminders say nothing about the new time."""
    await db.execute(delete(BookingReminder).where(BookingReminder.booking_id == booking_id))
