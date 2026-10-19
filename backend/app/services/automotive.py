"""Automotive rules that are not about scheduling: vehicle compatibility and parts lead time."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import IncompatibleVehicle, NotFound, ValidationFailed
from app.models import Product, Service, ServiceProduct, SupportedVehicle, Tenant, Vehicle
from app.schemas.public import BookingCreate


def normalise_plate(plate: str) -> str:
    """Upper-case and strip spaces/dashes so ``abc 123`` and ``ABC-123`` are the same vehicle."""
    return "".join(ch for ch in plate.upper() if ch.isalnum())


def year_allowed(sv: SupportedVehicle, year: int | None) -> bool:
    """A supported model may be limited to a year range; then the vehicle's year must be known and inside it."""
    if sv.year_from is None and sv.year_to is None:
        return True
    if year is None:
        return False
    return (sv.year_from is None or year >= sv.year_from) and (sv.year_to is None or year <= sv.year_to)


async def supported_model(db: AsyncSession, supported_id: uuid.UUID | None, year: int | None) -> SupportedVehicle:
    """The workshop's supported make/model for a vehicle, or :class:`IncompatibleVehicle` (unknown model, or a year outside its range)."""
    sv = await db.get(SupportedVehicle, supported_id) if supported_id else None
    if sv is None:
        raise IncompatibleVehicle("This workshop does not service that vehicle")
    if not year_allowed(sv, year):
        raise IncompatibleVehicle(
            f"This workshop services {sv.make} {sv.model} only for model years "
            f"{sv.year_from or '…'}-{sv.year_to or '…'}"
        )
    return sv


async def resolve_vehicle(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, data: BookingCreate) -> uuid.UUID:
    """Return the vehicle to book, registering it on the fly when given inline.

    The vehicle must match a workshop ``SupportedVehicle`` (make/model, and year if the entry is
    year-limited). ``vehicle_id`` must belong to the booking customer.
    """
    if (data.vehicle_id is None) == (data.vehicle is None):
        raise ValidationFailed("Provide exactly one of vehicle_id or vehicle for an automotive booking")

    if data.vehicle_id is not None:
        existing = await db.get(Vehicle, data.vehicle_id)
        if existing is None or existing.customer_id != customer_id:
            raise NotFound("Vehicle not found")
        supported_id, year = existing.supported_vehicle_id, existing.year
    else:
        assert data.vehicle is not None
        supported_id, year = data.vehicle.supported_vehicle_id, data.vehicle.year

    sv = await supported_model(db, supported_id, year)
    if data.vehicle_id is not None:
        return data.vehicle_id

    assert data.vehicle is not None
    insert_stmt = pg_insert(Vehicle).values(
        tenant_id=tenant.id, customer_id=customer_id, plate_no=normalise_plate(data.vehicle.plate_no),
        supported_vehicle_id=sv.id, make=sv.make, model=sv.model, year=year,
    )
    stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_vehicles_customer_plate",
        set_={"supported_vehicle_id": insert_stmt.excluded.supported_vehicle_id, "make": insert_stmt.excluded.make,
              "model": insert_stmt.excluded.model, "year": insert_stmt.excluded.year},
    ).returning(Vehicle.id)
    return (await db.execute(stmt)).scalar_one()


async def lead_time_notice(db: AsyncSession, services: list[Service]) -> list[dict[str, Any]]:
    """Parts the chosen services need that are only *available on order* (disclaimer before confirming)."""
    if not services:
        return []
    rows = (await db.execute(
        select(Product.name, Product.estimated_lead_time_days)
        .join(ServiceProduct, ServiceProduct.product_id == Product.id)
        .where(ServiceProduct.service_id.in_([s.id for s in services]), Product.stock_status == "available_on_order")
        .distinct().order_by(Product.name)
    )).all()
    return [{"name": name, "estimated_lead_time_days": days} for name, days in rows]
