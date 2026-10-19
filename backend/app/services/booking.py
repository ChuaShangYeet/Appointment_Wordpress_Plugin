"""Booking transaction and state machine.

Reservation happens in one transaction: expire stale holds, re-run the availability pipeline
for the requested slot, upsert the customer, then insert ``bookings`` + ``booking_allocations``
inside a SAVEPOINT. If two requests race past the pipeline, PostgreSQL's exclusion constraint
rejects the loser (SQLSTATE 23P01) and we translate that into :class:`SlotConflict` carrying
refreshed slots.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import re
import uuid
from decimal import Decimal
from collections.abc import Callable
from typing import Any

from asyncpg.exceptions import DeadlockDetectedError, ExclusionViolationError  # type: ignore[import-untyped]
from sqlalchemy import exists, func, select, text, update
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    IdempotencyKeyReused,
    InvalidTransition,
    LeadTimeNotAcknowledged,
    MinimumSpendNotAccepted,
    NotFound,
    SlotConflict,
    SlotUnavailable,
    TooLateToCancel,
    TooLateToReschedule,
    ValidationFailed,
)
from app.core.config import get_settings
from app.core.phone import normalise_email, normalise_phone
from app.core.security import encrypt_field
from app.models import (
    Booking,
    BookingAllocation,
    BookingItem,
    BookingPolicy,
    Customer,
    CustomerAddress,
    OperatingHours,
    Resource,
    ResourceShift,
    Service,
    Tenant,
)
from app.schemas.public import BookingCreate
from app.services import events, reminders
from app.services.allocators import Assignment, BaseAllocator, get_allocator
from app.services.automotive import lead_time_notice, resolve_vehicle
from app.services.customer_profile import ensure_gender_allowed
from app.services.inventory import deduct_stock, finalise_completion
from app.services import plans
from app.services.locations import resolve_address
from app.services.meetings import ensure_meeting_link
from app.services.parts import PARTS_BUSINESS_TYPES
from app.services.availability import (
    AvailabilityRequest,
    SlotOption,
    load_services,
    validate_booking_window,
    weekday_index,
)

EXCLUSION_VIOLATION = "23P01"
DEADLOCK_DETECTED = "40P01"
#: Re-decide attempts for "Any Available" after losing a race (candidates are few; this is ample).
ANY_AVAILABLE_ATTEMPTS = 5

#: from-status -> allowed to-statuses (blueprint §13.4).
#: ``waiting-for-parts`` is for automotive job orders only (checked in ``transition_booking``).
TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"confirmed", "rejected", "cancelled"}),
    "confirmed": frozenset({"cancelled", "in-progress", "completed", "no-show"}),
    "in-progress": frozenset({"completed", "waiting-for-parts"}),
    #: leaving waiting-for-parts to "confirmed" is a reschedule: see ``resume_booking``
    "waiting-for-parts": frozenset({"cancelled"}),
}
#: business types that use the arrival ("in-progress") step
IN_PROGRESS_VERTICALS = frozenset({"salon", "automotive", "professional"})
RELEASING = frozenset({"rejected", "cancelled", "completed", "no-show"})
#: what a customer may cancel online; anything started or finished needs the business
CUSTOMER_CANCELLABLE = frozenset({"pending", "confirmed"})
#: statuses in which a booking holds a future slot (the only ones review flags apply to)
LIVE_STATUSES = frozenset({"pending", "confirmed"})


def is_exclusion_violation(exc: BaseException) -> bool:
    """True if ``exc`` (or its cause chain) is PostgreSQL's exclusion_violation / asyncpg's ExclusionViolationError."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ExclusionViolationError):
            return True
        if getattr(seen, "sqlstate", None) == EXCLUSION_VIOLATION or getattr(seen, "pgcode", None) == EXCLUSION_VIOLATION:
            return True
        seen = seen.__cause__ or getattr(seen, "orig", None)
    return False


def is_lost_race(exc: BaseException) -> bool:
    """True if a write lost a race for a resource: an exclusion violation, or PostgreSQL picking it as a deadlock victim.

    Two transactions inserting overlapping ranges for the same resources at the same moment can each end
    up waiting for the other's uncommitted row; PostgreSQL then aborts one with ``deadlock_detected``
    (SQLSTATE 40P01) instead of raising the exclusion violation. Either way nothing was double booked and
    the loser should be treated like any other lost race, not answered with a server error.
    """
    if is_exclusion_violation(exc):
        return True
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, DeadlockDetectedError):
            return True
        if getattr(seen, "sqlstate", None) == DEADLOCK_DETECTED or getattr(seen, "pgcode", None) == DEADLOCK_DETECTED:
            return True
        seen = seen.__cause__ or getattr(seen, "orig", None)
    return False


def policy_snapshot(policy: BookingPolicy) -> dict[str, Any]:
    """Immutable copy of the policy in force at booking time."""
    merged_from: list[str] | None = getattr(policy, "merged_from", None)
    return {
        **({"merged_from": merged_from} if merged_from else {}),
        "policy_id": str(policy.id),
        "name": policy.name,
        "default_status": policy.default_status,
        "min_notice_booking_s": int(policy.min_notice_booking.total_seconds()),
        "min_notice_cancel_s": int(policy.min_notice_cancel.total_seconds()),
        "booking_window_s": int(policy.booking_window.total_seconds()),
        "pending_timeout_s": int(policy.pending_timeout.total_seconds()),
    }


def slot_to_json(s: SlotOption) -> dict[str, Any]:
    out: dict[str, Any] = {
        "start_at": s.start.isoformat(), "end_at": s.end.isoformat(),
        "resource_ids": [str(r) for r in s.resource_ids],
    }
    if s.combined:
        out["combined"] = True
    if s.minimum_spend is not None:
        out["minimum_spend"] = str(s.minimum_spend)
    return out


async def release_expired_holds(db: AsyncSession, now: dt.datetime | None = None) -> int:
    """Reject pending bookings past ``hold_expires_at`` and free their slots. Returns count.

    Called opportunistically before every reservation and by the periodic sweep (``app.services.sweep``).
    """
    now = now or dt.datetime.now(dt.UTC)
    # Look before writing: an UPDATE that matches nothing would still count as a write to the bookings table
    # (and drop the tenant's cached availability), and this runs on every sweep tick and every reservation.
    if not await db.scalar(select(Booking.id).where(Booking.status == "pending", Booking.hold_expires_at <= now).limit(1)):
        return 0
    expired = (await db.execute(
        update(Booking)
        .where(Booking.status == "pending", Booking.hold_expires_at <= now)
        .values(status="rejected")
        .returning(Booking.id, Booking.tenant_id, Booking.start_at, Booking.end_at)
    )).all()
    ids = [row[0] for row in expired]
    if ids:
        events.record_expired(db, [(r[0], r[1], r[2], r[3]) for r in expired])
        await db.execute(
            update(BookingAllocation)
            .where(BookingAllocation.booking_id.in_(ids), BookingAllocation.state == "held")
            .values(state="released")
        )
    return len(ids)


async def _upsert_customer(db: AsyncSession, tenant: Tenant, data: BookingCreate) -> uuid.UUID:
    """The customer behind a phone number, created on first sight.

    A public booking proves nothing about who holds the phone, so it may only *add* to a customer who already exists: the name follows the latest booking (people
    correct typos), but an email or gender that is already on the record is never replaced - that is how somebody could attach their own email to another person's record and
    sign in as them. The email given with this booking is kept on the booking (``contact_email``); the customer changes their own by confirming a new one.
    """
    ensure_gender_allowed(tenant, data.customer.gender)
    insert_stmt = pg_insert(Customer).values(
        tenant_id=tenant.id, name=data.customer.name, phone=normalise_phone(data.customer.phone, tenant.phone_country_code),
        email=data.customer.email, gender=data.customer.gender,
    )
    stmt = insert_stmt.on_conflict_do_update(
        constraint="uq_customers_tenant_phone",
        set_={
            "name": insert_stmt.excluded.name, "email": func.coalesce(Customer.email, insert_stmt.excluded.email),  # (what is on the record stays)
            "gender": func.coalesce(Customer.gender, insert_stmt.excluded.gender),  # (the customer changes it themselves: PATCH /public/me/profile)
        },
    ).returning(Customer.id)
    return (await db.execute(stmt)).scalar_one()


async def _has_overnight_window(db: AsyncSession, day: dt.date) -> bool:
    """Whether opening hours or a shift that starts on ``day`` run past midnight."""
    wd = weekday_index(day)
    hours = select(OperatingHours.id).where(OperatingHours.weekday == wd, OperatingHours.close_time < OperatingHours.open_time)
    shifts = select(ResourceShift.id).where(ResourceShift.weekday == wd, ResourceShift.end_time < ResourceShift.start_time)
    return bool(await db.scalar(select(exists(hours) | exists(shifts))))


async def _allocate_on_business_day(
    db: AsyncSession, allocator: BaseAllocator, req: AvailabilityRequest, start_at: dt.datetime
) -> list[Assignment]:
    """Allocate for ``req.day``; if that day does not offer the start, try the day before.

    With overnight hours a start such as 01:00 belongs to the business day that began the evening before (Business Day
    Rule). The request only carries the start instant, so the calendar date of that instant is tried first and, when it
    is not offered there, yesterday's business day. ``req.day`` is left on the day that worked, so retries after a lost
    race stay on it. When neither offers the start, the error (with the slots of the requested date) is the first one.
    """
    try:
        return await allocator.allocate(db, req, start_at)
    except SlotUnavailable as first:
        yesterday = req.day - dt.timedelta(days=1)
        if req.day != start_at.astimezone(req.tz).date():  # already on an earlier day (a retry): nothing left to try
            raise
        if not await _has_overnight_window(db, yesterday):  # nothing of yesterday's reaches past midnight: same answer as before
            raise
        previous = dataclasses.replace(req, day=yesterday)
        try:
            assignments = await allocator.allocate(db, previous, start_at)
        except SlotUnavailable:
            raise first from None
        req.day = yesterday
        return assignments


async def reserve_with_retry(
    db: AsyncSession,
    allocator: BaseAllocator,
    req: AvailabilityRequest,
    start_at: dt.datetime,
    *,
    attempts: int,
    build: Callable[[list[Assignment]], list[Any]],
) -> tuple[list[Assignment], list[Any]]:
    """Decide resources, then insert what ``build`` makes from them, inside a SAVEPOINT.

    If the exclusion constraint says another request won the race for a resource we picked, the
    savepoint is rolled back and the decision is re-made against fresh state (up to ``attempts``
    times): "Any Available"-style requests then find the resource that is still free, or the
    allocator raises ``SlotUnavailable`` when none is left. After the last attempt the loser gets
    :class:`SlotConflict` with refreshed slots.
    """
    for attempt in range(1, attempts + 1):
        assignments = await _allocate_on_business_day(db, allocator, req, start_at)
        rows = build(assignments)
        try:
            async with db.begin_nested():  # SAVEPOINT: a lost race must not poison the outer transaction
                db.add_all(rows)
                await db.flush()
            return assignments, rows
        except DBAPIError as exc:
            if not is_lost_race(exc):
                raise
            if attempt < attempts:
                continue
            fresh = await allocator.find_slots(db, req)
            raise SlotConflict(
                "Slot just taken, please pick another time", extra={"slots": [slot_to_json(s) for s in fresh]}
            ) from exc
    raise AssertionError("unreachable")  # pragma: no cover


def ensure_minimum_spend_accepted(minimum_spend: Decimal, accept: bool, accepted: Decimal | None) -> None:
    """Refuse (422 ``minimum_spend_not_accepted``, with the amount needed) tables whose minimum spend the customer has not agreed to.

    ``accepted`` is the amount the client showed and the customer accepted: anything up to it is fine. The tables are decided when the request
    arrives, not when the slot was shown, so another booking in between can leave only a dearer room; that is refused, never booked at an amount the
    customer never saw. Without ``accepted`` the older yes/no ``accept`` stands for any amount (kept for clients that do not send the number).
    """
    if minimum_spend <= 0 or (minimum_spend <= accepted if accepted is not None else accept):
        return
    if accepted is None:
        raise MinimumSpendNotAccepted("This table has a minimum spend that must be accepted", extra={"minimum_spend": str(minimum_spend)})
    raise MinimumSpendNotAccepted(
        f"The tables free now need a minimum spend of {minimum_spend}, more than the {accepted} accepted; please confirm the new amount",
        extra={"minimum_spend": str(minimum_spend), "accepted_minimum_spend": str(accepted)},
    )


#: What an ``Idempotency-Key`` may look like: 8-100 letters, digits and ``_ . : -`` (a UUID is the usual choice).
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9_.:\-]{8,100}")


def clean_idempotency_key(raw: str) -> str:
    if not IDEMPOTENCY_KEY.fullmatch(raw):
        raise ValidationFailed("Idempotency-Key must be 8-100 characters: letters, digits and _ . : - (a UUID will do)")
    return raw


def request_fingerprint(data: BookingCreate) -> str:
    """Identifies "the same request" under one ``Idempotency-Key``: a hash of the whole body, so key order and spacing in the JSON do not matter."""
    return hashlib.sha256(json.dumps(data.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def create_booking_once(
    db: AsyncSession, tenant: Tenant, data: BookingCreate, key: str, now: dt.datetime | None = None
) -> tuple[Booking, bool]:
    """``create_booking`` under an ``Idempotency-Key``: ``(booking, replayed)``.

    The first request with a key makes the booking and stamps it with the key and a hash of the request. The same request sent again (a double click, a retry after a time-out)
    gets that booking back and makes nothing new; the same key with a *different* request is refused (409 ``idempotency_key_reused``). Two copies arriving at the same moment
    queue on a lock for the key, so the second finds the first's booking. A request that failed (slot taken, outside the window) made nothing, so its key is free to try again.
    """
    fingerprint = request_fingerprint(data)
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"idempotency:{tenant.id}:{key}"})  # held to the end of the transaction
    seen = await db.scalar(select(Booking).where(Booking.idempotency_key == key))
    if seen is not None:
        if seen.idempotency_hash != fingerprint:
            raise IdempotencyKeyReused("This Idempotency-Key was already used for a different booking request; use a new key for a new request")
        return seen, True
    return await create_booking(db, tenant, data, now, idempotency=(key, fingerprint)), False


async def create_booking(
    db: AsyncSession, tenant: Tenant, data: BookingCreate, now: dt.datetime | None = None, idempotency: tuple[str, str] | None = None
) -> Booking:
    """Atomically reserve a slot and create the booking.

    Raises ``OutsideBookingWindow`` / ``SlotUnavailable`` / ``SlotConflict``. The caller's
    transaction should be rolled back on any raised error (the FastAPI dependency does this).
    """
    now = now or dt.datetime.now(dt.UTC)
    await release_expired_holds(db, now)  # (a lapsed hold gives its place back before the plan's limit is counted)
    await plans.ensure_can_book(db, tenant, now)

    allocator = get_allocator(tenant.business_type)
    services = await load_services(db, data.service_ids) if allocator.requires_services else []
    policy = await allocator.resolve_policy(db, tenant, services)
    validate_booking_window(policy, data.start_at, now)

    req = AvailabilityRequest(
        tenant=tenant, services=services, policy=policy,
        day=data.start_at.astimezone(_tz(tenant)).date(), resource_id=data.resource_id, now=now,
        party_size=data.party_size, table_type=data.table_type, allow_separate_seating=data.allow_separate_seating,
    )
    customer_id = await _upsert_customer(db, tenant, data)
    restaurant = not allocator.requires_services
    pending = policy.default_status == "pending"
    contact_email = normalise_email(str(data.customer.email)) if data.customer.email else None

    vehicle_id: uuid.UUID | None = None
    lead_ack_at: dt.datetime | None = None
    if allocator.uses_vehicles:
        vehicle_id = await resolve_vehicle(db, tenant, customer_id, data)
        parts = await lead_time_notice(db, services)
        if parts:
            if not data.acknowledge_lead_time:
                raise LeadTimeNotAcknowledged(
                    "Some parts must be ordered; please acknowledge the estimated lead time", extra={"parts": parts}
                )
            lead_ack_at = now
    elif data.vehicle_id is not None or data.vehicle is not None:
        raise ValidationFailed("Vehicles only apply to automotive bookings")

    address_id: uuid.UUID | None = None
    if allocator.uses_locations and any(s.location_type == "on_site" for s in services):
        address_id, req.latitude, req.longitude = await resolve_address(db, tenant, customer_id, data)
    elif data.address_id is not None or data.address is not None:
        raise ValidationFailed("An address applies only to on-site services")

    def build(assignments: list[Assignment]) -> list[Any]:
        # VIP tables carry a minimum spend the customer must have agreed to before submitting.
        minimum_spend = sum((a.minimum_spend for a in assignments), Decimal(0))
        ensure_minimum_spend_accepted(minimum_spend, data.accept_minimum_spend, data.accepted_minimum_spend)
        return [Booking(
            tenant_id=tenant.id, customer_id=customer_id, contact_email=contact_email, status=policy.default_status,
            idempotency_key=idempotency[0] if idempotency else None, idempotency_hash=idempotency[1] if idempotency else None,
            # the customer-visible window excludes any travel time reserved around it
            start_at=min(a.service_start for a in assignments), end_at=max(a.service_end for a in assignments),
            policy_snapshot=policy_snapshot(policy), vehicle_id=vehicle_id, lead_time_acknowledged_at=lead_ack_at,
            address_id=address_id, party_size=data.party_size, special_request=data.special_request, source=data.source,
            table_type=data.table_type if restaurant else None,
            allow_separate_seating=data.allow_separate_seating if restaurant else None,
            minimum_spend_agreed=minimum_spend if minimum_spend > 0 else None,
            hold_expires_at=(now + policy.pending_timeout) if pending else None,
            health_notes_enc=encrypt_field(data.health_notes, tenant.id) if data.health_notes else None,
            items=[
                BookingItem(tenant_id=tenant.id, service_id=s.id, duration_minutes=s.duration_minutes, buffer_minutes=s.buffer_minutes, seq=n)
                for n, s in enumerate(services)
            ],
            allocations=[
                BookingAllocation(
                    tenant_id=tenant.id, resource_id=a.resource_id,
                    service_id=a.service_id, period=Range(a.start, a.end, bounds="[)"), state="held" if pending else "active",
                )
                for a in assignments
            ],
        )]

    # A request for a named resource that is the *only* thing to choose gets one attempt: if it is
    # taken the answer really is "taken". Otherwise a lost race is re-decided (see reserve_with_retry).
    attempts = 1 if data.resource_id and not allocator.retry_when_resource_named else ANY_AVAILABLE_ATTEMPTS
    _, rows = await reserve_with_retry(db, allocator, req, data.start_at, attempts=attempts, build=build)
    booking: Booking = rows[0]
    events.record_created(db, booking, events.Actor("customer", str(customer_id)))
    await ensure_meeting_link(db, booking, tenant)  # no-op unless a confirmed virtual appointment
    return booking


def _tz(tenant: Tenant) -> dt.tzinfo:
    from zoneinfo import ZoneInfo

    return ZoneInfo(tenant.timezone)


async def get_booking(db: AsyncSession, booking_id: uuid.UUID, *, lock: bool = False) -> Booking:
    """Load a booking; ``lock=True`` takes ``SELECT ... FOR UPDATE`` so concurrent changes queue up.

    ``populate_existing`` matters: without it ``Session.get`` would hand back a copy already sitting in
    the session (say, from an ownership check earlier in the request) without touching the database, so the
    lock would never be taken and the status could be stale.
    """
    booking = await db.get(Booking, booking_id, with_for_update=lock, populate_existing=lock)
    if booking is None:
        raise NotFound("Booking not found")
    return booking


def ensure_status_time(booking: Booking, new_status: str, tenant: Tenant, now: dt.datetime) -> None:
    """Refuse to mark a booking as started, finished or missed before its time has come.

    ``no-show`` needs the start time to have passed. ``in-progress`` (the customer arrived) and ``completed`` open ``STATUS_EARLY_MINUTES`` (60) before the start, because people arrive
    early; a booking that is already ``in-progress`` can be completed at any time. Before this a booking next week could be completed (which sent "Thank you for visiting us" with
    the review link) or marked a no-show, and either freed its slot. Cancelling and rejecting are not timed: they are how a future booking ends.
    """
    if new_status == "no-show":
        opens, what = booking.start_at, "marked as a no-show"
    elif new_status in ("in-progress", "completed") and booking.status != "in-progress":
        opens, what = booking.start_at - dt.timedelta(minutes=get_settings().status_early_minutes), f"marked {new_status}"
    else:
        return
    if now < opens:
        starts = booking.start_at.astimezone(_tz(tenant)).strftime("%a %d %b %Y %H:%M")
        raise InvalidTransition(f"This booking cannot be {what} yet: it starts at {starts}" + ("" if new_status == "no-show" else f" (from {opens.astimezone(_tz(tenant)).strftime('%H:%M %a %d %b')})"))


async def transition_booking(
    db: AsyncSession,
    tenant: Tenant,
    booking_id: uuid.UUID,
    new_status: str,
    *,
    actor: str = "staff",
    actor_id: str | None = None,
    now: dt.datetime | None = None,
) -> Booking:
    """Move a booking through the state machine, updating its allocations accordingly.

    Every move is recorded in the booking's status history and as an outbox event (``actor``/``actor_id`` say who).

    ``actor="customer"`` may only cancel a ``pending`` or ``confirmed`` booking, and a confirmed one only
    while the snapshotted ``min_notice_cancel`` has not passed. The booking row is locked so two
    transitions racing each other (say an approval and a customer cancellation) run one after the other
    instead of leaving a status that disagrees with its allocations.
    """
    now = now or dt.datetime.now(dt.UTC)
    await release_expired_holds(db, now)
    booking = await get_booking(db, booking_id, lock=True)

    if booking.status == "waiting-for-parts" and new_status == "confirmed":
        raise InvalidTransition("A job waiting for parts is rescheduled with POST /admin/bookings/{id}/resume")
    if new_status not in TRANSITIONS.get(booking.status, frozenset()):
        raise InvalidTransition(f"Cannot move booking from '{booking.status}' to '{new_status}'")
    if new_status == "in-progress" and tenant.business_type not in IN_PROGRESS_VERTICALS:
        raise InvalidTransition(f"'in-progress' is not used by {tenant.business_type} bookings")
    if new_status == "waiting-for-parts" and tenant.business_type != "automotive":
        raise InvalidTransition("'waiting-for-parts' is only used by automotive job orders")
    if actor == "customer":
        if new_status != "cancelled":
            raise InvalidTransition("Customers can only cancel")
        if booking.status not in CUSTOMER_CANCELLABLE:
            raise InvalidTransition(
                f"A booking that is '{booking.status}' can no longer be cancelled online; please contact the business"
            )
        deadline = cancel_deadline(booking)  # only confirmed bookings have one: withdrawing a pending request is free
        if deadline is not None and now > deadline:
            raise TooLateToCancel(
                "Too late to cancel under this booking's cancellation policy", extra={"cancel_deadline": deadline.isoformat()}
            )

    ensure_status_time(booking, new_status, tenant, now)
    previous = booking.status
    if new_status == "confirmed":
        await db.execute(
            update(BookingAllocation)
            .where(BookingAllocation.booking_id == booking.id, BookingAllocation.state == "held")
            .values(state="active")
        )
        booking.hold_expires_at = None
    elif new_status == "waiting-for-parts":
        await truncate_allocations(db, booking, now)
    elif new_status in RELEASING:
        await db.execute(
            update(BookingAllocation)
            .where(BookingAllocation.booking_id == booking.id, BookingAllocation.state.in_(("held", "active")))
            .values(state="released")
        )
        booking.hold_expires_at = None
    booking.status = new_status
    if new_status not in LIVE_STATUSES:
        booking.flags = []  # review flags only matter while the booking still holds a future slot
    events.record_status_change(db, booking, previous, events.Actor(actor, actor_id))
    await db.flush()
    if new_status == "completed" and tenant.business_type == "salon":
        await finalise_completion(db, booking)  # deduct retail stock, lock in the final duration
    elif new_status == "completed" and tenant.business_type in PARTS_BUSINESS_TYPES:
        await deduct_stock(db, booking)  # the parts on the job order leave the shelf (the duration is reported separately, not worked out)
    if new_status == "confirmed":
        await ensure_meeting_link(db, booking, tenant)
    await db.refresh(booking, attribute_names=["allocations", "updated_at"])
    return booking


async def list_bookings(
    db: AsyncSession,
    *,
    day: dt.date | None = None,
    tz: dt.tzinfo | None = None,
    status: str | None = None,
    resource_id: uuid.UUID | None = None,
    table_type: str | None = None,
    schedule_conflict: bool | None = None,
    flagged: bool | None = None,
    flag: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Booking]:
    q = select(Booking).order_by(Booking.start_at).limit(limit).offset(offset)
    if day:
        lo = dt.datetime.combine(day, dt.time.min, tzinfo=tz)
        q = q.where(Booking.start_at >= lo, Booking.start_at < lo + dt.timedelta(days=1))
    if status:
        q = q.where(Booking.status == status)
    if table_type:
        q = q.where(Booking.table_type == table_type)
    if schedule_conflict is not None:
        q = q.where(Booking.schedule_conflict.is_(schedule_conflict))
    if flagged is not None:
        q = q.where(func.cardinality(Booking.flags) > 0 if flagged else func.cardinality(Booking.flags) == 0)
    if flag:
        q = q.where(Booking.flags.contains([flag]))
    if resource_id:
        q = q.where(Booking.id.in_(select(BookingAllocation.booking_id).where(BookingAllocation.resource_id == resource_id)))
    return list((await db.scalars(q)).unique().all())


async def truncate_allocations(db: AsyncSession, booking: Booking, now: dt.datetime) -> None:
    """Free the mechanic and bay from ``now`` on (blueprint §10.3, waiting-for-parts).

    Work already done stays on record: a period spanning ``now`` is cut to end at ``now``; periods
    entirely in the past are untouched; periods not yet started are released.
    """
    for alloc in booking.allocations:
        if alloc.state not in ("held", "active"):
            continue
        lower, upper = alloc.period.lower, alloc.period.upper
        if lower is None or upper is None or upper <= now:
            continue
        if lower >= now:
            alloc.state = "released"
        else:
            alloc.period = Range(lower, now, bounds="[)")
    await db.flush()


async def resume_booking(
    db: AsyncSession,
    tenant: Tenant,
    booking_id: uuid.UUID,
    start_at: dt.datetime,
    *,
    resource_id: uuid.UUID | None = None,
    actor_id: str | None = None,
    service_ids: list[uuid.UUID] | None = None,
    now: dt.datetime | None = None,
) -> Booking:
    """Reschedule a ``waiting-for-parts`` job: new mechanic/bay allocations, status back to ``confirmed``.

    Only the remaining services are scheduled (default: all services on the job order). This is an
    admin action, so minimum-notice and booking-window rules are skipped, but ``start_at`` must lie
    in the future. The original ``start_at`` is kept; ``end_at`` moves to the new end.
    """
    now = now or dt.datetime.now(dt.UTC)
    await release_expired_holds(db, now)
    booking = await get_booking(db, booking_id, lock=True)
    if booking.status != "waiting-for-parts":
        raise InvalidTransition(f"Only a job waiting for parts can be resumed (status is '{booking.status}')")
    if start_at <= now:
        raise ValidationFailed("start_at must be in the future")

    on_order = {item.service_id for item in booking.items}
    wanted = list(dict.fromkeys(service_ids)) if service_ids else [i.service_id for i in booking.items]
    if not set(wanted) <= on_order:
        raise ValidationFailed("service_ids must be services on this job order")

    allocator = get_allocator(tenant.business_type)
    services = await load_services(db, wanted)
    policy = await allocator.resolve_policy(db, tenant, services)
    req = AvailabilityRequest(
        tenant=tenant, services=services, policy=policy, day=start_at.astimezone(_tz(tenant)).date(),
        resource_id=resource_id, now=now, ignore_notice=True,
    )
    def build(assignments: list[Assignment]) -> list[Any]:
        return [
            BookingAllocation(
                tenant_id=tenant.id, booking_id=booking.id, resource_id=a.resource_id,
                service_id=a.service_id, period=Range(a.start, a.end, bounds="[)"), state="active",
            )
            for a in assignments
        ]

    assignments, _ = await reserve_with_retry(
        db, allocator, req, start_at, attempts=ANY_AVAILABLE_ATTEMPTS, build=build
    )
    booking.status = "confirmed"
    events.record_status_change(db, booking, "waiting-for-parts", events.Actor("staff", actor_id))
    booking.flags_checked_at = None  # new allocations: the periodic sweep re-checks the review flags once more
    booking.end_at = max(a.service_end for a in assignments)
    await db.flush()
    await db.refresh(booking, attribute_names=["allocations", "updated_at"])
    return booking


async def _clashes(
    db: AsyncSession, booking_id: uuid.UUID, live: list[tuple[uuid.UUID, uuid.UUID, dt.datetime, dt.datetime]], delta: dt.timedelta
) -> list[uuid.UUID]:
    """Other bookings holding one of these resources in the ``delta`` right after the current period."""
    found: set[uuid.UUID] = set()
    for _, resource_id, _, upper in live:
        rows = await db.scalars(
            select(BookingAllocation.booking_id).where(
                BookingAllocation.resource_id == resource_id,
                BookingAllocation.booking_id != booking_id,
                BookingAllocation.state.in_(("held", "active")),
                BookingAllocation.period.overlaps(Range(upper, upper + delta, bounds="[)")),
            )
        )
        found.update(rows.all())
    return sorted(found)


async def try_extend(db: AsyncSession, booking: Booking, delta: dt.timedelta) -> tuple[bool, list[uuid.UUID]]:
    """Stretch every live allocation of ``booking`` by ``delta`` if nothing else is booked in that time.

    Returns ``(extended, clashing booking ids)``. Nothing is ever forced: if another appointment sits in
    the extra time the reservation is left as it is and the caller decides what to flag. The update runs
    in a SAVEPOINT because a booking can slip into the gap between our check and our write; the
    exclusion constraint then refuses the extension and we report that booking as the clash instead of
    failing the request. A stretched booking is handed back to the periodic sweep (``flags_checked_at``): today only in-progress
    appointments are stretched, which the review flags never examine, but this keeps the guarantee if that ever changes.
    """
    live = [
        (a.id, a.resource_id, a.period.lower, a.period.upper)
        for a in booking.allocations
        if a.state in ("held", "active") and a.period.lower is not None and a.period.upper is not None
    ]
    if not live:
        return False, []
    clashes = await _clashes(db, booking.id, live, delta)
    if clashes:
        return False, clashes
    try:
        async with db.begin_nested():
            for alloc in booking.allocations:
                for alloc_id, _, lower, upper in live:
                    if alloc.id == alloc_id:
                        alloc.period = Range(lower, upper + delta, bounds="[)")
            await db.flush()
    except DBAPIError as exc:
        if not is_exclusion_violation(exc):  # a deadlock victim cannot be told apart from "no clash": fail the request instead
            raise
        clashes = await _clashes(db, booking.id, live, delta)
        await db.refresh(booking, attribute_names=["allocations"])
        return False, clashes
    booking.flags_checked_at = None  # the reservation changed: whatever the sweep concluded about it before no longer describes it
    return True, []


def visible_to_customer(booking: Booking, customer_id: uuid.UUID, email_scope: str | None) -> bool:
    """Whether a signed-in customer may see and act on ``booking``: it is theirs and, for a session limited to one email address, was made with that address."""
    return booking.customer_id == customer_id and (email_scope is None or (booking.contact_email or "").lower() == email_scope)


def cancel_deadline(booking: Booking) -> dt.datetime | None:
    """Last moment a customer may cancel a *confirmed* booking (start minus the snapshotted notice)."""
    if booking.status != "confirmed":
        return None
    return booking.start_at - dt.timedelta(seconds=booking.policy_snapshot["min_notice_cancel_s"])


def can_cancel_online(booking: Booking, now: dt.datetime) -> bool:
    """Whether the customer could cancel right now (the same rule ``transition_booking`` enforces)."""
    if booking.status == "pending":
        return True
    deadline = cancel_deadline(booking)
    return deadline is not None and now <= deadline


def can_reschedule_online(booking: Booking, now: dt.datetime) -> bool:
    """Whether the customer could move the booking right now: the same rule as cancelling it (a move is a cancel and a rebook)."""
    return can_cancel_online(booking, now)


def policy_from_snapshot(snapshot: dict[str, Any]) -> BookingPolicy:
    """A transient (never stored) policy carrying the rules a booking was made under."""
    return BookingPolicy(
        name=snapshot["name"], default_status=snapshot["default_status"],
        min_notice_booking=dt.timedelta(seconds=snapshot["min_notice_booking_s"]),
        min_notice_cancel=dt.timedelta(seconds=snapshot["min_notice_cancel_s"]),
        booking_window=dt.timedelta(seconds=snapshot["booking_window_s"]),
        pending_timeout=dt.timedelta(seconds=snapshot["pending_timeout_s"]),
    )


def _as_booked(service: Service, item: BookingItem) -> Service:
    """A transient copy of ``service`` with the duration and buffer it was booked with (a later edit must not change a booking)."""
    clone = Service(**{attr.key: getattr(service, attr.key) for attr in Service.__mapper__.column_attrs})
    clone.duration_minutes, clone.buffer_minutes = item.duration_minutes, item.buffer_minutes
    return clone


async def _previous_staff(db: AsyncSession, booking: Booking) -> uuid.UUID | None:
    """The staff member of the booking's first live segment (workshops: the mechanic, not the bay)."""
    live = sorted((a for a in booking.allocations if a.state in ("held", "active")), key=lambda a: a.period.lower or dt.datetime.max.replace(tzinfo=dt.UTC))
    if not live:
        return None
    staff = set(await db.scalars(select(Resource.id).where(Resource.id.in_([a.resource_id for a in live]), Resource.type == "staff")))
    return next((a.resource_id for a in live if a.resource_id in staff), None)


async def reschedule_as_customer(
    db: AsyncSession,
    tenant: Tenant,
    booking_id: uuid.UUID,
    customer_id: uuid.UUID,
    start_at: dt.datetime,
    *,
    resource_id: uuid.UUID | None = None,
    any_available: bool = False,
    accept_minimum_spend: bool = False,
    accepted_minimum_spend: Decimal | None = None,
    now: dt.datetime | None = None,
    email_scope: str | None = None,
) -> Booking:
    """Move a customer's own booking to ``start_at`` (same services, same status), atomically.

    * Allowed for ``pending`` and ``confirmed`` bookings, a confirmed one only until the cancellation notice of *its own*
      policy snapshot runs out (``too_late_to_reschedule``); someone else's booking is a plain 404.
    * The new time is judged by the same snapshot (minimum notice, booking window) and by the allocator of the vertical
      exactly as a new booking would be, with the booking's own current slot freed first - so a move that overlaps the old
      time works, and a refused move changes nothing (the whole request is rolled back).
    * Services are kept with the duration they were booked with. Staff-based bookings stay with the same staff member
      unless ``resource_id`` names another or ``any_available`` asks for the least-loaded one; restaurants get a fresh table
      choice (VIP minimum spend must be accepted again if it differs). A pending booking keeps its original hold deadline.
    * The old allocations are released, new ones written, review flags reset for the sweep to re-check, and a
      ``booking.rescheduled`` outbox event is recorded.
    """
    now = now or dt.datetime.now(dt.UTC)
    await release_expired_holds(db, now)
    if not visible_to_customer(await get_booking(db, booking_id), customer_id, email_scope):
        raise NotFound("Booking not found")
    booking = await get_booking(db, booking_id, lock=True)  # a racing cancel / approval / second move runs before or after, never during
    if booking.status not in CUSTOMER_CANCELLABLE:
        raise InvalidTransition(f"A booking that is '{booking.status}' can no longer be rescheduled online; please contact the business")
    deadline = cancel_deadline(booking)
    if deadline is not None and now > deadline:
        raise TooLateToReschedule(
            "Too late to reschedule under this booking's cancellation policy", extra={"reschedule_deadline": deadline.isoformat()}
        )
    return await _move_booking(
        db, tenant, booking, start_at, resource_id=resource_id, any_available=any_available,
        accept_minimum_spend=accept_minimum_spend, accepted_minimum_spend=accepted_minimum_spend, now=now, actor=events.Actor("customer", str(customer_id)), admin=False,
    )


async def reschedule_as_admin(
    db: AsyncSession,
    tenant: Tenant,
    booking_id: uuid.UUID,
    start_at: dt.datetime,
    *,
    actor_id: str | None = None,
    resource_id: uuid.UUID | None = None,
    any_available: bool = False,
    accept_minimum_spend: bool = False,
    accepted_minimum_spend: Decimal | None = None,
    now: dt.datetime | None = None,
) -> Booking:
    """Move any ``pending`` or ``confirmed`` booking of the business to ``start_at`` (a manager's or owner's decision).

    The same move as the customer's (same services, same status, atomic, a refused move changes nothing), without the
    customer's restrictions: no ownership check, no cancellation-notice deadline, and the new time is judged by the
    allocator only - the policy's minimum notice and booking window are skipped (like ``resume_booking``), but
    ``start_at`` must lie in the future. The event records the actor as ``staff``; the admin audit log records the call.
    A job ``waiting-for-parts`` is moved with ``resume_booking`` instead.
    """
    now = now or dt.datetime.now(dt.UTC)
    await release_expired_holds(db, now)
    booking = await get_booking(db, booking_id, lock=True)
    if booking.status == "waiting-for-parts":
        raise InvalidTransition("A job waiting for parts is rescheduled with POST /admin/bookings/{id}/resume")
    if booking.status not in CUSTOMER_CANCELLABLE:
        raise InvalidTransition(f"A booking that is '{booking.status}' cannot be rescheduled; only pending or confirmed ones can")
    if start_at <= now:
        raise ValidationFailed("start_at must be in the future")
    return await _move_booking(
        db, tenant, booking, start_at, resource_id=resource_id, any_available=any_available,
        accept_minimum_spend=accept_minimum_spend, accepted_minimum_spend=accepted_minimum_spend, now=now, actor=events.Actor("staff", actor_id), admin=True,
    )


async def _move_booking(
    db: AsyncSession,
    tenant: Tenant,
    booking: Booking,
    start_at: dt.datetime,
    *,
    resource_id: uuid.UUID | None,
    any_available: bool,
    accept_minimum_spend: bool,
    accepted_minimum_spend: Decimal | None,
    now: dt.datetime,
    actor: events.Actor,
    admin: bool,
) -> Booking:
    """The move itself, after the caller has locked the booking and checked who may do it and when (see the two entry points).

    ``admin`` skips the policy's minimum notice and booking window for the new time; everything else is identical.
    """
    if start_at == booking.start_at:
        raise ValidationFailed("The booking is already at that time")
    if resource_id is not None and any_available:
        raise ValidationFailed("Give either resource_id or any_available, not both")

    policy = policy_from_snapshot(booking.policy_snapshot)
    if not admin:
        validate_booking_window(policy, start_at, now)
    allocator = get_allocator(tenant.business_type)
    items = list((await db.scalars(select(BookingItem).where(BookingItem.booking_id == booking.id))).all())
    items.sort(key=lambda i: (i.seq, str(i.id)))
    by_id = {s.id: s for s in (await db.scalars(select(Service).where(Service.id.in_([i.service_id for i in items])))).all()}
    services = [_as_booked(by_id[i.service_id], i) for i in items]
    req = AvailabilityRequest(
        tenant=tenant, services=services, policy=policy, day=start_at.astimezone(_tz(tenant)).date(), resource_id=resource_id, now=now,
        party_size=booking.party_size, table_type=booking.table_type or "normal",
        allow_separate_seating=bool(booking.allow_separate_seating), ignore_notice=admin,
    )
    if allocator.uses_locations and any(s.location_type == "on_site" for s in services):
        address = await db.get(CustomerAddress, booking.address_id) if booking.address_id else None
        if address is None or address.latitude is None or address.longitude is None:
            raise ValidationFailed("This booking has no mapped address to visit")
        req.latitude, req.longitude = address.latitude, address.longitude
    if resource_id is None and allocator.requires_services and not any_available:
        req.resource_id = await _previous_staff(db, booking)

    previous_start, previous_end = booking.start_at, booking.end_at
    pending = booking.status == "pending"
    await db.execute(
        update(BookingAllocation)
        .where(BookingAllocation.booking_id == booking.id, BookingAllocation.state.in_(("held", "active")))
        .values(state="released")
    )
    await db.flush()

    def build(assignments: list[Assignment]) -> list[Any]:
        minimum_spend = sum((a.minimum_spend for a in assignments), Decimal(0))
        if minimum_spend != (booking.minimum_spend_agreed or 0):  # the amount already agreed for this booking needs no new consent
            ensure_minimum_spend_accepted(minimum_spend, accept_minimum_spend, accepted_minimum_spend)
        return [
            BookingAllocation(
                tenant_id=tenant.id, booking_id=booking.id, resource_id=a.resource_id,
                service_id=a.service_id, period=Range(a.start, a.end, bounds="[)"), state="held" if pending else "active",
            )
            for a in assignments
        ]

    attempts = 1 if req.resource_id and not allocator.retry_when_resource_named else ANY_AVAILABLE_ATTEMPTS
    assignments, _ = await reserve_with_retry(db, allocator, req, start_at, attempts=attempts, build=build)
    minimum_spend = sum((a.minimum_spend for a in assignments), Decimal(0))
    booking.start_at = min(a.service_start for a in assignments)
    booking.end_at = max(a.service_end for a in assignments)
    if not allocator.requires_services:
        booking.minimum_spend_agreed = minimum_spend if minimum_spend > 0 else None
    booking.flags = []  # judged against the old slot; the sweep checks the new one
    booking.flags_checked_at = None
    await reminders.forget(db, booking.id)  # the new time gets its own reminders
    events.record_rescheduled(db, booking, previous_start, previous_end, actor)
    await db.flush()
    await db.refresh(booking, attribute_names=["allocations", "updated_at"])
    return booking


async def cancel_as_customer(
    db: AsyncSession, tenant: Tenant, booking_id: uuid.UUID, customer_id: uuid.UUID, now: dt.datetime | None = None, email_scope: str | None = None
) -> Booking:
    """Customer cancellation (blueprint §14). Someone else's booking is reported as not found."""
    booking = await get_booking(db, booking_id)
    if not visible_to_customer(booking, customer_id, email_scope):
        raise NotFound("Booking not found")
    return await transition_booking(db, tenant, booking_id, "cancelled", actor="customer", actor_id=str(customer_id), now=now)
