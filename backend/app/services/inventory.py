"""Retail inventory on an appointment's ticket (blueprint §9.3, "Inventory Deduction")."""
from __future__ import annotations

from sqlalchemy import and_, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Booking, BookingProduct, Product


async def deduct_stock(db: AsyncSession, booking: Booking) -> None:
    """Reduce stock for every product on the ticket; a product that hits zero shows *Out of Stock*.

    Each product is one atomic ``UPDATE`` (the right-hand sides all see the pre-update quantity), so
    concurrent completions cannot lose updates. Quantity never goes below zero: if two tickets both
    claimed the last unit, the second completion simply leaves the shelf empty.
    """
    for line in (await db.scalars(select(BookingProduct).where(BookingProduct.booking_id == booking.id))).all():
        remaining = Product.stock_qty - line.quantity
        await db.execute(
            update(Product)
            .where(Product.id == line.product_id)
            .values(
                stock_qty=func.greatest(remaining, 0),
                stock_status=case(
                    (and_(Product.stock_status == "in_stock", remaining <= 0), "out_of_stock"),
                    else_=Product.stock_status,
                ),
            )
            .execution_options(synchronize_session=False)
        )


async def finalise_completion(db: AsyncSession, booking: Booking) -> None:
    """On completion: deduct ticket stock and lock in the duration (base + every add-on, incl. upsells)."""
    await deduct_stock(db, booking)
    if booking.actual_duration_minutes is None:
        booking.actual_duration_minutes = sum(item.duration_minutes for item in booking.items)
