"""Availability engine: turns shifts, days off and existing allocations into bookable slots.

Pipeline (see blueprint §13.1):

1. tenant operating hours + days off for the target (tenant-local) date
2. qualified active resources and their shift windows, minus breaks
3. subtract periods already ``held``/``active`` in ``booking_allocations``
4. enforce policy ``min_notice_booking`` / ``booking_window``
5. emit discrete start times sized for ``duration + buffer``

Steps 2-5's interval maths are pure functions (:func:`subtract_intervals`,
:func:`generate_slots`) so they can be unit-tested without a database.
"""
from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound, ValidationFailed
from app.models import (
    Booking,
    BookingAllocation,
    BookingPolicy,
    DayOff,
    OperatingHours,
    Resource,
    ResourceShift,
    Service,
    ServiceResource,
    Tenant,
)


@dataclass(frozen=True, order=True)
class Interval:
    """Half-open ``[start, end)`` interval, held as UTC instants whatever zone the ends are given in.

    Two datetimes in the *same* time zone are compared and subtracted by their clock readings, Python ignoring the zone's offset: wrong across
    a daylight-saving change (a night in Sydney is 7 or 9 real hours twice a year, not 8). So all the interval maths is done in UTC; convert to
    the business's zone only to show a time.
    """

    start: dt.datetime
    end: dt.datetime

    def __post_init__(self) -> None:
        start, end = self.start.astimezone(dt.UTC), self.end.astimezone(dt.UTC)
        if end < start:
            raise ValueError("interval end before start")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass(frozen=True)
class SlotOption:
    """A bookable start time and the resources that can serve it."""

    start: dt.datetime
    end: dt.datetime
    resource_ids: tuple[uuid.UUID, ...]
    #: True when several tables are combined for one party (restaurant separate seating).
    combined: bool = False
    #: Restaurant VIP: total minimum spend the customer must accept.
    minimum_spend: Decimal | None = None


@dataclass
class AvailabilityRequest:
    """Everything the pipeline needs; built once per request."""

    tenant: Tenant
    services: list[Service]
    policy: BookingPolicy
    day: dt.date
    resource_id: uuid.UUID | None = None
    now: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    #: Restaurant inputs (ignored by staff-based verticals).
    party_size: int | None = None
    table_type: str = "normal"
    allow_separate_seating: bool = False
    #: Admin rescheduling (resuming after waiting-for-parts, moving a booking): skip min-notice / booking-window checks.
    ignore_notice: bool = False
    #: Field service: where the customer wants an on-site visit (checked against the service radius).
    latitude: float | None = None
    longitude: float | None = None
    #: Salon: qualification follows the base service only, although add-ons lengthen the block.
    qualifying_services: list[Service] | None = None

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tenant.timezone)

    @property
    def day_start(self) -> dt.datetime:
        """The moment the business day ``day`` begins (midnight on the clock), as a UTC instant."""
        return clock_on(self.day, dt.time.min, self.tz)

    @property
    def overnight_end(self) -> dt.datetime:
        """The end of the day after: a night shift that starts on ``day`` may run to then."""
        return clock_on(self.day + dt.timedelta(days=2), dt.time.min, self.tz)

    @property
    def block_minutes(self) -> int:
        """Minutes reserved on the resource: all durations plus all buffers."""
        return sum(s.duration_minutes + s.buffer_minutes for s in self.services)

    @property
    def duration_minutes(self) -> int:
        return sum(s.duration_minutes for s in self.services)


# --------------------------------------------------------------------------- pure interval maths
def subtract_intervals(windows: list[Interval], busy: list[Interval]) -> list[Interval]:
    """Return ``windows`` with every ``busy`` span cut out (results sorted, non-empty)."""
    result: list[Interval] = []
    busy_sorted = sorted(busy)
    for w in sorted(windows):
        cursor = w.start
        for b in busy_sorted:
            if b.end <= cursor or b.start >= w.end:
                continue
            if b.start > cursor:
                result.append(Interval(cursor, b.start))
            cursor = max(cursor, b.end)
            if cursor >= w.end:
                break
        if cursor < w.end:
            result.append(Interval(cursor, w.end))
    return result


def intersect_intervals(a: list[Interval], b: list[Interval]) -> list[Interval]:
    """Pairwise intersection of two interval lists."""
    out = [
        Interval(max(x.start, y.start), min(x.end, y.end))
        for x in a
        for y in b
        if max(x.start, y.start) < min(x.end, y.end)
    ]
    return sorted(out)


def generate_slots(
    free: list[Interval],
    block: dt.timedelta,
    step: dt.timedelta,
    anchor: dt.datetime,
    earliest: dt.datetime,
    latest: dt.datetime,
) -> list[Interval]:
    """Discrete ``block``-long slots inside ``free``.

    Starts are aligned to ``step`` counted from ``anchor`` (tenant-local midnight) so that
    every resource offers the same grid, and are limited to ``[earliest, latest]``. All in real
    elapsed time (see :class:`Interval`): a step is a step even across a clock change, so no start is
    offered in an hour the clocks skipped, and both passes of a repeated hour are.
    """
    slots: list[Interval] = []
    earliest = earliest.astimezone(dt.UTC)  # (a local ``earliest`` would be stepped by wall-clock arithmetic below)
    for f in free:
        first = max(f.start, earliest)
        offset = (first - anchor) % step
        t = first if offset == dt.timedelta(0) else first + (step - offset)
        while t + block <= f.end and t <= latest:
            slots.append(Interval(t, t + block))
            t += step
    return slots


def covers_date(off: DayOff, day: dt.date) -> bool:
    """Whether a day-off entry applies to ``day`` (handles ``repeat_yearly`` and year-end spans)."""
    if not off.repeat_yearly:
        return off.start_date <= day <= off.end_date
    length = off.end_date - off.start_date
    for year in (day.year, day.year - 1):
        try:
            start = off.start_date.replace(year=year)
        except ValueError:  # 29 Feb in a non-leap year
            start = off.start_date.replace(year=year, day=28)
        if start <= day <= start + length:
            return True
    return False


def is_overnight(start: dt.time, end: dt.time) -> bool:
    """Opening hours or a shift whose end is not after its start run past midnight into the next day."""
    return end <= start


def clock_on(day: dt.date, at: dt.time, tz: dt.tzinfo) -> dt.datetime:
    """The moment the clock in ``tz`` reads ``at`` on ``day``, as a UTC instant.

    The two odd cases of a clock change: a reading that happens twice (the hour the clocks go back) is its first time round, and one that is
    skipped (the hour they go forward) is the moment after the jump, so a shift that starts at 02:30 on that night starts at 03:30.
    """
    return dt.datetime.combine(day, at, tzinfo=tz).astimezone(dt.UTC)


def window_on(day: dt.date, start: dt.time, end: dt.time, tz: ZoneInfo) -> Interval:
    """One window belonging to business day ``day``: from ``start`` on ``day`` to ``end``, on the next day if overnight.

    Business Day Rule (blueprint §6): time after midnight belongs to the day the window *started* on, so a Friday
    18:00-02:00 window is Friday's, including its early hours of Saturday. The next day is the next *date*, not 24 hours on.
    """
    end_day = day + dt.timedelta(days=1) if is_overnight(start, end) else day
    return Interval(clock_on(day, start, tz), clock_on(end_day, end, tz))


def shift_windows(shift: ResourceShift, day: dt.date, tz: ZoneInfo) -> list[Interval]:
    """Working windows of one shift that starts on ``day`` (it may end the next day), with the break removed."""
    whole = window_on(day, shift.start_time, shift.end_time, tz)
    if shift.break_start is None or shift.break_end is None:
        return [whole]
    # a break time earlier than the shift's start on the clock is after midnight; likewise for its end
    lo_day = day + dt.timedelta(days=1) if shift.break_start < shift.start_time else day
    hi_day = lo_day + dt.timedelta(days=1) if shift.break_end <= shift.break_start else lo_day
    brk_lo, brk_hi = clock_on(lo_day, shift.break_start, tz), clock_on(hi_day, shift.break_end, tz)
    return [Interval(whole.start, brk_lo), Interval(brk_hi, whole.end)]


def weekday_index(day: dt.date) -> int:
    """Blueprint convention: 0 = Sunday .. 6 = Saturday."""
    return day.isoweekday() % 7


@dataclass(frozen=True)
class ResInfo:
    """Just enough of a resource to choose between candidates deterministically."""

    id: uuid.UUID
    name: str


def fits(free: Sequence[Interval], segment: Interval) -> bool:
    """Whether ``segment`` lies entirely inside one of the ``free`` intervals."""
    return any(f.start <= segment.start and segment.end <= f.end for f in free)


def choose_resource(
    candidates: Sequence[ResInfo], previous: uuid.UUID | None, load: Mapping[uuid.UUID, float]
) -> ResInfo:
    """Continuity first (same resource as the previous segment), else least loaded, then name, then id."""
    if previous is not None:
        for c in candidates:
            if c.id == previous:
                return c
    return min(candidates, key=lambda c: (load.get(c.id, 0.0), c.name, str(c.id)))


# --------------------------------------------------------------------------- policy / catalog lookups
async def load_services(db: AsyncSession, service_ids: list[uuid.UUID]) -> list[Service]:
    """Load active services in request order; unknown/inactive ids raise NotFound."""
    if not service_ids:
        raise ValidationFailed("At least one service is required")
    rows = (await db.scalars(select(Service).where(Service.id.in_(service_ids), Service.active.is_(True)))).all()
    by_id = {s.id: s for s in rows}
    missing = [str(i) for i in service_ids if i not in by_id]
    if missing:
        raise NotFound("Unknown or inactive service", extra={"service_ids": missing})
    return [by_id[i] for i in dict.fromkeys(service_ids)]


def merge_policies(policies: Sequence[BookingPolicy]) -> BookingPolicy:
    """The strictest combination of several policies (blueprint §9.3, "Multiple Service Policies").

    Longest booking notice, longest cancellation notice, shortest booking window. Two judgment calls the
    blueprint leaves open: a cart is ``pending`` if any of its policies is, and the shortest
    ``pending_timeout`` applies. One distinct policy is returned as-is; a combination is a transient
    object (never persisted) whose ``merged_from`` lists the policy ids that produced it.
    """
    unique = list({p.id: p for p in policies}.values())
    if len(unique) == 1:
        return unique[0]
    merged = BookingPolicy(
        id=uuid.uuid4(), tenant_id=unique[0].tenant_id, name=("Combined: " + " + ".join(p.name for p in unique))[:100],
        default_status="pending" if any(p.default_status == "pending" for p in unique) else "confirmed",
        min_notice_booking=max(p.min_notice_booking for p in unique),
        min_notice_cancel=max(p.min_notice_cancel for p in unique),
        booking_window=min(p.booking_window for p in unique),
        pending_timeout=min(p.pending_timeout for p in unique),
    )
    merged.merged_from = [str(p.id) for p in unique]  # type: ignore[attr-defined]
    return merged


async def resolve_policy(db: AsyncSession, tenant: Tenant, services: list[Service]) -> BookingPolicy:
    """Each service's own policy (else the tenant default); a cart with several policies gets the strictest."""
    ids = {s.policy_id or tenant.default_policy_id for s in services} or {tenant.default_policy_id}
    if None in ids:
        raise ValidationFailed("No booking policy configured for this tenant/service")
    policies: list[BookingPolicy] = []
    for policy_id in sorted(ids, key=str):
        policy = await db.get(BookingPolicy, policy_id)
        if policy is None:
            raise ValidationFailed("No booking policy configured for this tenant/service")
        policies.append(policy)
    return merge_policies(policies)


def validate_booking_window(policy: BookingPolicy, start: dt.datetime, now: dt.datetime) -> None:
    from app.core.exceptions import OutsideBookingWindow

    if start < now + policy.min_notice_booking:
        raise OutsideBookingWindow("Too close to the start time to book")
    if start > now + policy.booking_window:
        raise OutsideBookingWindow("Too far in advance to book")


def booking_bounds(req: AvailabilityRequest) -> tuple[dt.datetime, dt.datetime]:
    """Earliest and latest start that may be offered: the policy's notice and window, or - for an admin move - anything from now on."""
    if req.ignore_notice:
        return req.now, req.now + dt.timedelta(days=3650)
    return req.now + req.policy.min_notice_booking, req.now + req.policy.booking_window


# --------------------------------------------------------------------------- the pipeline
async def operating_windows(db: AsyncSession, req: AvailabilityRequest) -> list[Interval] | None:
    """Tenant opening windows for the day; ``None`` means unrestricted, ``[]`` closed."""
    rows = (await db.scalars(select(OperatingHours))).all()
    if not rows:
        return None
    wd = weekday_index(req.day)
    return [window_on(req.day, r.open_time, r.close_time, req.tz) for r in rows if r.weekday == wd and r.close_time != r.open_time]


async def qualified_by_service(
    db: AsyncSession, services: list[Service], resource_type: str, only_id: uuid.UUID | None = None
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Active resources of ``resource_type`` able to perform each service, keyed by service id.

    A service with no ``service_resources`` rows *of that resource type* accepts any active resource
    of the type. Links are filtered by type so that, for example, a service linked only to a bay
    never disqualifies every mechanic. ``only_id`` restricts the pool to one resource.
    """
    q = select(Resource.id).where(Resource.type == resource_type, Resource.status == "active")
    if only_id:
        q = q.where(Resource.id == only_id)
    candidates = set((await db.scalars(q)).all())
    links = (await db.execute(
        select(ServiceResource.service_id, ServiceResource.resource_id)
        .join(Resource, Resource.id == ServiceResource.resource_id)
        .where(ServiceResource.service_id.in_([s.id for s in services]), Resource.type == resource_type)
    )).all()
    linked: dict[uuid.UUID, set[uuid.UUID]] = {}
    for sid, rid in links:
        linked.setdefault(sid, set()).add(rid)
    return {
        svc.id: sorted(candidates & linked[svc.id] if svc.id in linked else candidates) for svc in services
    }


async def qualified_staff_ids(db: AsyncSession, req: AvailabilityRequest, resource_type: str = "staff") -> list[uuid.UUID]:
    """Resources of ``resource_type`` qualified for *every* requested service (one resource does it all)."""
    needed = req.qualifying_services if req.qualifying_services is not None else req.services
    per_service = await qualified_by_service(db, needed, resource_type, req.resource_id)
    common: set[uuid.UUID] | None = None
    for ids in per_service.values():
        common = set(ids) if common is None else common & set(ids)
    return sorted(common or set())


async def working_windows(db: AsyncSession, req: AvailabilityRequest, resource_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, list[Interval]]:
    """Shift windows (breaks removed) of each resource on ``req.day``."""
    if not resource_ids:
        return {}
    shifts = (await db.scalars(
        select(ResourceShift).where(ResourceShift.resource_id.in_(list(resource_ids)), ResourceShift.weekday == weekday_index(req.day))
    )).all()
    out: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    for sh in shifts:
        out[sh.resource_id].extend(shift_windows(sh, req.day, req.tz))
    return out


async def busy_intervals(
    db: AsyncSession, resource_ids: list[uuid.UUID], lo: dt.datetime, hi: dt.datetime, now: dt.datetime
) -> dict[uuid.UUID, list[Interval]]:
    """Held/active allocations overlapping ``[lo, hi)`` per resource (expired holds ignored)."""
    if not resource_ids:
        return {}
    rows = (await db.execute(
        select(BookingAllocation.resource_id, BookingAllocation.period)
        .join(Booking, Booking.id == BookingAllocation.booking_id)
        .where(
            BookingAllocation.resource_id.in_(resource_ids),
            BookingAllocation.state.in_(("held", "active")),
            BookingAllocation.period.overlaps(Range(lo, hi, bounds="[)")),
            or_(
                BookingAllocation.state == "active",
                and_(Booking.hold_expires_at.is_not(None), Booking.hold_expires_at > now),
            ),
        )
    )).all()
    out: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    for rid, period in rows:
        if period.lower is not None and period.upper is not None:
            out[rid].append(Interval(period.lower, period.upper))
    return out


async def compute_availability(
    db: AsyncSession, req: AvailabilityRequest, resource_type: str = "staff", step_minutes: int | None = None
) -> list[SlotOption]:
    """Run the pipeline and return slots sorted by start time."""
    from app.core.config import get_settings

    step = dt.timedelta(minutes=step_minutes or get_settings().slot_step_minutes)
    tz = req.tz
    day_start = req.day_start

    # 1. tenant-wide constraints
    offs = (await db.scalars(select(DayOff))).all()
    if any(o.resource_id is None and covers_date(o, req.day) for o in offs):
        return []
    op_windows = await operating_windows(db, req)
    if op_windows == []:
        return []

    # 2. qualified resources + shifts
    resource_ids = await qualified_staff_ids(db, req, resource_type)
    on_leave = {o.resource_id for o in offs if o.resource_id and covers_date(o, req.day)}
    resource_ids = [r for r in resource_ids if r not in on_leave]
    if not resource_ids:
        return []
    shifts = (await db.scalars(
        select(ResourceShift).where(ResourceShift.resource_id.in_(resource_ids), ResourceShift.weekday == weekday_index(req.day))
    )).all()
    working: dict[uuid.UUID, list[Interval]] = defaultdict(list)
    for s in shifts:
        working[s.resource_id].extend(shift_windows(s, req.day, tz))

    # 3. existing allocations
    busy = await busy_intervals(db, list(working), day_start, req.overnight_end, req.now)  # windows may run past midnight

    # 4. booking-window bounds
    earliest, latest = booking_bounds(req)

    # 5. candidates
    block = dt.timedelta(minutes=req.block_minutes)
    by_start: dict[dt.datetime, list[uuid.UUID]] = defaultdict(list)
    for rid, windows in working.items():
        if op_windows is not None:
            windows = intersect_intervals(windows, op_windows)
        free = subtract_intervals(windows, busy.get(rid, []))
        for slot in generate_slots(free, block, step, day_start, earliest, latest):
            by_start[slot.start].append(rid)
    return [SlotOption(start.astimezone(tz), (start + block).astimezone(tz), tuple(sorted(ids))) for start, ids in sorted(by_start.items())]
