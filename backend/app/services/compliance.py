"""PDPA data-subject requests and retention (blueprint §16).

* **Access**: the customer asks for the data held about them; an owner completes the request and receives an export.
* **Erasure**: the customer asks to be forgotten; an owner completes it. The customer record is anonymised (name, phone,
  email, gender), their saved addresses and vehicles are deleted, and the free-text and health notes, the contact email and the idempotency key and
  request hash on their bookings are cleared; their bookings' Google Calendar events are rewritten without them (when the business shared customer details). The bookings themselves stay (the business needs its own records of what was done and when), with no way back
  to the person. Refused while the customer still has an upcoming or running booking.
* **Retention**: health notes are kept only as long as ``HEALTH_NOTES_RETENTION_DAYS`` after the booking ended
  (0 = keep, the default); the sweep clears older ones.

Everything here is written to the audit log.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, ErasureBlocked, NotFound
from app.core.security import decrypt_field
from app.models import Booking, BookingItem, Customer, CustomerAddress, NotificationLog, Service, Tenant, Vehicle
from app.models.business import Review
from app.models.compliance import DataRequest
from app.services import audit, calendar

#: A customer with a booking in one of these states cannot be erased yet.
OPEN_STATUSES = ("pending", "confirmed", "in-progress", "waiting-for-parts")
FINISHED_STATUSES = ("completed", "cancelled", "rejected", "no-show")
ERASED_NAME = "Erased customer"


async def open_request(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, kind: str) -> DataRequest:
    """A customer asks for ``access`` or ``erasure``; only one request of each kind can be open at a time."""
    already = await db.scalar(
        select(DataRequest.id).where(DataRequest.customer_id == customer_id, DataRequest.kind == kind, DataRequest.status == "pending")
    )
    if already is not None:
        raise Conflict(f"You already have an open {kind} request")
    request = DataRequest(tenant_id=tenant.id, customer_id=customer_id, kind=kind)
    db.add(request)
    await db.flush()
    return request


async def _get_request(db: AsyncSession, request_id: uuid.UUID) -> DataRequest:
    request = await db.get(DataRequest, request_id, with_for_update=True, populate_existing=True)
    if request is None:
        raise NotFound("Data request not found")
    return request


async def export_customer(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID) -> dict[str, Any]:
    """Everything held about one customer, with health notes decrypted (owners only)."""
    customer = await db.get(Customer, customer_id)
    if customer is None:
        raise NotFound("Customer not found")
    addresses = (await db.scalars(select(CustomerAddress).where(CustomerAddress.customer_id == customer_id))).all()
    vehicles = (await db.scalars(select(Vehicle).where(Vehicle.customer_id == customer_id))).all()
    bookings = (await db.scalars(select(Booking).where(Booking.customer_id == customer_id).order_by(Booking.start_at))).unique().all()
    names: dict[uuid.UUID, list[str]] = {}
    if bookings:
        for booking_id, name in (await db.execute(
            select(BookingItem.booking_id, Service.name).join(Service, Service.id == BookingItem.service_id)
            .where(BookingItem.booking_id.in_([b.id for b in bookings])).order_by(Service.name)
        )).all():
            names.setdefault(booking_id, []).append(name)
    reviews = (await db.scalars(select(Review).where(Review.customer_id == customer_id).order_by(Review.created_at))).all()
    return {
        "customer": {"id": str(customer.id), "name": customer.name, "phone": customer.phone, "email": customer.email, "gender": customer.gender,
                     "email_confirmed_at": customer.email_verified_at.isoformat() if customer.email_verified_at else None,
                     "created_at": customer.created_at.isoformat(), "erased_at": customer.erased_at.isoformat() if customer.erased_at else None},
        "addresses": [{"line1": a.line1, "city": a.city, "postcode": a.postcode, "latitude": a.latitude, "longitude": a.longitude} for a in addresses],
        "vehicles": [{"plate_no": v.plate_no} for v in vehicles],
        "reviews": [{"booking_id": str(r.booking_id), "rating": r.rating, "comment": r.comment, "status": r.status, "created_at": r.created_at.isoformat()} for r in reviews],
        "bookings": [
            {"id": str(b.id), "status": b.status, "start_at": b.start_at.isoformat(), "end_at": b.end_at.isoformat(), "services": names.get(b.id, []),
             "party_size": b.party_size, "special_request": b.special_request, "source": b.source, "contact_email": b.contact_email,
             "health_notes": decrypt_field(b.health_notes_enc, tenant.id) if b.health_notes_enc else None}
            for b in bookings
        ],
    }


async def erase_customer(db: AsyncSession, tenant: Tenant, customer_id: uuid.UUID, now: dt.datetime) -> None:
    """Anonymise a customer (see the module docstring). Raises :class:`ErasureBlocked` while a booking is still open."""
    customer = await db.get(Customer, customer_id, with_for_update=True, populate_existing=True)
    if customer is None:
        raise NotFound("Customer not found")
    if customer.erased_at is not None:
        raise Conflict("This customer has already been erased")
    open_bookings = await db.scalar(
        select(func.count()).select_from(Booking).where(Booking.customer_id == customer_id, Booking.status.in_(OPEN_STATUSES))
    ) or 0
    if open_bookings:
        raise ErasureBlocked(
            f"{open_bookings} booking(s) are still open; complete or cancel them first", extra={"open_bookings": open_bookings}
        )
    await db.execute(
        update(Booking).where(Booking.customer_id == customer_id)
        .values(vehicle_id=None, address_id=None, special_request=None, health_notes_enc=None, contact_email=None, idempotency_key=None, idempotency_hash=None)
    )
    await db.execute(update(Review).where(Review.customer_id == customer_id).values(comment=None))  # the rating stays, anonymously
    await db.execute(delete(NotificationLog).where(NotificationLog.customer_id == customer_id))
    await db.execute(delete(CustomerAddress).where(CustomerAddress.customer_id == customer_id))
    await db.execute(delete(Vehicle).where(Vehicle.customer_id == customer_id))
    customer.name = ERASED_NAME
    customer.phone = f"erased-{customer.id.hex[:24]}"  # unique per customer, and short enough for the phone column
    customer.email = None
    customer.email_verified_at = None
    customer.gender = None
    customer.erased_at = now
    await db.flush()
    # Google Calendar events written while the owner shared customer details still hold the name, phone and request: queue every booking of the customer so the sweep
    # writes them again from what is held now (nothing). A paused calendar is rewritten in full when it is connected again.
    for booking_id in (await db.scalars(select(Booking.id).where(Booking.customer_id == customer_id))).all():
        await calendar.mark_booking(db, booking_id)


async def complete_request(
    db: AsyncSession, tenant: Tenant, request_id: uuid.UUID, actor_id: str, role: str, now: dt.datetime
) -> tuple[DataRequest, dict[str, Any] | None]:
    """Carry out a pending request. Returns it and, for ``access``, the export."""
    request = await _get_request(db, request_id)
    if request.status != "pending":
        raise Conflict(f"This request is already {request.status}")
    export: dict[str, Any] | None = None
    if request.kind == "access":
        export = await export_customer(db, tenant, request.customer_id)
        audit.record(db, tenant_id=tenant.id, action="customer.data.export", actor_id=actor_id, role=role,
                     target_type="customer", target_id=request.customer_id, detail={"request_id": str(request.id)})
    else:
        await erase_customer(db, tenant, request.customer_id, now)
        audit.record(db, tenant_id=tenant.id, action="customer.data.erase", actor_id=actor_id, role=role,
                     target_type="customer", target_id=request.customer_id, detail={"request_id": str(request.id)})
    request.status, request.processed_at, request.processed_by = "completed", now, actor_id
    await db.flush()
    return request, export


async def reject_request(db: AsyncSession, request_id: uuid.UUID, actor_id: str, note: str, now: dt.datetime) -> DataRequest:
    request = await _get_request(db, request_id)
    if request.status != "pending":
        raise Conflict(f"This request is already {request.status}")
    request.status, request.processed_at, request.processed_by, request.note = "rejected", now, actor_id, note
    await db.flush()
    return request


async def purge_health_notes(db: AsyncSession, tenant_id: uuid.UUID, *, now: dt.datetime, days: int, batch: int = 500) -> int:
    """Clear health notes on finished bookings that ended more than ``days`` days ago (``days <= 0``: keep everything)."""
    if days <= 0:
        return 0
    bookings = (await db.scalars(
        select(Booking).where(
            Booking.health_notes_enc.is_not(None), Booking.status.in_(FINISHED_STATUSES), Booking.end_at < now - dt.timedelta(days=days),
        ).order_by(Booking.end_at).limit(batch).with_for_update(skip_locked=True)
    )).unique().all()
    for b in bookings:
        b.health_notes_enc = None
    if bookings:
        audit.record(db, tenant_id=tenant_id, action="retention.health_notes.purged", detail={"count": len(bookings), "retention_days": days})
        await db.flush()
    return len(bookings)
