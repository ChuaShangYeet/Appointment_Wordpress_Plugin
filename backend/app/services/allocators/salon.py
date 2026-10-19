"""Salon / barber allocator (blueprint §9): one base service plus optional add-ons, one stylist.

* An appointment has **exactly one base service**; **add-ons** (``Service.is_addon``) are optional extras
  that *extend the interval* - the stylist is reserved for base + add-ons back to back.
* **Auto-assignment** - with no stylist chosen, availability is computed across every active stylist
  qualified for the *base* service (add-ons never narrow the choice); the least-loaded one is assigned.
* A cart mixing services with different booking policies is governed by the strictest one (merged in
  :func:`app.services.availability.resolve_policy`).

Adding add-ons or products *during* the appointment (on-the-spot upsell) and inventory deduction on
completion are handled in :mod:`app.services.upsell` and :mod:`app.services.inventory`.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationFailed
from app.models import Service
from app.services.allocators.base import Assignment
from app.services.allocators.staff import StaffAllocator
from app.services.availability import AvailabilityRequest, SlotOption


def split_cart(services: list[Service]) -> tuple[Service, list[Service]]:
    """``(base service, add-ons)`` of a cart; raises unless it has exactly one base service."""
    base = [s for s in services if not s.is_addon]
    if len(base) != 1:
        raise ValidationFailed(
            "An appointment needs exactly one base service; add-ons are optional extras "
            f"(got {len(base)} base services)"
        )
    return base[0], [s for s in services if s.is_addon]


class StylistUpsellAllocator(StaffAllocator):
    """Staff allocation where the cart is one base service plus add-ons that lengthen the booking."""

    def _prepare(self, req: AvailabilityRequest) -> None:
        base, _ = split_cart(req.services)
        req.qualifying_services = [base]  # qualification follows the base service only

    async def find_slots(self, db: AsyncSession, req: AvailabilityRequest) -> list[SlotOption]:
        self._prepare(req)
        return await super().find_slots(db, req)

    async def allocate(self, db: AsyncSession, req: AvailabilityRequest, start: dt.datetime) -> list[Assignment]:
        self._prepare(req)
        return await super().allocate(db, req, start)
