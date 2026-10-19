"""Publishing outbox events (blueprint §2.3): at least once, retried with back-off, never lost.

The outbox table is filled in the same transaction as each change (:mod:`app.services.events`). This module hands the
pending events to a publisher: the worker (``python -m app.worker``) installs one for customer and staff messages, outgoing webhooks and calendar sync. Without one
events simply wait in the table. RabbitMQ is not used; :func:`set_event_publisher` is where a broker would plug in.

Delivery is *at least once*: an event is marked published only after the publisher accepted it, so a crash in between
publishes it again. Consumers must tolerate duplicates (``OutboxEvent.id`` identifies an event). A failed event is retried
with exponential back-off, and events of the same aggregate are not delivered out of order while an earlier one is failing.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.events import OutboxEvent

MAX_BACKOFF = dt.timedelta(hours=1)


class EventPublisher(Protocol):
    async def publish(self, event: OutboxEvent) -> None:
        """Deliver ``event``; raise to have it retried later."""


class CompositePublisher:
    """Hands each event to several publishers (customer notifications, webhooks). All of them are tried every time; if any failed the
    event is retried as a whole, which is safe because each publisher is idempotent per event (the notification log and the
    webhook delivery table both remember what they already did)."""

    def __init__(self, *publishers: EventPublisher) -> None:
        self.publishers = publishers

    async def publish(self, event: OutboxEvent) -> None:
        errors: list[Exception] = []
        for publisher in self.publishers:
            try:
                await publisher.publish(event)
            except Exception as exc:  # noqa: BLE001 - the others still get their turn
                errors.append(exc)
        if errors:
            raise errors[0] if len(errors) == 1 else RuntimeError("; ".join(f"{type(e).__name__}: {e}" for e in errors))


_publisher: EventPublisher | None = None


def set_event_publisher(publisher: EventPublisher | None) -> None:
    global _publisher
    _publisher = publisher


def get_event_publisher() -> EventPublisher | None:
    return _publisher


def backoff(attempts: int) -> dt.timedelta:
    """30 s, 1 min, 2 min, ... capped at an hour."""
    delay: dt.timedelta = dt.timedelta(seconds=30) * (2 ** max(attempts - 1, 0))
    return min(delay, MAX_BACKOFF)


@dataclass
class DispatchResult:
    published: int = 0
    failed: int = 0


async def dispatch_pending(
    db: AsyncSession, publisher: EventPublisher, *, now: dt.datetime | None = None, batch: int = 100
) -> DispatchResult:
    """Publish due events, oldest first; failures are scheduled for a retry."""
    now = now or dt.datetime.now(dt.UTC)
    result = DispatchResult()
    events = (await db.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None), OutboxEvent.available_at <= now)
        .order_by(OutboxEvent.seq)
        .limit(batch)
        .with_for_update(skip_locked=True)
    )).all()
    blocked: set[tuple[str, uuid.UUID]] = set()  # aggregates that must wait: an earlier event of theirs is unpublished
    for event in events:
        key = (event.aggregate_type, event.aggregate_id)
        if key in blocked:
            continue
        # An aggregate's events go out in order: never ahead of an earlier one that is still unpublished (failing and
        # waiting out its back-off, or locked by another worker). Events published earlier in this batch count as done.
        earlier = select(OutboxEvent.id).where(
            OutboxEvent.aggregate_type == event.aggregate_type, OutboxEvent.aggregate_id == event.aggregate_id,
            OutboxEvent.published_at.is_(None), OutboxEvent.seq < event.seq,
        ).limit(1)
        if await db.scalar(earlier) is not None:
            blocked.add(key)
            continue
        try:
            await publisher.publish(event)
        except Exception as exc:  # noqa: BLE001 - whatever the publisher raises means "try again later"
            event.attempts += 1
            event.last_error = f"{type(exc).__name__}: {exc}"[:500]
            event.available_at = now + backoff(event.attempts)
            blocked.add(key)
            result.failed += 1
        else:
            event.published_at = now
            result.published += 1
    await db.flush()
    return result


async def purge_published(db: AsyncSession, *, older_than: dt.datetime) -> int:
    """Delete events that were published before ``older_than``."""
    deleted = await db.execute(delete(OutboxEvent).where(OutboxEvent.published_at.is_not(None), OutboxEvent.published_at < older_than))
    return int(deleted.rowcount or 0)  # type: ignore[attr-defined]
