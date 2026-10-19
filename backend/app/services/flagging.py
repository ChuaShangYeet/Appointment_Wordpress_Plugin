"""Review flags for bookings that a settings, shift or service change no longer fits.

Blueprint §7.3 / §8.3 / §13.5: when opening hours, a shift, a day off, a slot rule, a table cap, the
service area or a staff member's services change and a *confirmed* booking no longer fits, the booking
is **flagged for review, never cancelled**. A human decides whether to move it, keep it or cancel it.

* Only upcoming ``pending`` (unexpired hold) and ``confirmed`` bookings are examined; ones that have
  started or ended are history.
* Each change re-checks only the reasons it can affect, for the bookings it can affect (e.g. a shift edit
  re-checks ``outside_shift`` for that resource's bookings). A reason is *added* when the booking now
  violates it and *removed* when it no longer does, so undoing a change lifts its own flags. A flag a
  manager dismissed by hand stays dismissed until a later change touches the same thing again.
* Booking policies are deliberately not checked: each booking carries a frozen policy snapshot, so a
  policy edit never changes the rules of an existing booking (blueprint §6).
* Runs in the same transaction as the change, and locks the bookings it touches (``FOR UPDATE``, in id
  order) so two administrators changing settings at once cannot overwrite each other's flags.

The rules are pure functions over a preloaded :class:`Context`, so they are testable without a database;
the two that need one (staff qualification, PostGIS distance) are small async wrappers.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import NamedTuple
from zoneinfo import ZoneInfo

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import PostgisUnavailable
from app.models import (
    Booking,
    BookingAllocation,
    BookingItem,
    CustomerAddress,
    DayOff,
    OperatingHours,
    Resource,
    ResourceShift,
    Service,
    TableOpenRule,
    Tenant,
    TimeSlotSetting,
)
from app.services.availability import Interval, covers_date, qualified_by_service, shift_windows, weekday_index, window_on
from app.services.geo import measure_km

log = logging.getLogger(__name__)

#: reason code -> what it tells the administrator
FLAG_REASONS: dict[str, str] = {
    "outside_shift": "The assigned staff member's shift no longer covers this booking",
    "day_off": "A company holiday or the assigned resource's leave now covers this booking's day",
    "outside_operating_hours": "The booking is no longer inside the business's operating hours",
    "outside_slot_times": "The start time is outside the reservation times the restaurant now accepts",
    "staff_not_qualified": "The assigned staff member or bay is no longer set up for the booked service",
    "table_cap_exceeded": "More tables are reserved at once than the open-table rule now allows",
    "outside_service_area": "The on-site address is now outside the business's service radius",
    "resource_inactive": "An assigned staff member, table or bay has been deactivated",
    "table_too_small": "The assigned table(s) no longer seat the whole party",
    "table_class_changed": "An assigned table changed between normal and VIP, so it no longer matches the booking's table type",
}


# --------------------------------------------------------------------------- context
@dataclass
class Context:
    """Everything the rules read, loaded once per re-check."""

    tenant: Tenant
    tz: ZoneInfo
    now: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    resources: dict[uuid.UUID, Resource] = field(default_factory=dict)
    shifts: dict[uuid.UUID, list[ResourceShift]] = field(default_factory=dict)
    days_off: list[DayOff] = field(default_factory=list)
    hours: list[OperatingHours] = field(default_factory=list)
    slot_setting: TimeSlotSetting | None = None
    table_rules: list[TableOpenRule] = field(default_factory=list)

    @property
    def is_restaurant(self) -> bool:
        return self.tenant.business_type == "restaurant"

    def local_date(self, t: dt.datetime) -> dt.date:
        return t.astimezone(self.tz).date()

    def operating_windows(self, day: dt.date) -> list[Interval] | None:
        """Opening windows on ``day``; ``None`` means no hours are configured (unrestricted)."""
        if not self.hours:
            return None
        wd = weekday_index(day)
        return [window_on(day, r.open_time, r.close_time, self.tz) for r in self.hours if r.weekday == wd and r.close_time != r.open_time]

    def shift_windows_on(self, resource_id: uuid.UUID, day: dt.date) -> list[Interval]:
        """Working windows (breaks removed) of the shifts of ``resource_id`` that start on business day ``day``."""
        wd = weekday_index(day)
        return [w for sh in self.shifts.get(resource_id, []) if sh.weekday == wd for w in shift_windows(sh, day, self.tz)]

    def candidate_days(self, t: dt.datetime) -> tuple[dt.date, dt.date]:
        """The business days an instant can belong to: its calendar date, or the day before (overnight hours run past midnight)."""
        d = self.local_date(t)
        return d, d - dt.timedelta(days=1)


async def load_context(db: AsyncSession, tenant: Tenant, bookings: Sequence[Booking], now: dt.datetime) -> Context:
    ctx = Context(tenant=tenant, tz=ZoneInfo(tenant.timezone), now=now)
    rids = {sp.resource_id for b in bookings for sp in _spans(b, now)}
    if rids:
        ctx.resources = {r.id: r for r in (await db.scalars(select(Resource).where(Resource.id.in_(rids)))).all()}
        for sh in (await db.scalars(select(ResourceShift).where(ResourceShift.resource_id.in_(rids)))).all():
            ctx.shifts.setdefault(sh.resource_id, []).append(sh)
    ctx.days_off = list((await db.scalars(select(DayOff))).all())
    ctx.hours = list((await db.scalars(select(OperatingHours))).all())
    ctx.slot_setting = await db.scalar(select(TimeSlotSetting))
    ctx.table_rules = list((await db.scalars(select(TableOpenRule))).all())
    return ctx


class Span(NamedTuple):
    """One resource reserved over ``[start, end)``."""

    resource_id: uuid.UUID
    start: dt.datetime
    end: dt.datetime
    #: The service the reservation is for (workshop segments); ``None`` = the resource serves the whole booking.
    service_id: uuid.UUID | None = None


def _spans(b: Booking, after: dt.datetime) -> list[Span]:
    """The booking's reservations that still lie ahead: not released, and not already over.

    A job that went ``waiting-for-parts`` and was resumed keeps its old released and truncated segments;
    work already done is history and must not be re-judged against today's shifts and hours.
    """
    return [
        Span(a.resource_id, a.period.lower, a.period.upper, a.service_id)
        for a in b.allocations
        if a.state != "released" and a.period.lower is not None and a.period.upper is not None and a.period.upper > after
    ]


# --------------------------------------------------------------------------- pure rules
def business_day(ctx: Context, sp: Span) -> dt.date:
    """The business day an allocation belongs to (Business Day Rule): the day whose hours/shift window it starts in.

    Normally its calendar date; for the early hours of an overnight window it is the day before. A staff member's own
    shifts decide when they have any, otherwise the opening hours; with neither it is the calendar date.
    """
    res = ctx.resources.get(sp.resource_id)
    own_shifts = res is not None and res.type == "staff" and bool(ctx.shifts.get(sp.resource_id))
    for day in ctx.candidate_days(sp.start):
        windows = ctx.shift_windows_on(sp.resource_id, day) if own_shifts else (ctx.operating_windows(day) or [])
        if any(w.start <= sp.start < w.end for w in windows):
            return day
    return ctx.local_date(sp.start)


def on_day_off(ctx: Context, b: Booking) -> bool:
    """A company holiday, or leave of an allocated resource, covers the business day an allocation belongs to."""
    for sp in _spans(b, ctx.now):
        day = business_day(ctx, sp)
        if any(covers_date(off, day) and off.resource_id in (None, sp.resource_id) for off in ctx.days_off):
            return True
    return False


def outside_shift(ctx: Context, b: Booking) -> bool:
    """A staff allocation is not entirely inside a shift window (breaks excluded) on its day."""
    for sp in _spans(b, ctx.now):
        res = ctx.resources.get(sp.resource_id)
        if res is None or res.type != "staff":
            continue  # tables and bays have no shifts
        if not any(w.start <= sp.start and sp.end <= w.end for day in ctx.candidate_days(sp.start) for w in ctx.shift_windows_on(sp.resource_id, day)):
            return True
    return False


def outside_operating_hours(ctx: Context, b: Booking) -> bool:
    """An allocation is not inside an opening window on its day.

    A restaurant seating is judged on ``slot_duration`` (the buffer after it may run past closing, as it
    does when booking), and a restaurant with no hours at all can take no reservations.
    """
    for sp in _spans(b, ctx.now):
        lo, hi = sp.start, sp.end
        if ctx.is_restaurant and ctx.slot_setting is not None:
            hi = lo + dt.timedelta(minutes=ctx.slot_setting.slot_duration_minutes)
        if not ctx.hours:
            if ctx.is_restaurant:
                return True
            continue
        if not any(w.start <= lo and hi <= w.end for day in ctx.candidate_days(lo) for w in ctx.operating_windows(day) or []):
            return True
    return False


def outside_slot_times(ctx: Context, b: Booking) -> bool:
    """Fixed-mode restaurants accept starts only between ``first_slot_time`` and ``last_slot_time``."""
    setting = ctx.slot_setting
    if b.table_type is None or setting is None or setting.booking_mode != "fixed":
        return False
    start = b.start_at.astimezone(ctx.tz).time()
    first, last = setting.first_slot_time, setting.last_slot_time
    if last < first:  # the last slot is after midnight
        return not (start >= first or start <= last)
    return not (first <= start <= last)


def resource_inactive(ctx: Context, b: Booking) -> bool:
    """An allocated resource has been deactivated (its reservation stays until a human moves or cancels it)."""
    return any((res := ctx.resources.get(sp.resource_id)) is not None and res.status == "inactive" for sp in _spans(b, ctx.now))


def _tables(ctx: Context, b: Booking) -> list[Resource]:
    ids = dict.fromkeys(sp.resource_id for sp in _spans(b, ctx.now))
    return [r for i in ids if (r := ctx.resources.get(i)) is not None and r.type == "table"]


def table_too_small(ctx: Context, b: Booking) -> bool:
    """The seats at the assigned table(s) add up to fewer than the party."""
    tables = _tables(ctx, b)
    return b.party_size is not None and bool(tables) and sum(t.capacity or 0 for t in tables) < b.party_size


def table_class_changed(ctx: Context, b: Booking) -> bool:
    """A table the booking sits at is now in the other seating class (VIP tables and normal ones are never mixed)."""
    return b.table_type is not None and any(("vip" if t.attributes.get("is_vip", False) else "normal") != b.table_type for t in _tables(ctx, b))


def table_cap_exceeded(ctx: Context, bookings: Sequence[Booking]) -> set[uuid.UUID]:
    """Bookings that are seated at a moment when more tables of their class are reserved than the cap.

    The cap is per table class and day (a date-specific rule beats a weekday rule; none = uncapped).
    Everyone reserved at an over-cap moment is flagged, because which of them to move is a human call.
    """
    groups: dict[tuple[dt.date, str], list[tuple[dt.datetime, int, uuid.UUID]]] = defaultdict(list)
    for b in bookings:
        if b.table_type is None:
            continue
        for sp in _spans(b, ctx.now):
            res = ctx.resources.get(sp.resource_id)
            if res is None or res.type != "table":
                continue
            groups[(business_day(ctx, sp), b.table_type)] += [(sp.start, 1, b.id), (sp.end, -1, b.id)]
    over: set[uuid.UUID] = set()
    for (day, table_type), events in groups.items():
        cap = _cap_for(ctx, day, table_type)
        if cap is None:
            continue
        active: dict[uuid.UUID, int] = defaultdict(int)
        total = 0
        for _, delta, bid in sorted(events, key=lambda e: (e[0], e[1])):  # ends (-1) before starts (+1)
            active[bid] += delta
            total += delta
            if active[bid] == 0:
                del active[bid]
            if total > cap:
                over.update(active)
    return over


def _cap_for(ctx: Context, day: dt.date, table_type: str) -> int | None:
    rules = [r for r in ctx.table_rules if r.table_type == table_type]
    rule = next((r for r in rules if r.specific_date == day), None) or next((r for r in rules if r.weekday == weekday_index(day)), None)
    return rule.open_tables if rule else None


# --------------------------------------------------------------------------- checks that need the database
#: A check returns the ids of the bookings that violate its reason, or ``None`` when it cannot tell
#: (then existing flags for that reason are left alone).
Check = Callable[[AsyncSession, Context, Sequence[Booking]], Awaitable[set[uuid.UUID] | None]]


def _sync(rule: Callable[[Context, Booking], bool]) -> Check:
    async def check(db: AsyncSession, ctx: Context, bookings: Sequence[Booking]) -> set[uuid.UUID] | None:
        return {b.id for b in bookings if rule(ctx, b)}

    return check


async def _check_table_cap(db: AsyncSession, ctx: Context, bookings: Sequence[Booking]) -> set[uuid.UUID] | None:
    return table_cap_exceeded(ctx, bookings)


async def _check_qualification(db: AsyncSession, ctx: Context, bookings: Sequence[Booking]) -> set[uuid.UUID] | None:
    """Every reservation still ahead must be held by a resource that can still do what that reservation is for.

    The rule is the one that applied when the booking was made:

    * a reservation that knows its service (a workshop job-order segment, each with its own mechanic and bay) needs a resource
      qualified for **that** service - a mechanic who lost "brake service" is flagged on a job whose brake segment he holds, even
      though he can still do the oil change in the same job;
    * a reservation without one is a single resource serving the whole booking (a spa treatment of several services, a salon
      cart), which had to be qualified for **every** base service. Add-ons are ignored, as when booking (a stylist qualifies
      through the base service only);
    * the exception is a workshop job order that predates ``booking_allocations.service_id`` and has several services: nothing
      says which segment a row belongs to, so such a row keeps the old lenient rule (qualified for at least one of the services).
    """
    ids = [b.id for b in bookings]
    if not ids:
        return set()
    items = (await db.execute(
        select(BookingItem.booking_id, Service).join(Service, Service.id == BookingItem.service_id).where(BookingItem.booking_id.in_(ids))
    )).all()
    base: dict[uuid.UUID, list[Service]] = defaultdict(list)
    for booking_id, svc in items:
        if not svc.is_addon:
            base[booking_id].append(svc)
    known = {s.id: s for ss in base.values() for s in ss}
    named = {sp.service_id for b in bookings for sp in _spans(b, ctx.now) if sp.service_id is not None}
    if named - known.keys():
        known.update({s.id: s for s in (await db.scalars(select(Service).where(Service.id.in_(named - known.keys())))).all()})
    services = list(known.values())
    qualified: dict[str, dict[uuid.UUID, set[uuid.UUID]]] = {}
    for rtype in ("staff", "bay"):
        qualified[rtype] = {sid: set(rids) for sid, rids in (await qualified_by_service(db, services, rtype)).items()} if services else {}
    workshop = ctx.tenant.business_type == "automotive"
    out: set[uuid.UUID] = set()
    for b in bookings:
        for sp in _spans(b, ctx.now):
            res = ctx.resources.get(sp.resource_id)
            if res is None or res.type not in qualified:
                continue
            can = qualified[res.type]
            if sp.service_id is not None:
                ok = sp.resource_id in can.get(sp.service_id, set())
            elif not base[b.id]:
                continue
            elif workshop and len(base[b.id]) > 1:
                ok = any(sp.resource_id in can.get(svc.id, set()) for svc in base[b.id])
            else:
                ok = all(sp.resource_id in can.get(svc.id, set()) for svc in base[b.id])
            if not ok:
                out.add(b.id)
    return out


async def _check_service_area(db: AsyncSession, ctx: Context, bookings: Sequence[Booking]) -> set[uuid.UUID] | None:
    """On-site bookings whose (geocoded) address is now beyond the service radius, measured in PostGIS."""
    t = ctx.tenant
    if t.latitude is None or t.longitude is None or t.service_radius_km is None:
        return set()  # no radius configured: nothing to be outside of
    on_site = [b for b in bookings if b.address_id is not None]
    if not on_site:
        return set()
    addresses = {
        a.id: a for a in (await db.scalars(select(CustomerAddress).where(CustomerAddress.id.in_({b.address_id for b in on_site})))).all()
    }
    out: set[uuid.UUID] = set()
    for b in on_site:
        addr = addresses.get(b.address_id) if b.address_id else None
        if addr is None or addr.latitude is None or addr.longitude is None:
            continue  # not geocoded yet: cannot be judged
        try:
            _, within = await measure_km(db, (t.latitude, t.longitude), (addr.latitude, addr.longitude), t.service_radius_km)
        except PostgisUnavailable:
            log.warning("PostGIS unavailable: service-area flags not re-evaluated")
            return None
        if not within:
            out.add(b.id)
    return out


CHECKS: dict[str, Check] = {
    "outside_shift": _sync(outside_shift),
    "day_off": _sync(on_day_off),
    "outside_operating_hours": _sync(outside_operating_hours),
    "outside_slot_times": _sync(outside_slot_times),
    "staff_not_qualified": _check_qualification,
    "table_cap_exceeded": _check_table_cap,
    "outside_service_area": _check_service_area,
    "resource_inactive": _sync(resource_inactive),
    "table_too_small": _sync(table_too_small),
    "table_class_changed": _sync(table_class_changed),
}
assert CHECKS.keys() == FLAG_REASONS.keys()


# --------------------------------------------------------------------------- re-check
@dataclass
class FlagReport:
    """What a re-check changed: booking ids per reason."""

    flagged: dict[str, list[uuid.UUID]] = field(default_factory=lambda: defaultdict(list))
    cleared: dict[str, list[uuid.UUID]] = field(default_factory=lambda: defaultdict(list))

    @property
    def newly_flagged(self) -> int:
        """Distinct bookings that gained at least one flag."""
        return len({bid for ids in self.flagged.values() for bid in ids})

    @property
    def lifted(self) -> int:
        """Distinct bookings that lost at least one flag because they fit again."""
        return len({bid for ids in self.cleared.values() for bid in ids})


async def reevaluate(
    db: AsyncSession,
    tenant: Tenant,
    reasons: Iterable[str],
    *,
    resource_ids: Iterable[uuid.UUID] | None = None,
    service_id: uuid.UUID | None = None,
    booking_ids: Iterable[uuid.UUID] | None = None,
    now: dt.datetime | None = None,
) -> FlagReport:
    """Re-check ``reasons`` for the upcoming bookings a change can affect; flag violations, lift resolved ones.

    ``resource_ids`` limits the check to bookings holding one of those resources, ``service_id`` to
    bookings that include that service, ``booking_ids`` to exactly those bookings. Never changes a booking's status or allocations.
    """
    now = now or dt.datetime.now(dt.UTC)
    unknown = set(reasons) - CHECKS.keys()
    if unknown:
        raise ValueError(f"unknown flag reasons: {sorted(unknown)}")
    reasons = list(dict.fromkeys(reasons))

    q = (
        select(Booking)
        .where(
            Booking.end_at > now,
            or_(Booking.status == "confirmed", and_(Booking.status == "pending", Booking.hold_expires_at > now)),
            Booking.id.in_(select(BookingAllocation.booking_id).where(BookingAllocation.state.in_(("held", "active")))),
        )
        .order_by(Booking.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if resource_ids is not None:
        q = q.where(Booking.id.in_(select(BookingAllocation.booking_id).where(BookingAllocation.resource_id.in_(list(resource_ids)))))
    if booking_ids is not None:
        q = q.where(Booking.id.in_(list(booking_ids)))
    if service_id is not None:
        q = q.where(Booking.id.in_(select(BookingItem.booking_id).where(BookingItem.service_id == service_id)))
    bookings = list((await db.scalars(q)).unique().all())

    report = FlagReport()
    if not bookings:
        return report
    # the caller's Tenant was loaded outside this session and may predate the change being checked
    fresh = await db.get(Tenant, tenant.id, populate_existing=True)
    ctx = await load_context(db, fresh or tenant, bookings, now)
    for reason in reasons:
        violating = await CHECKS[reason](db, ctx, bookings)
        if violating is None:
            continue
        for b in bookings:
            has = reason in b.flags
            if b.id in violating and not has:
                b.flags = [*b.flags, reason]  # assign a new list: in-place edits are not tracked
                report.flagged[reason].append(b.id)
            elif b.id not in violating and has:
                b.flags = [f for f in b.flags if f != reason]
                report.cleared[reason].append(b.id)
    await db.flush()
    return report
