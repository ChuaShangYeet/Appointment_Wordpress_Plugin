"""Reviews of completed appointments (blueprint §8).

A customer can review a booking of their own once it is ``completed``, once. The review is attributed to the staff member
who served it, when there is one. Publicly a review shows the rating, the comment and the reviewer's *first name* only;
a manager can hide a review (it is kept, not shown, and left out of the average).
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, NotFound, Unauthorized
from app.core.security import decode_review_token
from app.models import Booking, BookingAllocation, BookingItem, BusinessProfile, Customer, Resource, Service, Tenant
from app.models.business import Review
from app.schemas.business import ReviewInvitationOut
from app.services import events
from app.services.booking import visible_to_customer

ANONYMOUS = "Anonymous"


def first_name(name: str, erased: bool) -> str:
    """What the public sees of a reviewer: their first name, or ``Anonymous`` once they have been erased."""
    parts = name.split()
    return ANONYMOUS if erased or not parts else parts[0]


async def create_review(
    db: AsyncSession, tenant: Tenant, booking_id: uuid.UUID, customer_id: uuid.UUID, rating: int, comment: str | None, email_scope: str | None = None
) -> Review:
    booking = await db.get(Booking, booking_id, with_for_update=True, populate_existing=True)
    if booking is None or not visible_to_customer(booking, customer_id, email_scope):
        raise NotFound("Booking not found")  # someone else's booking looks the same as a missing one
    if booking.status != "completed":
        raise Conflict("Only a completed appointment can be reviewed")
    if await db.scalar(select(Review.id).where(Review.booking_id == booking_id)) is not None:
        raise Conflict("You have already reviewed this appointment")
    server = await db.scalar(
        select(BookingAllocation.resource_id).join(Resource, Resource.id == BookingAllocation.resource_id)
        .where(BookingAllocation.booking_id == booking_id, Resource.type == "staff").order_by(func.lower(BookingAllocation.period)).limit(1)
    )
    review = Review(
        tenant_id=tenant.id, booking_id=booking_id, customer_id=customer_id, resource_id=server, rating=rating,
        comment=(comment or "").strip() or None,
    )
    db.add(review)
    await db.flush()
    events.enqueue(db, tenant.id, "review.created", "review", review.id,
                   {"review_id": str(review.id), "booking_id": str(booking_id), "resource_id": str(server) if server else None, "rating": rating})
    return review


# --------------------------------------------------------------------------- invitations (the link in the completed-visit message)
async def invited_booking(db: AsyncSession, tenant: Tenant, token: str) -> tuple[Booking, Customer]:
    """The booking and customer a review token stands for. A token from another business, a forged or expired one is a 401; a booking that is
    gone, or whose customer has been erased (nothing may be attached to an anonymised person), looks like any missing booking."""
    tenant_id, booking_id = decode_review_token(token)
    if tenant_id != tenant.id:
        raise Unauthorized("This review link is invalid or has expired")
    booking = await db.get(Booking, booking_id)
    customer = await db.get(Customer, booking.customer_id) if booking is not None else None
    if booking is None or customer is None or customer.erased_at is not None:
        raise NotFound("Booking not found")
    return booking, customer


async def invitation(db: AsyncSession, tenant: Tenant, token: str) -> ReviewInvitationOut:
    """What the review page needs to say what is being reviewed, and whether it still can be."""
    booking, _ = await invited_booking(db, tenant, token)
    names = (await db.scalars(
        select(Service.name).join(BookingItem, BookingItem.service_id == Service.id).where(BookingItem.booking_id == booking.id).order_by(BookingItem.seq)
    )).all()
    reviewed = await db.scalar(select(Review.id).where(Review.booking_id == booking.id)) is not None
    status = "already_reviewed" if reviewed else ("open" if booking.status == "completed" else "not_completed")
    return ReviewInvitationOut(
        business_name=await db.scalar(select(BusinessProfile.business_name)) or tenant.name, service_names=list(names), start_at=booking.start_at, status=status,
    )
