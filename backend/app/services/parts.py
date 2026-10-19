"""Parts on a workshop job order (blueprint §10.1 "purchased_parts", §10.2 "Parts Ordering Status", §11.1).

A mechanic (or a manager) keeps the job order's list of parts up to date: ``set_parts`` replaces the whole list in one step, so the list is
always exactly what the caller sent and two people editing at once simply apply one after the other (the booking row is locked).

* A line is **priced when it is added** (today's catalog price) and keeps that price when its quantity changes later.
* Nothing is taken off the shelf while the job is running. The shelf is only *checked*: a line whose quantity is more than the shelf holds is
  flagged ``needs_ordering`` - the mechanic's cue to mark the job ``waiting-for-parts`` (the customer is told) and, when the parts arrive, to
  update the stock and resume the job. Such a line is still allowed on the job order (the part is wanted, it just is not here yet).
* The stock is **deducted when the job is completed** (:func:`app.services.inventory.deduct_stock`): never below zero, and a part that reaches zero
  shows *Out of Stock*. Two open jobs can still claim the last unit; the second completion simply leaves the shelf empty.

Applies to automotive and professional businesses (both have ``purchased_parts`` on the appointment); a salon's retail products have their own flow
(:mod:`app.services.upsell`).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InvalidTransition, NotFound, ValidationFailed
from app.models import Booking, BookingProduct, Product, Tenant

PARTS_BUSINESS_TYPES = frozenset({"automotive", "professional"})
#: A job's parts can be edited while the job is accepted and not yet finished.
EDITABLE_STATUSES = ("confirmed", "in-progress", "waiting-for-parts")
MAX_LINES = 50


@dataclass(frozen=True)
class PartLine:
    product_id: uuid.UUID
    name: str
    quantity: int
    unit_price: Decimal
    #: What the shelf holds right now.
    in_stock: int
    #: The shelf holds less than the job needs: the part has to be ordered (or the stock count corrected) before the job can finish properly.
    needs_ordering: bool
    estimated_lead_time_days: int | None


def ensure_supported(tenant: Tenant) -> None:
    if tenant.business_type not in PARTS_BUSINESS_TYPES:
        raise ValidationFailed("Parts on a job order apply to automotive and professional businesses")


async def parts_of(db: AsyncSession, booking_id: uuid.UUID) -> list[PartLine]:
    rows = (await db.execute(
        select(BookingProduct, Product).join(Product, Product.id == BookingProduct.product_id)
        .where(BookingProduct.booking_id == booking_id).order_by(Product.name, Product.id)
    )).all()
    return [
        PartLine(
            product_id=product.id, name=product.name, quantity=line.quantity, unit_price=line.unit_price, in_stock=product.stock_qty,
            needs_ordering=line.quantity > product.stock_qty, estimated_lead_time_days=product.estimated_lead_time_days,
        )
        for line, product in rows
    ]


async def get_parts(db: AsyncSession, tenant: Tenant, booking_id: uuid.UUID) -> list[PartLine]:
    ensure_supported(tenant)
    if await db.get(Booking, booking_id) is None:
        raise NotFound("Booking not found")
    return await parts_of(db, booking_id)


async def set_parts(db: AsyncSession, tenant: Tenant, booking_id: uuid.UUID, wanted: list[tuple[uuid.UUID, int]]) -> list[PartLine]:
    """Make the job order's parts exactly ``wanted`` (``(product id, quantity)`` pairs; an empty list clears them)."""
    ensure_supported(tenant)
    if len(wanted) > MAX_LINES:
        raise ValidationFailed(f"At most {MAX_LINES} different parts on one job order")
    ids = [product_id for product_id, _ in wanted]
    if len(set(ids)) != len(ids):
        raise ValidationFailed("Each part can appear once; give the total quantity")

    booking = await db.get(Booking, booking_id, with_for_update=True, populate_existing=True)  # edits to one job queue up; so does completing it
    if booking is None:
        raise NotFound("Booking not found")
    if booking.status not in EDITABLE_STATUSES:
        raise InvalidTransition(f"Parts can be changed on a confirmed, running or waiting-for-parts job (status is '{booking.status}')")

    products = {p.id: p for p in (await db.scalars(select(Product).where(Product.id.in_(ids)))).all()} if ids else {}
    if len(products) != len(ids):  # checked before anything is written
        raise NotFound("Unknown part")

    lines = {line.product_id: line for line in (await db.scalars(select(BookingProduct).where(BookingProduct.booking_id == booking.id))).all()}
    for product_id, line in lines.items():
        if product_id not in products:
            await db.delete(line)
    for product_id, quantity in wanted:
        if product_id in lines:
            lines[product_id].quantity = quantity  # the price it was added at stays
        else:
            db.add(BookingProduct(tenant_id=tenant.id, booking_id=booking.id, product_id=product_id, quantity=quantity, unit_price=products[product_id].price))
    await db.flush()
    return await parts_of(db, booking.id)
