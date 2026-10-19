"""Professional field-service allocator (blueprint §11): one professional, travel buffers, radius check.

* Exactly one service per booking; it happens *in office*, *virtual* or *on site*.
* **On-site** requests must carry the customer's coordinates and are refused (422
  ``outside_service_area``) if farther than ``service_radius_km`` from the business (PostGIS).
* **Travel buffer** - an on-site appointment reserves ``travel_buffer_minutes`` on the professional's
  schedule *before and after* the customer-visible window; the whole padded block must fit inside the
  professional's shift (and opening hours) and be free of other allocations. In-office and virtual
  services carry no travel time.
* **Auto-assignment** - the qualified professional with the lightest load that day, or a named one.

Existing on-site allocations (padded) are what later requests see as busy, so routing/travel time is
respected automatically. Travel is modelled per visit (back to base each time); chained-route
optimisation between consecutive visits is not attempted.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import NotFound, SlotUnavailable, ValidationFailed
from app.models import DayOff, Resource
from app.services.allocators.base import Assignment, BaseAllocator
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
from app.services.geo import ensure_within_service_area

Plan = list[tuple[dt.datetime, dt.datetime, list[Assignment]]]


def travel_pad(location_type: str, travel_buffer_minutes: int) -> dt.timedelta:
    """Travel time reserved on each side of an appointment: only on-site visits travel."""
    return dt.timedelta(minutes=travel_buffer_minutes if location_type == "on_site" else 0)


class FieldServiceAllocator(BaseAllocator):
    """Books one professional for one in-office, virtual or on-site service."""

    uses_locations = True

    async def find_slots(self, db: AsyncSession, req: AvailabilityRequest) -> list[SlotOption]:
        return [
            SlotOption(start=start.astimezone(req.tz), end=end.astimezone(req.tz), resource_ids=(assignments[0].resource_id,))
            for start, end, assignments in await self._plan(db, req)
        ]

    async def allocate(self, db: AsyncSession, req: AvailabilityRequest, start: dt.datetime) -> list[Assignment]:
        plan = await self._plan(db, req)
        chosen = next((a for s, _, a in plan if s == start), None)
        if chosen is None:
            raise SlotUnavailable("That time is not available", extra={"slots": [
                {"start_at": s.astimezone(req.tz).isoformat(), "end_at": e.astimezone(req.tz).isoformat(), "resource_ids": [str(a[0].resource_id)]}
                for s, e, a in plan
            ]})
        return chosen

    async def _plan(self, db: AsyncSession, req: AvailabilityRequest) -> Plan:
        """Every offered service start with its (travel-padded) allocation."""
        if len(req.services) != 1:
            raise ValidationFailed("Field-service bookings take exactly one service")
        svc = req.services[0]
        on_site = svc.location_type == "on_site"
        if on_site:
            if req.latitude is None or req.longitude is None:
                raise ValidationFailed("latitude and longitude are required for an on-site service")
            await ensure_within_service_area(db, req.tenant, req.latitude, req.longitude)
        pad = travel_pad(svc.location_type, svc.travel_buffer_minutes)
        if req.resource_id is not None:
            named = await db.get(Resource, req.resource_id)
            if named is None or named.type != "staff":
                raise NotFound("Professional not found")

        offs = (await db.scalars(select(DayOff))).all()
        if any(o.resource_id is None and covers_date(o, req.day) for o in offs):
            return []
        day_start, day_end = req.day_start, req.overnight_end  # a night shift may run into the next day
        op = await operating_windows(db, req)
        if op == []:
            return []
        hours = op if op is not None else [Interval(day_start, day_end)]

        on_leave = {o.resource_id for o in offs if o.resource_id and covers_date(o, req.day)}
        staff_ids = sorted(set((await qualified_by_service(db, req.services, "staff", req.resource_id))[svc.id]) - on_leave)
        if not staff_ids:
            return []
        names = {r.id: ResInfo(r.id, r.name) for r in (await db.scalars(select(Resource).where(Resource.id.in_(staff_ids)))).all()}
        working = await working_windows(db, req, staff_ids)
        busy = await busy_intervals(db, staff_ids, day_start, day_end, req.now)
        free = {m: subtract_intervals(intersect_intervals(working.get(m, []), hours), busy.get(m, [])) for m in staff_ids}
        load = {m: sum(iv.minutes for iv in busy.get(m, [])) for m in staff_ids}

        earliest, latest = booking_bounds(req)
        step = dt.timedelta(minutes=get_settings().slot_step_minutes)
        block = dt.timedelta(minutes=svc.duration_minutes + svc.buffer_minutes)

        plan: Plan = []
        start = day_start
        while start < day_end:  # the business day may run past midnight
            if earliest <= start <= latest:
                padded = Interval(start - pad, start + block + pad)
                candidates = [names[m] for m in staff_ids if m in names and fits(free[m], padded)]
                if candidates:
                    pick = choose_resource(candidates, None, load)
                    plan.append((start, start + block, [Assignment(pick.id, padded.start, padded.end, pad_before=pad, pad_after=pad)]))
            start += step
        return plan
