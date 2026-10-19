"""Restaurant allocator: seating solver, slot modes, VIP, open-table caps (blueprint §7)."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.core.exceptions import MinimumSpendNotAccepted
from app.services.allocators.restaurant import (
    TableInfo,
    candidate_starts,
    max_concurrency,
    solve_seating,
)
from app.services.availability import Interval, weekday_index
from app.services.booking import ensure_minimum_spend_accepted
from tests.conftest import PLATFORM, TZ, Seed, at

D = dt.date(2030, 1, 7)


def T(name: str, cap: int) -> TableInfo:
    return TableInfo(id=uuid.uuid5(uuid.NAMESPACE_DNS, name), name=name, capacity=cap)


def names(tables: list[TableInfo] | None) -> list[str] | None:
    return None if tables is None else sorted(t.name for t in tables)


# --------------------------------------------------------------------- pure: seating solver
def test_smallest_single_table_that_fits() -> None:
    tables = [T("a", 6), T("b", 2), T("c", 4), T("d", 2)]
    assert names(solve_seating(tables, 2, False)) == ["b"]  # 2-top, not the 4 or 6
    assert names(solve_seating(tables, 3, False)) == ["c"]
    assert names(solve_seating(tables, 5, False)) == ["a"]


def test_equal_capacity_is_deterministic_by_name() -> None:
    assert names(solve_seating([T("z", 2), T("m", 2), T("b", 2)], 2, False)) == ["b"]


def test_no_single_fit_and_separate_seating_not_allowed_means_none() -> None:
    assert solve_seating([T("a", 4), T("b", 4)], 7, False) is None


def test_separate_seating_uses_fewest_tables() -> None:
    tables = [T("a", 2), T("b", 2), T("c", 4), T("d", 4)]
    assert names(solve_seating(tables, 7, True)) == ["c", "d"]  # 4+4 (2 tables) beats 4+2+2 (3 tables)


def test_separate_seating_tie_prefers_least_spare_capacity() -> None:
    tables = [T("a", 3), T("b", 3), T("c", 5), T("d", 5)]
    assert names(solve_seating(tables, 7, True)) == ["a", "c"]  # 3+5=8 rather than 5+5=10


def test_single_table_always_preferred_over_combining() -> None:
    assert names(solve_seating([T("a", 4), T("b", 4), T("c", 8)], 8, True)) == ["c"]


def test_separate_seating_impossible_when_total_capacity_too_small() -> None:
    assert solve_seating([T("a", 2), T("b", 2)], 5, True) is None


def test_combination_size_is_bounded() -> None:
    ones = [T(f"t{i}", 1) for i in range(7)]
    assert solve_seating(ones, 7, True, max_tables=6) is None
    assert len(solve_seating(ones, 6, True, max_tables=6) or []) == 6


def test_solver_result_is_independent_of_input_order() -> None:
    tables = [T("a", 3), T("b", 3), T("c", 5), T("d", 5), T("e", 2)]
    assert names(solve_seating(tables, 7, True)) == names(solve_seating(list(reversed(tables)), 7, True))


# --------------------------------------------------------------------- pure: concurrency + slots
def _iv(h1: int, m1: int, h2: int, m2: int) -> Interval:
    return Interval(at(D, h1, m1), at(D, h2, m2))


def test_max_concurrency_counts_overlap_but_not_touching() -> None:
    win = _iv(18, 0, 21, 0)
    assert max_concurrency([_iv(18, 0, 19, 0), _iv(19, 0, 20, 0)], win) == 1  # back-to-back
    assert max_concurrency([_iv(18, 0, 19, 30), _iv(19, 0, 20, 0), _iv(19, 15, 19, 45)], win) == 3
    assert max_concurrency([_iv(10, 0, 12, 0)], win) == 0  # outside the window
    assert max_concurrency([_iv(17, 0, 18, 30), _iv(20, 30, 22, 0)], win) == 1  # clipped to the window


def _starts(**kw: Any) -> list[str]:
    base: dict[str, Any] = dict(
        windows=[_iv(11, 0, 22, 0)], day_start=at(D, 0), duration=dt.timedelta(minutes=90),
        interval=dt.timedelta(minutes=30), first=dt.time(11), last=dt.time(20, 30),
    )
    base.update(kw)
    return [s.astimezone(TZ).strftime("%H:%M") for s in candidate_starts(**base)]


def test_flexible_mode_runs_from_opening_to_closing_minus_duration() -> None:
    starts = _starts(mode="flexible")
    assert starts[0] == "11:00" and starts[-1] == "20:30" and len(starts) == 20  # 22:00 - 90min


def test_flexible_mode_anchors_at_opening_time() -> None:
    assert _starts(mode="flexible", windows=[_iv(17, 15, 20, 0)])[:3] == ["17:15", "17:45", "18:15"]


def test_fixed_mode_uses_first_to_last_slot_time() -> None:
    assert _starts(mode="fixed", first=dt.time(12), last=dt.time(14), interval=dt.timedelta(hours=1)) == ["12:00", "13:00", "14:00"]


def test_fixed_mode_drops_starts_that_do_not_fit_before_closing() -> None:
    got = _starts(mode="fixed", windows=[_iv(11, 0, 14, 30)], first=dt.time(12), last=dt.time(15), interval=dt.timedelta(hours=1))
    assert got == ["12:00", "13:00"]  # 14:00 + 90min would run past 14:30


# --------------------------------------------------------------------- end to end (needs PostgreSQL)
class Restaurant(Seed):
    """A restaurant tenant: 2-tops a,b · 4-top · 6-top · VIP 4-top ($300) · VIP 2-top ($200)."""

    tables: dict[str, uuid.UUID]


async def make_restaurant(
    client: httpx.AsyncClient, *, default_status: str = "confirmed", configure: bool = True
) -> Restaurant:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Bistro {uuid.uuid4().hex[:6]}", "business_type": "restaurant"})
    assert r.status_code == 201, r.text
    rest = Restaurant(client, r.json())
    rest.admin = await rest.token("owner")
    if not configure:
        rest.tables = {}
        return rest
    ok = await client.post(
        "/v1/admin/policies", headers=rest.admin,
        json={"name": "Dining", "default_status": default_status, "min_notice_booking": 60,
              "min_notice_cancel": 7200, "booking_window": 60 * 86400, "pending_timeout": 1800, "make_default": True},
    )
    assert ok.status_code == 201, ok.text
    hours = [{"weekday": d, "open_time": "11:00:00", "close_time": "22:00:00"} for d in range(7)]
    assert (await client.put("/v1/admin/operating-hours", json=hours, headers=rest.admin)).status_code == 200
    await put_slots(rest)
    rest.tables = {}
    for name, cap, attrs in [
        ("a2", 2, {}), ("b2", 2, {}), ("c4", 4, {}), ("d6", 6, {}),
        ("v4", 4, {"is_vip": True, "minimum_spend": 300}), ("v2", 2, {"is_vip": True, "minimum_spend": 200}),
    ]:
        rest.tables[name] = await add_table(rest, name, cap, attrs)
    return rest


async def put_slots(rest: Seed, **over: Any) -> None:
    body: dict[str, Any] = {
        "booking_mode": "flexible", "slot_duration_minutes": 90, "slot_interval_minutes": 30,
        "first_slot_time": "11:00:00", "last_slot_time": "20:30:00", "buffer_minutes": 15,
    }
    body.update(over)
    r = await rest.client.put("/v1/admin/time-slot-settings", json=body, headers=rest.admin)
    assert r.status_code == 200, r.text


async def add_table(rest: Seed, name: str, cap: int, attrs: dict[str, Any]) -> uuid.UUID:
    r = await rest.client.post(
        "/v1/admin/resources", headers=rest.admin,
        json={"type": "table", "name": name, "capacity": cap, "attributes": {"location": "indoor", **attrs}},
    )
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


def body(rest: Seed, start: dt.datetime, party: int, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "start_at": start.isoformat(), "party_size": party,
        "customer": {"name": "Aisha", "phone": f"+6012{uuid.uuid4().int % 10**7:07d}"},
    }
    out.update(extra)
    return out


async def slots(rest: Seed, party: int, *, day: dt.date | None = None, **params: Any) -> list[dict[str, Any]]:
    q = {"date": (day or rest.day).isoformat(), "party_size": party, **params}
    r = await rest.client.get("/v1/public/availability", params=q, headers=rest.pk)
    assert r.status_code == 200, r.text
    return r.json()["slots"]  # type: ignore[no-any-return]


def hhmm(ss: list[dict[str, Any]]) -> list[str]:
    return [dt.datetime.fromisoformat(s["start_at"]).astimezone(TZ).strftime("%H:%M") for s in ss]


async def book(rest: Seed, start: dt.datetime, party: int, **extra: Any) -> httpx.Response:
    return await rest.client.post("/v1/public/bookings", json=body(rest, start, party, **extra), headers=rest.pk)


@pytest.fixture
async def rest(client: httpx.AsyncClient) -> Restaurant:
    return await make_restaurant(client)


async def test_party_gets_the_smallest_fitting_normal_table(rest: Restaurant) -> None:
    for party, expected in [(2, {"a2"}), (3, {"c4"}), (5, {"d6"})]:  # equal 2-tops: lowest name first
        proposed = {rid for s in await slots(rest, party) for rid in s["resource_ids"]}
        assert proposed and proposed <= {str(rest.tables[n]) for n in expected}, (party, proposed)
    r = await book(rest, at(rest.day, 19), 3)
    assert r.status_code == 201 and r.json()["resource_ids"] == [str(rest.tables["c4"])]


async def test_slot_grid_and_occupancy_follow_time_slot_settings(rest: Restaurant) -> None:
    ss = await slots(rest, 2)
    starts = hhmm(ss)
    assert starts[0] == "11:00" and starts[-1] == "20:30"  # closing 22:00 - 90 min
    first = ss[0]
    occupied = dt.datetime.fromisoformat(first["end_at"]) - dt.datetime.fromisoformat(first["start_at"])
    assert occupied == dt.timedelta(minutes=105)  # 90 duration + 15 buffer


async def test_fixed_mode_only_offers_the_configured_grid(rest: Restaurant) -> None:
    await put_slots(rest, booking_mode="fixed", slot_interval_minutes=120, first_slot_time="12:00:00", last_slot_time="18:00:00")
    assert hhmm(await slots(rest, 2)) == ["12:00", "14:00", "16:00", "18:00"]


async def test_separate_seating_combines_the_fewest_tables_only_when_allowed(rest: Restaurant) -> None:
    assert await slots(rest, 8) == []  # no single table seats 8
    combos = await slots(rest, 8, allow_separate_seating="true")
    assert combos and all(s["combined"] and len(s["resource_ids"]) == 2 for s in combos)  # 6 + 2, not 6+... more
    assert set(combos[0]["resource_ids"]) == {str(rest.tables["d6"]), str(rest.tables["a2"])}

    refused = await book(rest, at(rest.day, 19), 8)
    assert refused.status_code == 409 and refused.json()["error"] == "slot_unavailable"
    ok = await book(rest, at(rest.day, 19), 8, allow_separate_seating=True)
    assert ok.status_code == 201 and len(ok.json()["resource_ids"]) == 2
    tables = {t for t in ok.json()["resource_ids"]}
    assert tables == {str(rest.tables["d6"]), str(rest.tables["a2"])}


async def test_vip_and_normal_pools_are_never_mixed(rest: Restaurant) -> None:
    vip_ids = {str(rest.tables["v4"]), str(rest.tables["v2"])}
    normal = {rid for s in await slots(rest, 2) for rid in s["resource_ids"]}
    assert normal and not (normal & vip_ids)
    vip = {rid for s in await slots(rest, 2, table_type="vip") for rid in s["resource_ids"]}
    assert vip and vip <= vip_ids
    # 5 guests: the normal 6-top fits, but a VIP request must not borrow it, and VIP tables alone cannot seat 5 singly
    assert await slots(rest, 5, table_type="vip") == []
    # combining is confined to the pool too: VIP 4 + VIP 2 = 6 (never a normal table)
    combo = await slots(rest, 5, table_type="vip", allow_separate_seating="true")
    assert combo and all(set(s["resource_ids"]) == vip_ids for s in combo)
    # a normal party too big for normal singles can't spill onto VIP tables either
    assert await slots(rest, 15, allow_separate_seating="true") == []  # normal tables seat 14 in total; VIP must not top it up
    assert await slots(rest, 12, allow_separate_seating="true")  # 6+4+2 fits within the normal pool


async def test_vip_minimum_spend_must_be_accepted(rest: Restaurant) -> None:
    offered = await slots(rest, 3, table_type="vip")
    assert offered and Decimal(offered[0]["minimum_spend"]) == Decimal(300)
    start = at(rest.day, 19)
    r = await book(rest, start, 3, table_type="vip")
    assert r.status_code == 422 and r.json()["error"] == "minimum_spend_not_accepted"
    assert Decimal(r.json()["minimum_spend"]) == Decimal(300)
    r = await book(rest, start, 3, table_type="vip", accept_minimum_spend=True)
    assert r.status_code == 201 and Decimal(r.json()["minimum_spend_agreed"]) == Decimal(300)
    assert r.json()["resource_ids"] == [str(rest.tables["v4"])]
    # nothing was written by the refused attempt: the VIP 4-top is booked exactly once
    listing = (await rest.client.get("/v1/admin/bookings", params={"table_type": "vip"}, headers=rest.admin)).json()
    assert len(listing) == 1 and listing[0]["table_type"] == "vip"


async def test_combined_vip_minimum_spend_is_the_sum(rest: Restaurant) -> None:
    r = await book(rest, at(rest.day, 19), 6, table_type="vip", allow_separate_seating=True, accept_minimum_spend=True)
    assert r.status_code == 201 and Decimal(r.json()["minimum_spend_agreed"]) == Decimal(500)


async def test_normal_tables_do_not_ask_for_minimum_spend(rest: Restaurant) -> None:
    r = await book(rest, at(rest.day, 19), 2)
    assert r.status_code == 201 and r.json()["minimum_spend_agreed"] is None


# --------------------------------------------------------------------- the amount shown (accepted_minimum_spend)
@pytest.mark.parametrize(("spend", "accept", "accepted", "ok"), [
    ("0", False, None, True), ("0", False, "0", True),  # no minimum spend: nothing to accept
    ("300", False, None, False), ("300", True, None, True),  # the older yes/no: any amount
    ("300", False, "300", True), ("300", False, "299.99", False), ("200", False, "500", True),  # the number: up to it
    ("300", True, "299", False),  # the number governs when both are sent
])
def test_minimum_spend_rule(spend: str, accept: bool, accepted: str | None, ok: bool) -> None:
    amount = Decimal(accepted) if accepted is not None else None
    if ok:
        ensure_minimum_spend_accepted(Decimal(spend), accept, amount)
        return
    with pytest.raises(MinimumSpendNotAccepted) as e:
        ensure_minimum_spend_accepted(Decimal(spend), accept, amount)
    assert e.value.extra["minimum_spend"] == spend
    assert e.value.extra.get("accepted_minimum_spend") == accepted  # (absent when only the yes/no was sent)


async def test_the_amount_shown_caps_what_the_booking_may_cost(rest: Restaurant) -> None:
    """``accepted_minimum_spend`` is the amount the client showed: tables that need more are refused with the new amount, never booked."""
    start = at(rest.day, 19)
    r = await book(rest, start, 3, table_type="vip", accepted_minimum_spend="299.99")
    assert r.status_code == 422 and r.json()["error"] == "minimum_spend_not_accepted", r.text
    assert Decimal(r.json()["minimum_spend"]) == Decimal(300) and Decimal(r.json()["accepted_minimum_spend"]) == Decimal("299.99")
    assert "300" in r.json()["detail"] and "299.99" in r.json()["detail"]
    r = await book(rest, start, 3, table_type="vip", accept_minimum_spend=True, accepted_minimum_spend=0)  # the number governs, not the yes
    assert r.status_code == 422 and Decimal(r.json()["minimum_spend"]) == Decimal(300)
    assert (await rest.client.get("/v1/admin/bookings", headers=rest.admin)).json() == []  # the refusals wrote nothing
    r = await book(rest, start, 3, table_type="vip", accepted_minimum_spend=300)  # with the number no yes/no is needed
    assert r.status_code == 201 and Decimal(r.json()["minimum_spend_agreed"]) == Decimal(300)


async def test_a_cheaper_table_than_the_amount_shown_is_fine_and_records_its_own_amount(rest: Restaurant) -> None:
    r = await book(rest, at(rest.day, 19), 2, table_type="vip", accept_minimum_spend=False, accepted_minimum_spend=500)
    assert r.status_code == 201 and Decimal(r.json()["minimum_spend_agreed"]) == Decimal(200)
    combined = await book(rest, at(rest.day, 12), 6, table_type="vip", allow_separate_seating=True, accepted_minimum_spend="500.00")
    assert combined.status_code == 201 and Decimal(combined.json()["minimum_spend_agreed"]) == Decimal(500)  # the sum, exactly
    normal = await book(rest, at(rest.day, 19), 2, accepted_minimum_spend=0)
    assert normal.status_code == 201 and normal.json()["minimum_spend_agreed"] is None


@pytest.mark.parametrize("bad", [-1, "-0.01", "NaN", "Infinity", "lots"])
async def test_accepted_minimum_spend_must_be_a_real_amount(rest: Restaurant, bad: Any) -> None:
    r = await book(rest, at(rest.day, 19), 2, table_type="vip", accepted_minimum_spend=bad)
    assert r.status_code == 422 and "minimum_spend_not_accepted" not in r.text and "accepted_minimum_spend" in r.text, r.text


async def test_the_yes_alone_still_accepts_whatever_the_tables_need(rest: Restaurant) -> None:
    """Compatibility: a client that sends only ``accept_minimum_spend`` agrees to any amount, so it can still be seated in a dearer room
    than the one it showed. Clients should send ``accepted_minimum_spend``."""
    assert (await book(rest, at(rest.day, 19), 2, table_type="vip", accept_minimum_spend=True)).status_code == 201  # takes the 200 room
    r = await book(rest, at(rest.day, 19), 2, table_type="vip", accept_minimum_spend=True)
    assert r.status_code == 201 and Decimal(r.json()["minimum_spend_agreed"]) == Decimal(300)


async def test_a_lost_race_never_seats_the_loser_at_a_dearer_room(rest: Restaurant, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both customers are shown the 200 room and accept it. The tables are decided when each request arrives (one at a time, under the day
    lock), so the second finds only the 300 room: it is refused with the new amount, which the page can show and ask about, not booked."""
    import app.services.booking as booking_module

    start = at(rest.day, 19)
    shown = next(s for s in await slots(rest, 2, table_type="vip") if s["start_at"] == start.isoformat())
    assert Decimal(shown["minimum_spend"]) == Decimal(200)
    barrier = asyncio.Barrier(2)
    real = booking_module.get_allocator
    monkeypatch.setattr(booking_module, "get_allocator", lambda bt: _SyncStart(real(bt), barrier))
    results = await asyncio.gather(*[
        book(rest, start, 2, table_type="vip", accept_minimum_spend=True, accepted_minimum_spend=shown["minimum_spend"]) for _ in range(2)
    ])
    monkeypatch.undo()
    assert sorted(r.status_code for r in results) == [201, 422], [r.text for r in results]
    won = next(r.json() for r in results if r.status_code == 201)
    lost = next(r.json() for r in results if r.status_code == 422)
    assert Decimal(won["minimum_spend_agreed"]) == Decimal(200) and won["resource_ids"] == [str(rest.tables["v2"])]
    assert lost["error"] == "minimum_spend_not_accepted" and Decimal(lost["minimum_spend"]) == Decimal(300)
    assert Decimal(lost["accepted_minimum_spend"]) == Decimal(200)
    assert len((await rest.client.get("/v1/admin/bookings", headers=rest.admin)).json()) == 1
    again = await book(rest, start, 2, table_type="vip", accepted_minimum_spend=lost["minimum_spend"])  # the customer confirms the new amount
    assert again.status_code == 201 and Decimal(again.json()["minimum_spend_agreed"]) == Decimal(300)


async def test_tables_fill_smallest_first_then_overflow_upward(rest: Restaurant) -> None:
    start = at(rest.day, 19)
    got = [(await book(rest, start, 2)).json()["resource_ids"][0] for _ in range(4)]
    order = [k for g in got for k, v in rest.tables.items() if str(v) == g]
    assert sorted(order[:2]) == ["a2", "b2"] and order[2:] == ["c4", "d6"]
    assert (await book(rest, start, 2)).status_code == 409  # normal pool exhausted
    # VIP tables stay untouched
    assert {str(rest.tables["v4"]), str(rest.tables["v2"])}.isdisjoint(g for g in got)


async def test_open_table_rule_caps_concurrent_tables(rest: Restaurant) -> None:
    wd = weekday_index(rest.day)
    r = await rest.client.put("/v1/admin/table-open-rules", headers=rest.admin, json=[{"weekday": wd, "table_type": "normal", "open_tables": 2}])
    assert r.status_code == 200, r.text
    start = at(rest.day, 12)  # occupies 12:00-13:45 (90 min + 15 buffer)
    assert (await book(rest, start, 2)).status_code == 201
    assert (await book(rest, start, 2)).status_code == 201
    third = await book(rest, start, 2)
    assert third.status_code == 409  # 4 normal tables exist, but only 2 are open for reservation
    assert (await book(rest, at(rest.day, 13), 2)).status_code == 409  # still overlaps both earlier bookings
    # the cap limits simultaneous use, so a sitting that starts after those two have finished is fine
    assert (await book(rest, at(rest.day, 14), 2)).status_code == 201


async def test_open_table_rule_is_enforced_under_concurrency(rest: Restaurant) -> None:
    wd = weekday_index(rest.day)
    await rest.client.put("/v1/admin/table-open-rules", headers=rest.admin, json=[{"weekday": wd, "table_type": "normal", "open_tables": 2}])
    start = at(rest.day, 19)
    results = await asyncio.gather(*[book(rest, start, 2) for _ in range(8)])
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 201] + [409] * 6, codes  # never 3: the cap can't be raced past
    listing = (await rest.client.get("/v1/admin/bookings", headers=rest.admin)).json()
    assert len(listing) == 2


async def test_date_specific_rule_overrides_weekday_rule(rest: Restaurant) -> None:
    wd = weekday_index(rest.day)
    rules = [
        {"weekday": wd, "table_type": "normal", "open_tables": 4},
        {"specific_date": rest.day.isoformat(), "table_type": "normal", "open_tables": 1},
    ]
    assert (await rest.client.put("/v1/admin/table-open-rules", headers=rest.admin, json=rules)).status_code == 200
    start = at(rest.day, 19)
    assert (await book(rest, start, 2)).status_code == 201
    assert (await book(rest, start, 2)).status_code == 409
    other = rest.day + dt.timedelta(days=1)  # the weekday rule (or none) applies elsewhere
    assert (await book(rest, at(other, 19), 2)).status_code == 201
    assert (await book(rest, at(other, 19), 2)).status_code == 201


async def test_open_table_rule_validation(rest: Restaurant) -> None:
    put = lambda rules: rest.client.put("/v1/admin/table-open-rules", headers=rest.admin, json=rules)  # noqa: E731
    assert (await put([{"weekday": 1, "table_type": "normal", "open_tables": 5}])).status_code == 422  # only 4 normal tables
    assert (await put([{"weekday": 1, "table_type": "vip", "open_tables": 3}])).status_code == 422  # only 2 VIP tables
    assert (await put([{"table_type": "normal", "open_tables": 1}])).status_code == 422  # neither weekday nor date
    assert (await put([{"weekday": 1, "specific_date": "2031-01-01", "open_tables": 1}])).status_code == 422  # both
    dup = {"weekday": 1, "table_type": "normal", "open_tables": 1}
    assert (await put([dup, dup])).status_code == 422
    assert (await put([{"weekday": 1, "table_type": "normal", "open_tables": 4}])).status_code == 200


async def test_zero_open_tables_closes_online_reservations_for_that_day(rest: Restaurant) -> None:
    await rest.client.put("/v1/admin/table-open-rules", headers=rest.admin, json=[{"specific_date": rest.day.isoformat(), "table_type": "normal", "open_tables": 0}])
    assert await slots(rest, 2) == []


async def test_same_table_race_has_one_winner_and_no_orphans(rest: Restaurant) -> None:
    start = at(rest.day, 19)
    results = await asyncio.gather(*[book(rest, start, 6) for _ in range(6)])  # only the 6-top fits 6 singly
    assert sorted(r.status_code for r in results) == [201] + [409] * 5
    assert all(r.json()["error"] == "slot_unavailable" for r in results if r.status_code == 409)  # serialised, not raced
    assert len((await rest.client.get("/v1/admin/bookings", headers=rest.admin)).json()) == 1


async def test_overlapping_occupancy_blocks_the_table_but_adjacent_does_not(rest: Restaurant) -> None:
    assert (await book(rest, at(rest.day, 18), 6)).status_code == 201  # occupies 18:00-19:45
    assert (await book(rest, at(rest.day, 19), 6)).status_code == 409
    assert (await book(rest, at(rest.day, 19, 30), 6)).status_code == 409
    assert (await book(rest, at(rest.day, 19, 45), 6)).status_code == 409  # 19:45 is off the 30-minute grid
    assert (await book(rest, at(rest.day, 20), 6)).status_code == 201  # first grid slot after 18:00-19:45 is free


async def test_cancelling_frees_the_tables(rest: Restaurant) -> None:
    start = at(rest.day, 19)
    bid = (await book(rest, start, 6)).json()["id"]
    assert (await book(rest, start, 6)).status_code == 409
    assert (await rest.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=rest.admin)).status_code == 200
    assert (await book(rest, start, 6)).status_code == 201


async def test_pending_reservation_holds_its_table(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client, default_status="pending")
    start = at(rest.day, 19)
    r = await book(rest, start, 6)
    assert r.status_code == 201 and r.json()["status"] == "pending" and r.json()["hold_expires_at"]
    assert (await book(rest, start, 6)).status_code == 409


async def test_closed_days_holidays_and_table_leave(rest: Restaurant) -> None:
    hours = [{"weekday": d, "open_time": "11:00:00", "close_time": "22:00:00"} for d in range(7) if d != weekday_index(rest.day)]
    await rest.client.put("/v1/admin/operating-hours", json=hours, headers=rest.admin)
    assert await slots(rest, 2) == []  # not an operating day

    await rest.client.put("/v1/admin/operating-hours", json=[{"weekday": d, "open_time": "11:00:00", "close_time": "22:00:00"} for d in range(7)], headers=rest.admin)
    assert await slots(rest, 2)
    leave = {"resource_id": str(rest.tables["d6"]), "name": "Repair", "start_date": rest.day.isoformat(), "end_date": rest.day.isoformat()}
    assert (await rest.client.post("/v1/admin/days-off", json=leave, headers=rest.admin)).status_code == 201
    assert await slots(rest, 5) == []  # the only table that seats 5 is out of service
    holiday = {"name": "Closed", "start_date": rest.day.isoformat(), "end_date": rest.day.isoformat()}
    await rest.client.post("/v1/admin/days-off", json=holiday, headers=rest.admin)
    assert await slots(rest, 2) == []


async def test_input_validation_and_missing_configuration(client: httpx.AsyncClient, rest: Restaurant) -> None:
    r = await rest.client.get("/v1/public/availability", params={"date": rest.day.isoformat()}, headers=rest.pk)
    assert r.status_code == 422 and "party_size" in r.text  # party size is required
    bare = await make_restaurant(client, configure=False)
    r = await bare.client.get("/v1/public/availability", params={"date": bare.day.isoformat(), "party_size": 2}, headers=bare.pk)
    assert r.status_code == 422  # no policy / slot settings yet
    bad = await rest.client.post("/v1/admin/resources", headers=rest.admin, json={"type": "table", "name": "x"})
    assert bad.status_code == 422  # tables need a capacity
    bad = await rest.client.post("/v1/admin/resources", headers=rest.admin, json={"type": "table", "name": "x", "capacity": 2, "attributes": {"minimum_spend": 50}})
    assert bad.status_code == 422  # minimum spend only makes sense on VIP tables


async def test_special_request_and_admin_filters(rest: Restaurant) -> None:
    r = await book(rest, at(rest.day, 19), 2, special_request="Window seat, birthday")
    assert r.status_code == 201
    await book(rest, at(rest.day, 19), 3, table_type="vip", accept_minimum_spend=True)
    ls = lambda **p: rest.client.get("/v1/admin/bookings", params=p, headers=rest.admin)  # noqa: E731
    assert len((await ls()).json()) == 2
    normal = (await ls(table_type="normal")).json()
    assert len(normal) == 1 and normal[0]["special_request"] == "Window seat, birthday" and normal[0]["party_size"] == 2
    assert len((await ls(table_type="vip")).json()) == 1


class _SyncStart:
    """Wraps the allocator so every request reaches the table decision at the same moment."""

    def __init__(self, inner: Any, barrier: asyncio.Barrier) -> None:
        self.inner, self.barrier = inner, barrier

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)  # everything except allocate is the real allocator's

    async def allocate(self, *a: Any, **k: Any) -> Any:
        await asyncio.wait_for(self.barrier.wait(), timeout=10)
        return await self.inner.allocate(*a, **k)


async def test_open_table_cap_cannot_be_raced_by_parties_choosing_different_tables(
    rest: Restaurant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parties of 2, 3 and 5 pick three *different* tables, so the per-table exclusion constraint cannot
    stop them. Only the per-day serialisation keeps the cap of 1 open table from being exceeded."""
    import app.services.booking as booking_module

    await rest.client.put("/v1/admin/table-open-rules", headers=rest.admin, json=[{"weekday": weekday_index(rest.day), "table_type": "normal", "open_tables": 1}])
    barrier = asyncio.Barrier(3)
    real = booking_module.get_allocator
    monkeypatch.setattr(booking_module, "get_allocator", lambda bt: _SyncStart(real(bt), barrier))

    results = await asyncio.gather(*[book(rest, at(rest.day, 19), party) for party in (2, 3, 5)])
    assert sorted(r.status_code for r in results) == [201, 409, 409], [r.text for r in results]
    monkeypatch.undo()
    assert len((await rest.client.get("/v1/admin/bookings", headers=rest.admin)).json()) == 1


async def test_table_selection_is_off_by_default_and_resource_id_is_refused(rest: Restaurant) -> None:
    """Blueprint §7.3: the system assigns tables. A supplied resource_id is refused, not silently ignored."""
    table = str(rest.tables["d6"])
    r = await book(rest, at(rest.day, 19), 2, resource_id=table)
    assert r.status_code == 422 and "assigned automatically" in r.json()["detail"]
    r = await rest.client.get(
        "/v1/public/availability", params={"date": rest.day.isoformat(), "party_size": 2, "resource_id": table}, headers=rest.pk
    )
    assert r.status_code == 422
    assert (await rest.client.get("/v1/admin/bookings", headers=rest.admin)).json() == []  # nothing was booked


# --------------------------------------------------------------------- per-tenant table selection
async def _enable_selection(rest: Restaurant, on: bool = True) -> None:
    await put_slots(rest, allow_table_selection=on)


async def test_selection_setting_defaults_to_off(rest: Restaurant) -> None:
    got = (await rest.client.get("/v1/admin/time-slot-settings", headers=rest.admin)).json()
    assert got["allow_table_selection"] is False


async def test_customer_gets_the_table_they_chose_when_enabled(rest: Restaurant) -> None:
    await _enable_selection(rest)
    six = str(rest.tables["d6"])
    offered = await slots(rest, 2, resource_id=six)
    assert offered and all(s["resource_ids"] == [six] for s in offered)  # not the smaller table auto-assign would use
    r = await book(rest, at(rest.day, 19), 2, resource_id=six)
    assert r.status_code == 201 and r.json()["resource_ids"] == [six]


async def test_chosen_table_that_is_taken_is_refused_while_others_stay_bookable(rest: Restaurant) -> None:
    await _enable_selection(rest)
    six, start = str(rest.tables["d6"]), at(rest.day, 19)
    assert (await book(rest, start, 4, resource_id=six)).status_code == 201
    again = await book(rest, start, 2, resource_id=six)
    assert again.status_code == 409 and again.json()["error"] == "slot_unavailable"
    assert "19:00" not in hhmm(await slots(rest, 2, resource_id=six))
    assert (await book(rest, start, 2, resource_id=str(rest.tables["c4"]))).status_code == 201  # a different table is fine
    assert (await book(rest, start, 2)).status_code == 201  # and auto-assign still works alongside


async def test_chosen_table_must_be_large_enough(rest: Restaurant) -> None:
    await _enable_selection(rest)
    small = str(rest.tables["a2"])
    assert await slots(rest, 5, resource_id=small) == []
    r = await book(rest, at(rest.day, 19), 5, resource_id=small)
    assert r.status_code == 409 and r.json()["error"] == "slot_unavailable"


async def test_chosen_table_must_match_the_seating_class(rest: Restaurant) -> None:
    await _enable_selection(rest)
    normal, vip = str(rest.tables["a2"]), str(rest.tables["v4"])
    assert (await book(rest, at(rest.day, 19), 2, resource_id=vip)).status_code == 422  # VIP table, normal request
    assert (await book(rest, at(rest.day, 19), 2, resource_id=normal, table_type="vip")).status_code == 422
    # a chosen VIP table still needs the minimum-spend agreement
    r = await book(rest, at(rest.day, 19), 3, resource_id=vip, table_type="vip")
    assert r.status_code == 422 and r.json()["error"] == "minimum_spend_not_accepted"
    r = await book(rest, at(rest.day, 19), 3, resource_id=vip, table_type="vip", accept_minimum_spend=True)
    assert r.status_code == 201 and r.json()["resource_ids"] == [vip] and Decimal(r.json()["minimum_spend_agreed"]) == Decimal(300)


async def test_unknown_table_and_combination_requests_are_rejected(rest: Restaurant) -> None:
    await _enable_selection(rest)
    assert (await book(rest, at(rest.day, 19), 2, resource_id=str(uuid.uuid4()))).status_code == 404
    r = await book(rest, at(rest.day, 19), 2, resource_id=str(rest.tables["d6"]), allow_separate_seating=True)
    assert r.status_code == 422  # a chosen table is never combined with others


async def test_chosen_table_out_of_service_has_no_slots(rest: Restaurant) -> None:
    await _enable_selection(rest)
    six = rest.tables["d6"]
    leave = {"resource_id": str(six), "name": "Repair", "start_date": rest.day.isoformat(), "end_date": rest.day.isoformat()}
    assert (await rest.client.post("/v1/admin/days-off", json=leave, headers=rest.admin)).status_code == 201
    assert await slots(rest, 2, resource_id=str(six)) == []
    assert await slots(rest, 2)  # other tables unaffected


async def test_open_table_cap_still_applies_to_chosen_tables(rest: Restaurant) -> None:
    await _enable_selection(rest)
    await rest.client.put("/v1/admin/table-open-rules", headers=rest.admin, json=[{"weekday": weekday_index(rest.day), "table_type": "normal", "open_tables": 1}])
    start = at(rest.day, 19)
    assert (await book(rest, start, 2, resource_id=str(rest.tables["a2"]))).status_code == 201
    assert (await book(rest, start, 2, resource_id=str(rest.tables["b2"]))).status_code == 409


async def test_concurrent_requests_for_the_same_chosen_table_have_one_winner(rest: Restaurant) -> None:
    await _enable_selection(rest)
    table, start = str(rest.tables["c4"]), at(rest.day, 19)
    results = await asyncio.gather(*[book(rest, start, 3, resource_id=table) for _ in range(6)])
    assert sorted(r.status_code for r in results) == [201] + [409] * 5
    assert len((await rest.client.get("/v1/admin/bookings", headers=rest.admin)).json()) == 1


async def test_switching_the_setting_off_again_restores_auto_assignment(rest: Restaurant) -> None:
    await _enable_selection(rest, True)
    six = str(rest.tables["d6"])
    assert (await book(rest, at(rest.day, 12), 2, resource_id=six)).status_code == 201
    await _enable_selection(rest, False)
    r = await book(rest, at(rest.day, 14), 2, resource_id=six)
    assert r.status_code == 422 and "assigned automatically" in r.json()["detail"]
    assert (await book(rest, at(rest.day, 14), 2)).status_code == 201  # system assignment unaffected
