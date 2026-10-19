"""Allocator contract shared by all business verticals."""
from __future__ import annotations

import datetime as dt
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BookingPolicy, Service, Tenant
from app.services.availability import AvailabilityRequest, SlotOption, resolve_policy


async def lock_day(db: AsyncSession, tenant_id: uuid.UUID, day: dt.date, namespace: str) -> None:
    """Serialise reservations for one tenant and day until the transaction ends (advisory lock).

    Used by verticals whose decision spans several resources or a cap (restaurant, workshop) so
    concurrent requests decide one at a time against committed state.
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"),
        {"k": f"{namespace}:{tenant_id}:{day.isoformat()}"},
    )


@dataclass(frozen=True)
class Assignment:
    """One resource reserved over ``[start, end)``."""

    resource_id: uuid.UUID
    start: dt.datetime
    end: dt.datetime
    #: Minimum spend attached to this resource (VIP tables); 0 otherwise.
    minimum_spend: Decimal = Decimal(0)
    #: Time reserved on the resource before/after the customer-visible window (on-site travel).
    pad_before: dt.timedelta = dt.timedelta(0)
    pad_after: dt.timedelta = dt.timedelta(0)
    #: The service this reservation is for, when the booking is made of segments (a workshop job order: each segment is one
    #: service and has its own mechanic and bay). ``None`` when one resource serves the whole booking.
    service_id: uuid.UUID | None = None

    @property
    def service_start(self) -> dt.datetime:
        return self.start + self.pad_before

    @property
    def service_end(self) -> dt.datetime:
        return self.end - self.pad_after


class BaseAllocator(ABC):
    """Vertical-specific strategy: which slots exist and which resources serve a chosen slot.

    Allocators only *decide*; the reservation itself is written by
    :mod:`app.services.booking` inside one transaction, where the PostgreSQL exclusion
    constraint remains the final arbiter of conflicts.
    """

    #: Staff-based verticals book named services; restaurants book a party and need none.
    requires_services: ClassVar[bool] = True
    #: Automotive: bookings carry a vehicle and the parts lead-time disclaimer applies.
    uses_vehicles: ClassVar[bool] = False
    #: Field service: on-site services need a customer address checked against the service radius.
    uses_locations: ClassVar[bool] = False
    #: True when a customer-named resource still leaves other resources to be auto-chosen (e.g. the
    #: bay when a mechanic is named), so a lost race should be re-decided rather than reported.
    retry_when_resource_named: ClassVar[bool] = False

    async def resolve_policy(self, db: AsyncSession, tenant: Tenant, services: list[Service]) -> BookingPolicy:
        """Effective booking policy; verticals may override where the override lives."""
        return await resolve_policy(db, tenant, services)

    @abstractmethod
    async def find_slots(self, db: AsyncSession, req: AvailabilityRequest) -> list[SlotOption]:
        """Candidate slots for ``req.day``."""

    @abstractmethod
    async def allocate(self, db: AsyncSession, req: AvailabilityRequest, start: dt.datetime) -> list[Assignment]:
        """Pick resources for a slot starting at ``start``; raise ``SlotUnavailable`` if it is not offered."""
