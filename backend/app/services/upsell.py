"""On-the-spot upsell for salon appointments (blueprint §9.2-9.3).

The stylist marks the appointment *in progress* on arrival and can then add **add-ons** or retail
**products** to the ticket:

* an add-on *extends the allocation* by its duration (+ buffer). If that runs into the stylist's next
  appointment the stylist is **alerted with that appointment's details** - the add-on is still put on the
  ticket (the customer asked for it), but nothing is forced into the other customer's time: the
  reservation is left as it is and both appointments are flagged ``schedule_conflict`` for a human;
* a product goes on the ticket at today's price; stock is checked now and **deducted when the
  appointment is completed** (:mod:`app.services.inventory`).

The booking row is locked (``SELECT ... FOR UPDATE``) for the whole operation, so concurrent upsells to
the same appointment apply one after the other instead of overwriting each other's extension.
"""
from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import InsufficientStock, InvalidTransition, NotFound, ValidationFailed
from app.models import Booking, BookingItem, BookingProduct, Customer, Product, Service, Tenant
from app.services import events
from app.services.availability import load_services
from app.services.booking import try_extend


@dataclass(frozen=True)
class ClashInfo:
    """The stylist's next appointment that an upsell would run into."""

    booking_id: uuid.UUID
    status: str
    start_at: dt.datetime
    end_at: dt.datetime
    customer_name: str
    service_names: list[str]


@dataclass
class UpsellOutcome:
    booking: Booking
    extended: bool
    added_minutes: int
    clashes: list[ClashInfo] = field(default_factory=list)


async def _clash_details(db: AsyncSession, ids: list[uuid.UUID]) -> list[ClashInfo]:
    if not ids:
        return []
    rows = (await db.execute(
        select(Booking, Customer.name).join(Customer, Customer.id == Booking.customer_id)
        .where(Booking.id.in_(ids)).order_by(Booking.start_at)
    )).all()
    names: dict[uuid.UUID, list[str]] = {}
    for booking_id, name in (await db.execute(
        select(BookingItem.booking_id, Service.name).join(Service, Service.id == BookingItem.service_id)
        .where(BookingItem.booking_id.in_(ids)).order_by(Service.name)
    )).all():
        names.setdefault(booking_id, []).append(name)
    return [ClashInfo(b.id, b.status, b.start_at, b.end_at, customer, names.get(b.id, [])) for b, customer in rows]


async def add_to_ticket(
    db: AsyncSession,
    tenant: Tenant,
    booking_id: uuid.UUID,
    service_ids: list[uuid.UUID],
    products: list[tuple[uuid.UUID, int]],
) -> UpsellOutcome:
    """Add add-ons and/or products to an in-progress salon appointment."""
    if tenant.business_type != "salon":
        raise InvalidTransition("On-the-spot upsell applies to salon appointments only")
    if not service_ids and not products:
        raise ValidationFailed("Provide at least one add-on service or product")

    booking = await db.get(Booking, booking_id, with_for_update=True, populate_existing=True)
    if booking is None:
        raise NotFound("Booking not found")
    if booking.status != "in-progress":
        raise InvalidTransition(f"Items can be added once the appointment is in progress (status is '{booking.status}')")

    addons = await load_services(db, service_ids) if service_ids else []
    for svc in addons:
        if not svc.is_addon:
            raise ValidationFailed(f"'{svc.name}' is a base service; only add-ons can be added to a running appointment")

    # validate every product before writing anything
    on_ticket = {
        line.product_id: line
        for line in (await db.scalars(select(BookingProduct).where(BookingProduct.booking_id == booking.id))).all()
    }
    wanted: dict[uuid.UUID, int] = {}
    for product_id, quantity in products:
        wanted[product_id] = wanted.get(product_id, 0) + quantity
    stock: dict[uuid.UUID, Product] = {}
    for product_id, quantity in wanted.items():
        product = await db.get(Product, product_id)
        if product is None:
            raise NotFound("Unknown product")
        already = on_ticket[product_id].quantity if product_id in on_ticket else 0
        if product.stock_status == "out_of_stock" or already + quantity > product.stock_qty:
            raise InsufficientStock(
                f"Only {product.stock_qty} of '{product.name}' in stock", extra={"product_id": str(product.id), "in_stock": product.stock_qty}
            )
        stock[product_id] = product

    for product_id, quantity in wanted.items():
        if product_id in on_ticket:
            on_ticket[product_id].quantity += quantity
        else:
            db.add(BookingProduct(
                tenant_id=tenant.id, booking_id=booking.id, product_id=product_id, quantity=quantity, unit_price=stock[product_id].price,
            ))
    for svc in addons:
        booking.items.append(BookingItem(
            tenant_id=tenant.id, service_id=svc.id, duration_minutes=svc.duration_minutes,
            buffer_minutes=svc.buffer_minutes, is_upsell=True, seq=len(booking.items),
        ))

    added = sum(svc.duration_minutes + svc.buffer_minutes for svc in addons)
    extended = False
    clash_ids: list[uuid.UUID] = []
    if added:
        delta = dt.timedelta(minutes=added)
        extended, clash_ids = await try_extend(db, booking, delta)
        if extended:
            booking.end_at = booking.end_at + delta
        elif clash_ids:
            booking.schedule_conflict = True
            for other in (await db.scalars(select(Booking).where(Booking.id.in_(clash_ids)))).all():
                other.schedule_conflict = True
            events.record_schedule_conflict(db, booking, clash_ids, booking.end_at + delta, "upsell")  # the stylist is told (and so is anyone subscribed)
    await db.flush()
    await db.refresh(booking, attribute_names=["allocations", "items", "updated_at"])
    return UpsellOutcome(booking, extended, added, await _clash_details(db, clash_ids))
