"""Automotive allocator (blueprint §10): mechanic AND bay, multi-service time stacking.

Rules implemented:

* **Double constraint** - every segment of a job needs a qualified mechanic *and* a supporting bay,
  both free for the whole segment; each segment reserves two allocation rows (one per resource).
* **Time stacking** - several services run back to back; segment *i* starts when segment *i-1*
  (duration + buffer) ends. Segments are disjoint in time, so the choice of resources per segment is
  independent: a slot exists iff every segment has at least one free mechanic and one free bay.
* **Continuity** - for each segment we prefer the previous segment's mechanic (and bay) when still
  eligible, otherwise the least-loaded mechanic / lowest-named bay (deterministic).
* Mechanics obey shifts, breaks, days off and opening hours; bays obey opening hours and days off.
* An optional named mechanic (``resource_id``) must be qualified for, and free during, every segment.

The interval helpers are pure functions so they can be tested without a database. Decisions run under a
per-tenant-per-day advisory lock (no spurious conflicts); the GiST constraint stays the final backstop.
"""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound, SlotUnavailable, ValidationFailed
from app.models import DayOff, Resource, Service
from app.services.allocators.base import Assignment, BaseAllocator, lock_day
from app.services.availability import (
    AvailabilityRequest,
    Interval,
    ResInfo,
    SlotOption,
    booking_bounds,
    busy_intervals,
    choose_resource,
    covers_date,
    fits,
    intersect_intervals,
    operating_windows,
    qualified_by_service,
    subtract_intervals,
    working_windows,
)

Plan = list[tuple[dt.datetime, dt.datetime, list[Assignment]]]


# --------------------------------------------------------------------------- pure logic
def plan_segments(start: dt.datetime, services: Sequence[Service]) -> list[tuple[Service, Interval]]:
    """Contiguous ``(service, interval)`` segments, each ``duration + buffer`` long, in request order."""
    out: list[tuple[Service, Interval]] = []
    cursor = start
    for svc in services:
        end = cursor + dt.timedelta(minutes=svc.duration_minutes + svc.buffer_minutes)
        out.append((svc, Interval(cursor, end)))
        cursor = end
    return out


# --------------------------------------------------------------------------- allocator
class WorkshopAllocator(BaseAllocator):
    """Reserves a mechanic and a bay for every service segment of a job order."""

    uses_vehicles = True
    retry_when_resource_named = True  # a named mechanic still leaves the bay to be auto-chosen

    async def find_slots(self, db: AsyncSession, req: AvailabilityRequest) -> list[SlotOption]:
        return [
            SlotOption(start=start.astimezone(req.tz), end=end.astimezone(req.tz), resource_ids=tuple(dict.fromkeys(a.resource_id for a in assignments)))
            for start, end, assignments in await self._plan(db, req)
        ]

    async def allocate(self, db: AsyncSession, req: AvailabilityRequest, start: dt.datetime) -> list[Assignment]:
        await lock_day(db, req.tenant.id, req.day, "workshop")
        plan = await self._plan(db, req)
        chosen = next((a for s, _, a in plan if s == start), None)
        if chosen is None:
            raise SlotUnavailable("That time is not available for this job", extra={"slots": [
                {"start_at": s.astimezone(req.tz).isoformat(), "end_at": e.astimezone(req.tz).isoformat(),
                 "resource_ids": [str(r) for r in dict.fromkeys(a.resource_id for a in assigns)]}
                for s, e, assigns in plan
            ]})
        return chosen

    # ------------------------------------------------------------------ internals
    async def _plan(self, db: AsyncSession, req: AvailabilityRequest) -> Plan:
        """Every offered start with its full set of allocations (two rows per segment)."""
        from app.core.config import get_settings

        if not req.services:
            raise ValidationFailed("At least one service is required")
        if req.resource_id is not None:
            named = await db.get(Resource, req.resource_id)
            if named is None or named.type != "staff":
                raise NotFound("Mechanic not found")

        offs = (await db.scalars(select(DayOff))).all()
        if any(o.resource_id is None and covers_date(o, req.day) for o in offs):
            return []
        day_start, day_end = req.day_start, req.overnight_end  # a night shift may run into the next day
        op = await operating_windows(db, req)
        if op == []:
            return []
        hours = op if op is not None else [Interval(day_start, day_end)]

        mechanics = await qualified_by_service(db, req.services, "staff", req.resource_id)
        bays = await qualified_by_service(db, req.services, "bay")
        if any(not ids for ids in mechanics.values()) or any(not ids for ids in bays.values()):
            return []  # some service has nobody / no bay that can do it at all

        on_leave = {o.resource_id for o in offs if o.resource_id and covers_date(o, req.day)}
        mech_ids = sorted({m for ids in mechanics.values() for m in ids} - on_leave)
        bay_ids = sorted({b for ids in bays.values() for b in ids} - on_leave)
        names = {
            r.id: ResInfo(r.id, r.name)
            for r in (await db.scalars(select(Resource).where(Resource.id.in_(mech_ids + bay_ids)))).all()
        }

        working = await working_windows(db, req, mech_ids)

        busy = await busy_intervals(db, mech_ids + bay_ids, day_start, day_end, req.now)
        mech_free = {m: subtract_intervals(intersect_intervals(working.get(m, []), hours), busy.get(m, [])) for m in mech_ids}
        bay_free = {b: subtract_intervals(hours, busy.get(b, [])) for b in bay_ids}
        load = {m: sum(iv.minutes for iv in busy.get(m, [])) for m in mech_ids}

        earliest, latest = booking_bounds(req)
        step = dt.timedelta(minutes=get_settings().slot_step_minutes)

        plan: Plan = []
        start = day_start
        while start < day_end:  # the business day may run past midnight
            if earliest <= start <= latest:
                assignments = self._assign(start, req.services, mechanics, bays, mech_free, bay_free, names, load)
                if assignments:
                    plan.append((start, max(a.end for a in assignments), assignments))
            start += step
        return plan

    @staticmethod
    def _assign(
        start: dt.datetime,
        services: Sequence[Service],
        mechanics: Mapping[uuid.UUID, list[uuid.UUID]],
        bays: Mapping[uuid.UUID, list[uuid.UUID]],
        mech_free: Mapping[uuid.UUID, list[Interval]],
        bay_free: Mapping[uuid.UUID, list[Interval]],
        names: Mapping[uuid.UUID, ResInfo],
        load: Mapping[uuid.UUID, float],
    ) -> list[Assignment]:
        """Resources for every segment starting at ``start``, or ``[]`` if any segment cannot be served."""
        assignments: list[Assignment] = []
        prev_mech: uuid.UUID | None = None
        prev_bay: uuid.UUID | None = None
        for svc, seg in plan_segments(start, services):
            m_cands = [names[m] for m in mechanics[svc.id] if m in names and fits(mech_free.get(m, []), seg)]
            b_cands = [names[b] for b in bays[svc.id] if b in names and fits(bay_free.get(b, []), seg)]
            if not m_cands or not b_cands:
                return []
            mech = choose_resource(m_cands, prev_mech, load)
            bay = choose_resource(b_cands, prev_bay, {})
            assignments += [Assignment(mech.id, seg.start, seg.end, service_id=svc.id), Assignment(bay.id, seg.start, seg.end, service_id=svc.id)]
            prev_mech, prev_bay = mech.id, bay.id
        return assignments
