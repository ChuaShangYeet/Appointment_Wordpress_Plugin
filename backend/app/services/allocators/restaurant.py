"""Restaurant allocator (blueprint §7): table capacity fitting and separate-seating solver.

Rules implemented:

* one booking = a party; it is seated at the **smallest single table** that fits, or, only if the
  customer allowed separate seating, at the **fewest tables** whose total capacity covers the party
  (ties: least spare capacity, then by table name/id);
* VIP and normal tables are separate pools and are never combined;
* ``TableOpenRule`` caps how many tables of a class may be reserved *at the same time* on a day
  (date-specific rules override weekday rules);
* slots come from ``TimeSlotSetting`` (flexible or fixed mode); a table is occupied for
  ``slot_duration + buffer``.

The interval and seating maths are pure functions so they can be unit-tested without a database.
Cap enforcement and table choice run under a per-tenant-per-day advisory lock, so concurrent
reservations serialise instead of racing past the cap; the GiST exclusion constraint still
guards each individual table as the final backstop.
"""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFound, SlotUnavailable, ValidationFailed
from app.models import BookingPolicy, DayOff, Resource, Service, TableOpenRule, Tenant, TimeSlotSetting
from app.services.allocators.base import Assignment, BaseAllocator, lock_day
from app.services.availability import (
    AvailabilityRequest,
    Interval,
    SlotOption,
    booking_bounds,
    busy_intervals,
    clock_on,
    covers_date,
    operating_windows,
    weekday_index,
)

MAX_COMBINED_TABLES = 6


@dataclass(frozen=True)
class TableInfo:
    id: uuid.UUID
    name: str
    capacity: int
    is_vip: bool = False
    minimum_spend: Decimal = Decimal(0)

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (self.capacity, self.name, str(self.id))


# --------------------------------------------------------------------------- pure logic
def solve_seating(
    tables: Sequence[TableInfo], party: int, allow_separate: bool, max_tables: int = MAX_COMBINED_TABLES
) -> list[TableInfo] | None:
    """Choose tables for ``party`` or return ``None`` if impossible.

    1. smallest single table with ``capacity >= party``;
    2. if ``allow_separate``: the fewest tables (at most ``max_tables``) whose capacities sum to at
       least ``party``; among equals the smallest total capacity, then lowest names/ids (deterministic).

    Step 2 is a small subset-sum DP over reachable totals, keeping for each total the combination
    with the fewest tables. Extending a set that already covers the party is never useful, so those
    states are not expanded and the search stays tiny.
    """
    if party < 1:
        raise ValueError("party must be >= 1")
    ordered = sorted(tables, key=lambda t: t.sort_key)
    single = next((t for t in ordered if t.capacity >= party), None)
    if single is not None:
        return [single]
    if not allow_separate:
        return None

    # total capacity -> (table count, chosen tables)
    states: dict[int, tuple[int, tuple[TableInfo, ...]]] = {0: (0, ())}
    for table in ordered:
        for total, (count, chosen) in list(states.items()):
            if total >= party or count >= max_tables:
                continue
            new_total, new_state = total + table.capacity, (count + 1, chosen + (table,))
            current = states.get(new_total)
            if current is None or _combo_key(new_state) < _combo_key(current):
                states[new_total] = new_state
    covering = [(total, st) for total, st in states.items() if total >= party]
    if not covering:
        return None
    _, (_, best) = min(covering, key=lambda item: (item[1][0], item[0], _combo_ids(item[1])))
    return list(best)


def _combo_ids(state: tuple[int, tuple[TableInfo, ...]]) -> tuple[str, ...]:
    """Tie-break key: table names (then ids), the same ordering single-table choice uses."""
    return tuple(f"{t.name}|{t.id}" for t in state[1])


def _combo_key(state: tuple[int, tuple[TableInfo, ...]]) -> tuple[int, tuple[str, ...]]:
    return (state[0], _combo_ids(state))


def max_concurrency(intervals: Sequence[Interval], window: Interval) -> int:
    """Largest number of ``intervals`` simultaneously active anywhere inside ``window``."""
    events: list[tuple[dt.datetime, int]] = []
    for iv in intervals:
        start, end = max(iv.start, window.start), min(iv.end, window.end)
        if start < end:
            events += [(start, 1), (end, -1)]
    events.sort(key=lambda e: (e[0], e[1]))  # ends (-1) before starts (+1) at the same instant
    current = peak = 0
    for _, delta in events:
        current += delta
        peak = max(peak, current)
    return peak


def candidate_starts(
    *,
    mode: str,
    windows: Sequence[Interval],
    day_start: dt.datetime,
    duration: dt.timedelta,
    interval: dt.timedelta,
    first: dt.time,
    last: dt.time,
) -> list[dt.datetime]:
    """Reservation start times for one day (blueprint §7.3).

    * flexible: from each opening window's start, every ``interval``, up to ``closing - duration``;
    * fixed: from ``first`` every ``interval`` through ``last`` (the next day if ``last`` is before ``first``, i.e. a
      service that runs past midnight), keeping only starts whose seating (``duration``) fits inside an opening window.
    """
    starts: list[dt.datetime] = []
    if mode == "flexible":
        for w in windows:
            t = w.start
            while t + duration <= w.end:
                starts.append(t)
                t += interval
    else:
        tz = day_start.tzinfo
        if tz is None:
            raise ValueError("day_start must be timezone-aware")
        t = clock_on(day_start.date(), first, tz)
        stop = clock_on(day_start.date() + dt.timedelta(days=1 if last < first else 0), last, tz)
        while t <= stop:
            if any(w.start <= t and t + duration <= w.end for w in windows):
                starts.append(t)
            t += interval
    return sorted(set(starts))


# --------------------------------------------------------------------------- allocator
class RestaurantAllocator(BaseAllocator):
    """Seats a party at one table, or at several tables when the customer accepts separate seating.

    By default the system chooses. If the tenant enables ``allow_table_selection`` the customer may
    name one table; it must be free, of the requested VIP/normal class and large enough (a chosen
    table is never combined with others). All other rules (opening hours, open-table cap, minimum
    spend, locking) apply unchanged.
    """

    requires_services = False

    async def resolve_policy(self, db: AsyncSession, tenant: Tenant, services: list[Service]) -> BookingPolicy:
        """``TimeSlotSetting.policy_id`` overrides the tenant default policy."""
        setting = await self._settings(db)
        policy_id = setting.policy_id or tenant.default_policy_id
        policy = await db.get(BookingPolicy, policy_id) if policy_id else None
        if policy is None:
            raise ValidationFailed("No booking policy configured for this tenant")
        return policy

    async def find_slots(self, db: AsyncSession, req: AvailabilityRequest) -> list[SlotOption]:
        return [
            SlotOption(
                start=start.astimezone(req.tz), end=end.astimezone(req.tz), resource_ids=tuple(t.id for t in tables), combined=len(tables) > 1,
                minimum_spend=(sum((t.minimum_spend for t in tables), Decimal(0)) or None),
            )
            for start, end, tables in await self._plan(db, req)
        ]

    async def allocate(self, db: AsyncSession, req: AvailabilityRequest, start: dt.datetime) -> list[Assignment]:
        # Serialise all reservations for this tenant+day until the transaction ends, so the
        # open-table cap and table choice are evaluated against committed state, one at a time.
        await lock_day(db, req.tenant.id, req.day, "restaurant")
        plan = await self._plan(db, req)
        chosen = next(((end, tables) for s, end, tables in plan if s == start), None)
        if chosen is None:
            raise SlotUnavailable("That time is not available for this party", extra={"slots": [
                {"start_at": s.astimezone(req.tz).isoformat(), "end_at": e.astimezone(req.tz).isoformat(), "resource_ids": [str(t.id) for t in ts]}
                for s, e, ts in plan
            ]})
        end, tables = chosen
        return [Assignment(t.id, start, end, t.minimum_spend) for t in tables]

    # ------------------------------------------------------------------ internals
    async def _settings(self, db: AsyncSession) -> TimeSlotSetting:
        setting: TimeSlotSetting | None = await db.scalar(select(TimeSlotSetting))
        if setting is None:
            raise ValidationFailed("Restaurant time-slot settings are not configured")
        return setting

    async def _open_cap(self, db: AsyncSession, req: AvailabilityRequest) -> int | None:
        rules = (await db.scalars(select(TableOpenRule).where(TableOpenRule.table_type == req.table_type))).all()
        by_date = next((r for r in rules if r.specific_date == req.day), None)
        by_weekday = next((r for r in rules if r.weekday == weekday_index(req.day)), None)
        rule = by_date or by_weekday
        return rule.open_tables if rule else None

    async def _plan(self, db: AsyncSession, req: AvailabilityRequest) -> list[tuple[dt.datetime, dt.datetime, list[TableInfo]]]:
        """Every offered start with its proposed tables: ``[(start, end, tables)]`` sorted by start."""
        if req.party_size is None or req.party_size < 1:
            raise ValidationFailed("party_size is required and must be at least 1")
        if req.table_type not in ("normal", "vip"):
            raise ValidationFailed("table_type must be 'normal' or 'vip'")
        setting = await self._settings(db)
        if req.resource_id is not None:
            if not setting.allow_table_selection:
                raise ValidationFailed("Tables are assigned automatically at this restaurant; resource_id is not supported")
            if req.allow_separate_seating:
                raise ValidationFailed("A chosen table cannot be combined with others; drop allow_separate_seating")

        offs = (await db.scalars(select(DayOff))).all()
        if any(o.resource_id is None and covers_date(o, req.day) for o in offs):
            return []
        windows = await operating_windows(db, req)
        if not windows:  # closed that day, or no opening hours configured at all
            return []

        vip = req.table_type == "vip"
        rows = (await db.scalars(
            select(Resource).where(Resource.type == "table", Resource.status == "active")
        )).all()
        pool = [r for r in rows if bool(r.attributes.get("is_vip", False)) == vip and (r.capacity or 0) > 0]
        on_leave = {o.resource_id for o in offs if o.resource_id and covers_date(o, req.day)}
        tables = [_table_info(r) for r in pool if r.id not in on_leave]
        if req.resource_id is not None:
            chosen_row = next((r for r in rows if r.id == req.resource_id), None)
            if chosen_row is None:
                raise NotFound("Table not found")
            if bool(chosen_row.attributes.get("is_vip", False)) != vip:
                raise ValidationFailed("That table is in the other seating class; set table_type to match")
            tables = [t for t in tables if t.id == req.resource_id]  # empty if out of service that day
        if not tables:
            return []

        tz = req.tz
        day_start = dt.datetime.combine(req.day, dt.time.min, tzinfo=tz)
        duration = dt.timedelta(minutes=setting.slot_duration_minutes)
        occupancy = duration + dt.timedelta(minutes=setting.buffer_minutes)
        busy = await busy_intervals(db, [r.id for r in pool], req.day_start, req.overnight_end, req.now)
        all_busy = [iv for ivs in busy.values() for iv in ivs]
        cap = await self._open_cap(db, req)

        earliest, latest = booking_bounds(req)
        plan: list[tuple[dt.datetime, dt.datetime, list[TableInfo]]] = []
        for start in candidate_starts(
            mode=setting.booking_mode, windows=windows, day_start=day_start, duration=duration,
            interval=dt.timedelta(minutes=setting.slot_interval_minutes),
            first=setting.first_slot_time, last=setting.last_slot_time,
        ):
            if start < earliest or start > latest:
                continue
            slot = Interval(start, start + occupancy)
            free = [
                t for t in tables
                if not any(iv.start < slot.end and iv.end > slot.start for iv in busy.get(t.id, []))
            ]
            chosen = solve_seating(free, req.party_size, req.allow_separate_seating)
            if chosen is None:
                continue
            if cap is not None and max_concurrency(all_busy, slot) + len(chosen) > cap:
                continue
            plan.append((slot.start, slot.end, chosen))
        return plan


def _table_info(r: Resource) -> TableInfo:
    attrs: dict[str, Any] = r.attributes or {}
    return TableInfo(
        id=r.id, name=r.name, capacity=int(r.capacity or 0), is_vip=bool(attrs.get("is_vip", False)),
        minimum_spend=Decimal(str(attrs.get("minimum_spend", 0) or 0)),
    )
