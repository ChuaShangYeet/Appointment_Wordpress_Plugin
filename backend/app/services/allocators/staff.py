"""Spa / Clinic / Salon allocator: a single staff member per booking."""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import SlotUnavailable
from app.services.allocators.base import Assignment, BaseAllocator
from app.services.availability import (
    AvailabilityRequest,
    SlotOption,
    busy_intervals,
    compute_availability,
)


def pick_least_loaded(candidates: Sequence[uuid.UUID], load_minutes: Mapping[uuid.UUID, float]) -> uuid.UUID:
    """"Any Available" routing: fewest booked minutes that day; ties broken by id for determinism."""
    return min(candidates, key=lambda r: (load_minutes.get(r, 0.0), str(r)))


class StaffAllocator(BaseAllocator):
    """Specific staff selection, or "Any Available" auto-routing to the least-loaded qualified staff."""

    async def find_slots(self, db: AsyncSession, req: AvailabilityRequest) -> list[SlotOption]:
        return await compute_availability(db, req, resource_type="staff")

    async def allocate(self, db: AsyncSession, req: AvailabilityRequest, start: dt.datetime) -> list[Assignment]:
        slots = await self.find_slots(db, req)
        wanted = start.astimezone(dt.UTC)  # (compared as instants: ``==`` on two zones calls a repeated clock hour unequal to everything, so the second 02:30 could never be booked)
        slot = next((s for s in slots if s.start.astimezone(dt.UTC) == wanted), None)
        if slot is None:
            raise SlotUnavailable("That time is not available", extra={"slots": [_slot_json(s) for s in slots]})

        if req.resource_id:  # explicit choice: the pipeline already restricted candidates to it
            chosen = req.resource_id
        else:
            busy = await busy_intervals(db, list(slot.resource_ids), req.day_start, req.overnight_end, req.now)
            load = {rid: sum(i.minutes for i in ivs) for rid, ivs in busy.items()}
            chosen = pick_least_loaded(slot.resource_ids, load)
        return [Assignment(chosen, slot.start.astimezone(dt.UTC), slot.end.astimezone(dt.UTC))]  # (UTC: min/max of two local times in one zone compare clock readings)


def _slot_json(s: SlotOption) -> dict[str, object]:
    return {"start_at": s.start.isoformat(), "end_at": s.end.isoformat(), "resource_ids": [str(r) for r in s.resource_ids]}
