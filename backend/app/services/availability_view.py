"""What the public availability endpoints return: one day's slots, computed once and shared by the day and month views.

The month view is built from exactly the same per-day answers (and the same cache entries) as the day view, so the
two can never disagree about a date, and invalidation needs nothing extra.
"""
from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BookingPolicy, Service, Tenant
from app.schemas.public import AvailabilityResponse, PartNotice, SlotOut
from app.services.allocators import BaseAllocator
from app.services.automotive import lead_time_notice
from app.services.availability import AvailabilityRequest, load_services


@dataclass(frozen=True)
class DayQuery:
    """The parts of an availability request that do not depend on the date."""

    tenant: Tenant
    allocator: BaseAllocator
    service_ids: list[uuid.UUID]
    resource_id: uuid.UUID | None = None
    party_size: int | None = None
    table_type: str = "normal"
    allow_separate_seating: bool = False
    latitude: float | None = None
    longitude: float | None = None

    @property
    def cacheable(self) -> bool:
        """A customer's map pin is personal and effectively unique: such requests are never cached or used as keys."""
        return self.latitude is None and self.longitude is None

    def cache_params(self, day: dt.date) -> dict[str, Any]:
        return {
            "date": day.isoformat(), "services": [str(i) for i in self.service_ids] if self.allocator.requires_services else [],
            "resource": str(self.resource_id) if self.resource_id else None, "party": self.party_size,
            "table_type": self.table_type, "separate": self.allow_separate_seating,
        }


@dataclass(frozen=True)
class Prepared:
    """Loaded once per request, however many days it covers."""

    services: list[Service]
    policy: BookingPolicy


async def prepare(db: AsyncSession, q: DayQuery) -> Prepared:
    services = await load_services(db, q.service_ids) if q.allocator.requires_services else []
    return Prepared(services, await q.allocator.resolve_policy(db, q.tenant, services))


async def compute_day(
    db: AsyncSession, q: DayQuery, prep: Prepared, day: dt.date, now: dt.datetime | None = None
) -> AvailabilityResponse:
    """Run the availability pipeline for one date (no cache involved)."""
    req = AvailabilityRequest(
        tenant=q.tenant, services=prep.services, policy=prep.policy, day=day, resource_id=q.resource_id,
        party_size=q.party_size, table_type=q.table_type, allow_separate_seating=q.allow_separate_seating,
        latitude=q.latitude, longitude=q.longitude, now=now or dt.datetime.now(dt.UTC),
    )
    slots = await q.allocator.find_slots(db, req)
    notice = await lead_time_notice(db, prep.services) if q.allocator.uses_vehicles else []
    return AvailabilityResponse(
        date=day,
        timezone=q.tenant.timezone,
        lead_time_notice=[PartNotice(**n) for n in notice],
        slots=[
            SlotOut(start_at=s.start, end_at=s.end, resource_ids=list(s.resource_ids), combined=s.combined, minimum_spend=s.minimum_spend)
            for s in slots
        ],
    )


def cache_payload(policy: BookingPolicy, response: AvailabilityResponse) -> str:
    """What is stored in Redis: the answer plus the policy bounds needed to keep it honest as time passes."""
    return json.dumps({
        "min_notice_s": policy.min_notice_booking.total_seconds(), "window_s": policy.booking_window.total_seconds(),
        "response": response.model_dump(mode="json"),
    })


def still_bookable(cached: dict[str, object], now: dt.datetime) -> AvailabilityResponse | None:
    """A cached answer with the slots the clock has since made unbookable removed (``None`` if unreadable).

    Only removes slots: the minimum-notice cut-off moves forward with time, and a slot that has slipped inside
    it would be refused at booking time. (Slots that newly enter the booking window wait for the entry to expire.)
    """
    try:
        response = AvailabilityResponse.model_validate(cached["response"])
        earliest = now + dt.timedelta(seconds=float(str(cached["min_notice_s"])))
        latest = now + dt.timedelta(seconds=float(str(cached["window_s"])))
    except (KeyError, ValueError):
        return None
    response.slots = [s for s in response.slots if earliest <= s.start_at <= latest]
    return response


def read_cached(payload: str | None, now: dt.datetime) -> AvailabilityResponse | None:
    """A usable cached answer, or ``None`` for a miss or an entry that cannot be trusted."""
    if payload is None:
        return None
    try:
        return still_bookable(json.loads(payload), now)
    except ValueError:
        return None


def nothing_bookable(day: dt.date, tz: dt.tzinfo, policy: BookingPolicy, now: dt.datetime) -> bool:
    """True when no slot on ``day`` can be bookable under ``policy``: the whole business day is before the
    minimum-notice cut-off (which covers every past date) or after the end of the booking window. No pipeline run is
    needed. A business day can run past midnight (overnight hours), so it is taken to last up to 24 hours longer."""
    start = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
    end = dt.datetime.combine(day + dt.timedelta(days=2), dt.time.min, tzinfo=tz)
    return end <= now + policy.min_notice_booking or start > now + policy.booking_window
