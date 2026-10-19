"""Review flags (blueprint §7.3 / §8.3 / §13.5): a settings, shift or service change flags upcoming bookings
that no longer fit - it never cancels, moves or releases anything."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import Range

from app.core.database import tenant_session
from app.core.exceptions import PostgisUnavailable
from app.models import Booking, BookingAllocation
from app.schemas.public import BookingConfirmation, CustomerBookingOut
from app.services import flagging
from app.services.availability import weekday_index
from tests.conftest import TZ, Seed, allocations, at, make_seed
from tests.test_automotive import add_bay, add_service as add_garage_service, gbook, make_garage
from tests.test_field_service import KL, NEAR, SUBANG, make_practice, pbook, pin
from tests.test_restaurant import add_table, book as rbook, make_restaurant, put_slots

HEADER = "X-Bookings-Flagged"


# --------------------------------------------------------------------- pure rules
def _ctx(**over: Any) -> flagging.Context:
    return flagging.Context(tenant=SimpleNamespace(business_type="restaurant", timezone="Asia/Kuala_Lumpur"), tz=TZ, **over)  # type: ignore[arg-type]


def _table_booking(tables: list[uuid.UUID], start: dt.datetime, end: dt.datetime, table_type: str = "normal") -> Booking:
    return Booking(
        id=uuid.uuid4(), table_type=table_type, start_at=start, end_at=end, status="confirmed",
        allocations=[BookingAllocation(resource_id=t, period=Range(start, end, bounds="[)"), state="active") for t in tables],
    )


def _rule(open_tables: int, *, weekday: int | None = None, day: dt.date | None = None, table_type: str = "normal") -> Any:
    return SimpleNamespace(table_type=table_type, weekday=weekday, specific_date=day, open_tables=open_tables)


D = dt.date(2030, 1, 7)  # a Monday


def _cap_ctx(tables: list[uuid.UUID], *rules: Any) -> flagging.Context:
    return _ctx(resources={t: SimpleNamespace(type="table") for t in tables}, table_rules=list(rules))  # type: ignore[arg-type]


def test_table_cap_flags_everyone_seated_at_an_over_cap_moment() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    one, two, three = (_table_booking([t], at(D, 19), at(D, 20, 45)) for t in (a, b, c))
    ctx = _cap_ctx([a, b, c], _rule(2, weekday=weekday_index(D)))
    assert flagging.table_cap_exceeded(ctx, [one, two]) == set()  # exactly at the cap is fine
    assert flagging.table_cap_exceeded(ctx, [one, two, three]) == {one.id, two.id, three.id}


def test_bookings_that_only_touch_never_count_as_simultaneous() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    early = _table_booking([a], at(D, 17), at(D, 19))
    late = _table_booking([b], at(D, 19), at(D, 21))  # starts the instant the first one ends
    assert flagging.table_cap_exceeded(_cap_ctx([a, b], _rule(1, weekday=weekday_index(D))), [early, late]) == set()


def test_a_combined_party_counts_every_table_it_occupies() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    party = _table_booking([a, b], at(D, 19), at(D, 20, 45))
    ctx = _cap_ctx([a, b], _rule(1, weekday=weekday_index(D)))
    assert flagging.table_cap_exceeded(ctx, [party]) == {party.id}


def test_no_rule_means_uncapped_and_classes_are_counted_separately() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    normal = _table_booking([a], at(D, 19), at(D, 20, 45))
    vip = _table_booking([b], at(D, 19), at(D, 20, 45), table_type="vip")
    assert flagging.table_cap_exceeded(_cap_ctx([a, b]), [normal, vip]) == set()
    capped = _cap_ctx([a, b], _rule(1, weekday=weekday_index(D)))  # the cap is for normal tables only
    assert flagging.table_cap_exceeded(capped, [normal, vip]) == set()


def test_a_date_rule_beats_the_weekday_rule() -> None:
    ctx = _cap_ctx([], _rule(1, weekday=weekday_index(D)), _rule(5, day=D))
    assert flagging._cap_for(ctx, D, "normal") == 5
    assert flagging._cap_for(ctx, D + dt.timedelta(days=7), "normal") == 1
    assert flagging._cap_for(ctx, D, "vip") is None


def test_only_reservations_still_ahead_are_examined() -> None:
    r1, r2, r3 = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    now = at(D, 12)
    done = BookingAllocation(resource_id=r1, period=Range(at(D, 9), at(D, 11), bounds="[)"), state="active")  # work already done
    ahead = BookingAllocation(resource_id=r2, period=Range(at(D, 14), at(D, 16), bounds="[)"), state="active")
    released = BookingAllocation(resource_id=r3, period=Range(at(D, 15), at(D, 17), bounds="[)"), state="released")
    running = BookingAllocation(resource_id=r1, period=Range(at(D, 11), at(D, 13), bounds="[)"), state="active")  # started, not over
    b = Booking(id=uuid.uuid4(), start_at=at(D, 9), end_at=at(D, 17), allocations=[done, ahead, released, running])
    assert [sp.resource_id for sp in flagging._spans(b, now)] == [r2, r1]


def test_every_reason_has_a_check_and_an_explanation() -> None:
    assert set(flagging.CHECKS) == set(flagging.FLAG_REASONS) and all(flagging.FLAG_REASONS.values())


def test_customers_never_see_review_flags() -> None:
    assert "flags" not in CustomerBookingOut.model_fields and "flags" not in BookingConfirmation.model_fields


# --------------------------------------------------------------------- helpers
async def book(s: Seed, staff: uuid.UUID | None, start: dt.datetime, **extra: Any) -> str:
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(start, staff, **extra), headers=s.pk)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def get(s: Seed, bid: str) -> dict[str, Any]:
    r = await s.client.get(f"/v1/admin/bookings/{bid}", headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def flags(s: Seed, bid: str) -> list[str]:
    return list((await get(s, bid))["flags"])


async def put_shifts(s: Seed, staff: uuid.UUID, *, start: str = "09:00:00", end: str = "18:00:00",
                     brk: tuple[str, str] | None = ("13:00:00", "14:00:00")) -> httpx.Response:
    body = [
        {"weekday": d, "start_time": start, "end_time": end, **({"break_start": brk[0], "break_end": brk[1]} if brk else {})}
        for d in range(7)
    ]
    r = await s.client.put(f"/v1/admin/resources/{staff}/shifts", json=body, headers=s.admin)
    assert r.status_code == 200, r.text
    return r


async def put_hours(s: Seed, open_: str, close: str) -> httpx.Response:
    body = [{"weekday": d, "open_time": open_, "close_time": close} for d in range(7)]
    r = await s.client.put("/v1/admin/operating-hours", json=body, headers=s.admin)
    assert r.status_code == 200, r.text
    return r


async def day_off(s: Seed, day: dt.date, resource: uuid.UUID | None = None) -> httpx.Response:
    body: dict[str, Any] = {"name": "Off", "start_date": day.isoformat(), "end_date": day.isoformat()}
    if resource:
        body["resource_id"] = str(resource)
    r = await s.client.post("/v1/admin/days-off", json=body, headers=s.admin)
    assert r.status_code == 201, r.text
    return r


# --------------------------------------------------------------------- shifts
async def test_a_shift_edit_flags_only_the_bookings_it_no_longer_covers_and_cancels_nothing(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    morning, afternoon, other = (await book(seeded, mei, at(seeded.day, 10)), await book(seeded, mei, at(seeded.day, 15)),
                                 await book(seeded, lin, at(seeded.day, 10)))
    r = await put_shifts(seeded, mei, start="12:00:00", brk=None)  # Mei now starts at noon
    assert r.headers[HEADER] == "1"
    assert await flags(seeded, morning) == ["outside_shift"]
    assert await flags(seeded, afternoon) == [] and await flags(seeded, other) == []
    d = await get(seeded, morning)  # flagged, not touched
    assert d["status"] == "confirmed" and [a[4] for a in await allocations(seeded, morning)] == ["active"]
    assert (dt.datetime.fromisoformat(d["start_at"]), d["resource_ids"]) == (at(seeded.day, 10), [str(mei)])


async def test_undoing_the_change_lifts_its_own_flag(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await put_shifts(seeded, mei, start="12:00:00", brk=None)
    assert await flags(seeded, bid) == ["outside_shift"]
    r = await put_shifts(seeded, mei)  # back to 09:00-18:00
    assert r.headers[HEADER] == "0" and await flags(seeded, bid) == []


async def test_moving_a_break_over_a_booking_flags_it(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    at_ten, at_three = await book(seeded, mei, at(seeded.day, 10)), await book(seeded, mei, at(seeded.day, 15))
    await put_shifts(seeded, mei, brk=("10:00:00", "11:00:00"))
    assert await flags(seeded, at_ten) == ["outside_shift"] and await flags(seeded, at_three) == []


async def test_a_shift_edit_only_rechecks_the_bookings_of_that_resource(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await put_shifts(seeded, mei, start="12:00:00", brk=None)
    await seeded.client.post(f"/v1/admin/bookings/{bid}/clear-flags", headers=seeded.admin)  # a manager looked and accepted it
    r = await put_shifts(seeded, lin, start="12:00:00", brk=None)  # Mei's booking is still outside Mei's shift, but this is Lin's edit
    assert r.headers[HEADER] == "0" and await flags(seeded, bid) == []


# --------------------------------------------------------------------- days off
async def test_a_personal_day_off_flags_that_persons_bookings_on_those_days_only(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    tomorrow = seeded.day + dt.timedelta(days=1)
    hers, his, later = (await book(seeded, mei, at(seeded.day, 10)), await book(seeded, lin, at(seeded.day, 10)),
                        await book(seeded, mei, at(tomorrow, 10)))
    r = await day_off(seeded, seeded.day, lin)
    assert r.headers[HEADER] == "1"
    assert await flags(seeded, his) == ["day_off"]
    assert await flags(seeded, hers) == [] and await flags(seeded, later) == []
    r = await day_off(seeded, tomorrow, mei)  # Lin's leave on the 1st is not Mei's, even though Mei works that day too
    assert r.headers[HEADER] == "1"
    assert await flags(seeded, later) == ["day_off"] and await flags(seeded, hers) == [] and await flags(seeded, his) == ["day_off"]


async def test_a_company_holiday_flags_everyones_bookings_that_day(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    tomorrow = seeded.day + dt.timedelta(days=1)
    ids = [await book(seeded, mei, at(seeded.day, 10)), await book(seeded, lin, at(seeded.day, 11))]
    spared = await book(seeded, mei, at(tomorrow, 10))
    r = await day_off(seeded, seeded.day)
    assert r.headers[HEADER] == "2"
    assert [await flags(seeded, i) for i in ids] == [["day_off"], ["day_off"]] and await flags(seeded, spared) == []


# --------------------------------------------------------------------- operating hours
async def test_new_opening_hours_flag_bookings_outside_them_and_removing_the_limit_lifts_them(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    inside, outside = await book(seeded, mei, at(seeded.day, 10)), await book(seeded, mei, at(seeded.day, 16))
    r = await put_hours(seeded, "09:00:00", "15:00:00")
    assert r.headers[HEADER] == "1"
    assert await flags(seeded, inside) == [] and await flags(seeded, outside) == ["outside_operating_hours"]
    assert (await seeded.client.put("/v1/admin/operating-hours", json=[], headers=seeded.admin)).headers[HEADER] == "0"
    assert await flags(seeded, outside) == []  # no hours configured = unrestricted


# --------------------------------------------------------------------- staff qualification
async def test_changing_who_can_do_a_service_flags_bookings_with_someone_no_longer_qualified(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    svc = f"/v1/admin/services/{seeded.service_id}"
    assert (await seeded.client.patch(svc, json={"resource_ids": [str(mei)]}, headers=seeded.admin)).status_code == 200
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await seeded.client.patch(svc, json={"resource_ids": [str(lin)]}, headers=seeded.admin)
    assert r.headers[HEADER] == "1" and await flags(seeded, bid) == ["staff_not_qualified"]
    r = await seeded.client.patch(svc, json={"resource_ids": [str(mei), str(lin)]}, headers=seeded.admin)
    assert r.headers[HEADER] == "0" and await flags(seeded, bid) == []


async def test_a_service_edit_only_rechecks_bookings_that_include_that_service(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    r = await seeded.client.post("/v1/admin/services", json={"name": "Facial", "duration_minutes": 30, "price": "80.00"}, headers=seeded.admin)
    facial = r.json()["id"]
    massage = f"/v1/admin/services/{seeded.service_id}"
    await seeded.client.patch(massage, json={"resource_ids": [str(mei)]}, headers=seeded.admin)
    bid = await book(seeded, mei, at(seeded.day, 10))
    await seeded.client.patch(massage, json={"resource_ids": [str(lin)]}, headers=seeded.admin)
    assert await flags(seeded, bid) == ["staff_not_qualified"]
    await seeded.client.post(f"/v1/admin/bookings/{bid}/clear-flags", headers=seeded.admin)
    r = await seeded.client.patch(f"/v1/admin/services/{facial}", json={"resource_ids": [str(lin)]}, headers=seeded.admin)
    assert r.headers[HEADER] == "0" and await flags(seeded, bid) == []  # the massage booking is not the facial's business


async def test_other_service_edits_do_not_flag_existing_bookings(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    svc = f"/v1/admin/services/{seeded.service_id}"
    r = await seeded.client.patch(svc, json={"duration_minutes": 120, "price": "300.00", "active": False}, headers=seeded.admin)
    assert r.status_code == 200 and HEADER not in r.headers  # the booking keeps the duration and price it was made with
    assert await flags(seeded, bid) == []


# --------------------------------------------------------------------- what is deliberately not flagged
async def test_a_new_policy_never_flags_existing_bookings(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    snapshot = (await get(seeded, bid))["policy_snapshot"]
    body = {"name": "Strict", "default_status": "confirmed", "min_notice_booking": 60, "min_notice_cancel": 30 * 86400,
            "booking_window": 86400, "pending_timeout": 60, "make_default": True}
    assert (await seeded.client.post("/v1/admin/policies", json=body, headers=seeded.admin)).status_code == 201
    d = await get(seeded, bid)
    assert d["flags"] == [] and d["policy_snapshot"] == snapshot  # snapshotted rules are untouched (blueprint §6)


async def test_finished_and_expired_bookings_are_not_examined(client: httpx.AsyncClient) -> None:
    s = await make_seed(client, default_status="pending")
    mei = await s.add_staff("Mei")
    live, cancelled, expired = (await book(s, mei, at(s.day, 10)), await book(s, mei, at(s.day, 11)), await book(s, mei, at(s.day, 12)))
    assert (await s.client.patch(f"/v1/admin/bookings/{cancelled}/status", json={"status": "cancelled"}, headers=s.admin)).status_code == 200
    async with tenant_session(s.tenant_id) as db:  # the hold ran out but no sweep has released it yet
        await db.execute(update(Booking).where(Booking.id == uuid.UUID(expired)).values(hold_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5)))
    r = await day_off(s, s.day)
    assert r.headers[HEADER] == "1"
    assert await flags(s, live) == ["day_off"]  # a pending booking that still holds its slot is flagged
    assert await flags(s, cancelled) == [] and await flags(s, expired) == []


async def test_leaving_the_live_states_drops_the_flags(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await day_off(seeded, seeded.day)
    assert await flags(seeded, bid) == ["day_off"]
    r = await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=seeded.admin)
    assert r.status_code == 200 and r.json()["flags"] == []


# --------------------------------------------------------------------- reviewing flags
async def test_a_manager_can_dismiss_flags_and_the_list_can_be_filtered_by_them(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    inside, outside = await book(seeded, mei, at(seeded.day, 10)), await book(seeded, mei, at(seeded.day, 16))
    await put_hours(seeded, "09:00:00", "15:00:00")  # 16:00 is now outside the opening hours
    listing = lambda **p: seeded.client.get("/v1/admin/bookings", params=p, headers=seeded.admin)  # noqa: E731
    assert [b["id"] for b in (await listing(flagged="true")).json()] == [outside]
    assert [b["id"] for b in (await listing(flagged="false")).json()] == [inside]
    assert [b["id"] for b in (await listing(flag="outside_operating_hours")).json()] == [outside]
    assert (await listing(flag="not_a_reason")).status_code == 422

    staff_token = await seeded.token("staff")
    denied = await seeded.client.post(f"/v1/admin/bookings/{outside}/clear-flags", headers=staff_token)
    assert denied.status_code == 403 and await flags(seeded, outside) == ["outside_operating_hours"]
    ok = await seeded.client.post(f"/v1/admin/bookings/{outside}/clear-flags", headers=seeded.admin)
    assert ok.status_code == 200 and ok.json()["flags"] == []
    assert (await seeded.client.post(f"/v1/admin/bookings/{uuid.uuid4()}/clear-flags", headers=seeded.admin)).status_code == 404


async def test_a_dismissed_flag_stays_dismissed_until_something_touches_it_again(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 16))
    await put_hours(seeded, "09:00:00", "15:00:00")
    await seeded.client.post(f"/v1/admin/bookings/{bid}/clear-flags", headers=seeded.admin)
    await put_shifts(seeded, mei)  # unrelated change: only shifts are re-checked
    assert await flags(seeded, bid) == []
    await put_hours(seeded, "09:00:00", "15:00:00")  # the same subject changes again
    assert await flags(seeded, bid) == ["outside_operating_hours"]


async def test_flagging_is_tenant_scoped(client: httpx.AsyncClient) -> None:
    ours, theirs = await make_seed(client), await make_seed(client)
    ours_id = await book(ours, await ours.add_staff("Mei"), at(ours.day, 10))
    theirs_id = await book(theirs, await theirs.add_staff("Mei"), at(theirs.day, 10))
    await day_off(ours, ours.day)
    assert await flags(ours, ours_id) == ["day_off"] and await flags(theirs, theirs_id) == []


# --------------------------------------------------------------------- concurrent changes
class _Meet:
    """Lets two racers arrive together; if the other cannot arrive (it is queued behind a row lock) it moves on."""

    def __init__(self, n: int, patience: float = 1.5) -> None:
        self.n, self.patience, self.arrived, self.event = n, patience, 0, asyncio.Event()

    def wrap(self, real: Any) -> Any:
        async def racer(*a: Any, **k: Any) -> Any:
            self.arrived += 1
            if self.arrived >= self.n:
                self.event.set()
            try:
                await asyncio.wait_for(self.event.wait(), timeout=self.patience)
            except TimeoutError:
                pass
            return await real(*a, **k)

        return racer


async def test_two_administrators_changing_settings_at_once_keep_both_flags(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    with monkeypatch.context() as m:
        m.setattr(flagging, "load_context", _Meet(2).wrap(flagging.load_context))
        shifts, hours = await asyncio.gather(
            put_shifts(seeded, mei, start="12:00:00", brk=None),
            seeded.client.put("/v1/admin/operating-hours", json=[{"weekday": d, "open_time": "12:00:00", "close_time": "18:00:00"} for d in range(7)], headers=seeded.admin),
        )
    assert shifts.status_code == 200 and hours.status_code == 200
    assert sorted(await flags(seeded, bid)) == ["outside_operating_hours", "outside_shift"]  # neither overwrote the other


# --------------------------------------------------------------------- restaurant
async def _table_of(rest: Seed, bid: str) -> str:
    return str((await get(rest, bid))["resource_ids"][0])


async def test_restaurant_seatings_are_judged_on_their_duration_not_the_buffer_after_it(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)  # 11:00-22:00, 90-minute seatings + 15 minutes to reset the table
    late = (await rbook(rest, at(rest.day, 20, 30), 2)).json()["id"]  # seated 20:30-22:00, table free again at 22:15
    noon = (await rbook(rest, at(rest.day, 12), 2)).json()["id"]
    r = await put_hours(rest, "11:00:00", "22:00:00")  # the same hours again
    assert r.headers[HEADER] == "0" and await flags(rest, late) == []  # the reset time running past closing is not a conflict
    r = await put_hours(rest, "11:00:00", "21:30:00")  # closing earlier now cuts the 20:30 seating short
    assert r.headers[HEADER] == "1"
    assert await flags(rest, late) == ["outside_operating_hours"] and await flags(rest, noon) == []
    await put_hours(rest, "11:00:00", "22:00:00")
    assert await flags(rest, late) == []


async def test_a_restaurant_with_no_opening_hours_can_seat_nobody(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    bid = (await rbook(rest, at(rest.day, 19), 2)).json()["id"]
    assert (await rest.client.put("/v1/admin/operating-hours", json=[], headers=rest.admin)).headers[HEADER] == "1"
    assert await flags(rest, bid) == ["outside_operating_hours"]


async def test_slot_rule_changes_flag_reservations_that_no_longer_fit(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    ids = {h: (await rbook(rest, at(rest.day, h, m), 2)).json()["id"] for h, m in [(12, 0), (19, 0), (20, 30)]}
    r = await rest.client.put(
        "/v1/admin/time-slot-settings", headers=rest.admin,
        json={"booking_mode": "fixed", "slot_duration_minutes": 90, "slot_interval_minutes": 30,
              "first_slot_time": "18:00:00", "last_slot_time": "20:00:00", "buffer_minutes": 15},
    )
    assert r.status_code == 200 and r.headers[HEADER] == "2"  # fixed mode: only 18:00-20:00 starts are accepted now
    assert await flags(rest, ids[12]) == ["outside_slot_times"] and await flags(rest, ids[20]) == ["outside_slot_times"]
    assert await flags(rest, ids[19]) == []
    await put_slots(rest)  # back to flexible
    assert [await flags(rest, i) for i in ids.values()] == [[], [], []]
    r = await rest.client.put(
        "/v1/admin/time-slot-settings", headers=rest.admin,
        json={"booking_mode": "flexible", "slot_duration_minutes": 150, "slot_interval_minutes": 30,
              "first_slot_time": "11:00:00", "last_slot_time": "20:30:00", "buffer_minutes": 15},
    )
    assert r.headers[HEADER] == "1"  # a 150-minute seating from 20:30 would run to 23:00, past closing
    assert await flags(rest, ids[20]) == ["outside_operating_hours"] and await flags(rest, ids[19]) == []


async def test_lowering_the_open_table_cap_flags_the_overlapping_reservations(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    evening = [(await rbook(rest, at(rest.day, 19), 2)).json()["id"] for _ in range(2)]  # a2 and b2, same time
    lunch = (await rbook(rest, at(rest.day, 12), 2)).json()["id"]
    vip = (await rbook(rest, at(rest.day, 19), 2, table_type="vip", accept_minimum_spend=True)).json()["id"]
    wd = weekday_index(rest.day)
    put = lambda *rules: rest.client.put("/v1/admin/table-open-rules", json=list(rules), headers=rest.admin)  # noqa: E731
    r = await put({"weekday": wd, "table_type": "normal", "open_tables": 1})
    assert r.status_code == 200 and r.headers[HEADER] == "2"
    assert [await flags(rest, i) for i in evening] == [["table_cap_exceeded"]] * 2
    assert await flags(rest, lunch) == [] and await flags(rest, vip) == []  # a lone lunch and the VIP class are within their caps
    r = await put({"weekday": wd, "table_type": "normal", "open_tables": 1}, {"specific_date": rest.day.isoformat(), "table_type": "normal", "open_tables": 2})
    assert r.headers[HEADER] == "0" and [await flags(rest, i) for i in evening] == [[], []]  # the date rule beats the weekday rule
    r = await put()
    assert r.status_code == 200 and r.headers[HEADER] == "0"  # no rule: nothing is capped


async def test_a_table_taken_out_of_service_flags_only_its_own_reservation(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    first, second = [(await rbook(rest, at(rest.day, 19), 2)).json()["id"] for _ in range(2)]
    broken = await _table_of(rest, second)
    r = await day_off(rest, rest.day, uuid.UUID(broken))
    assert r.headers[HEADER] == "1"
    assert await flags(rest, second) == ["day_off"] and await flags(rest, first) == []


# --------------------------------------------------------------------- automotive
async def test_a_mechanics_shift_and_a_bays_leave_are_judged_separately(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    bid = (await gbook(g, at(g.day, 10), ["brake"])).json()["id"]
    d = await get(g, bid)
    mechanic = next(uuid.UUID(r) for r in d["resource_ids"] if uuid.UUID(r) in g.mechanics.values())
    bay = next(uuid.UUID(r) for r in d["resource_ids"] if uuid.UUID(r) in g.bays.values())
    r = await put_shifts(g, mechanic, start="12:00:00", brk=None)
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["outside_shift"]
    await put_shifts(g, mechanic)
    assert await flags(g, bid) == []
    r = await day_off(g, g.day, bay)  # bays have no shifts, but they can be closed for a day
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["day_off"]


async def test_a_bay_that_can_no_longer_do_the_service_is_flagged_though_every_mechanic_still_can(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    tyres = await add_garage_service(g, "Tyres", 60, resource_ids=[g.bays["Bay 1"]])  # only Bay 1 supports it; any mechanic may do it
    g.svc["tyres"] = tyres
    bid = (await gbook(g, at(g.day, 10), ["tyres"])).json()["id"]
    assert str(g.bays["Bay 1"]) in (await get(g, bid))["resource_ids"]
    svc = f"/v1/admin/services/{tyres}"
    r = await g.client.patch(svc, json={"resource_ids": [str(g.bays["Bay 2"])]}, headers=g.admin)
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]
    r = await g.client.patch(svc, json={"resource_ids": []}, headers=g.admin)  # no links: every bay qualifies again
    assert r.headers[HEADER] == "0" and await flags(g, bid) == []


async def test_a_resumed_job_is_judged_on_its_new_slot_not_the_segments_it_gave_up(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    bid = (await gbook(g, at(g.day, 10), ["brake"])).json()["id"]  # Ali or Bala, 10:00-12:00
    d = await get(g, bid)
    mechanic = next(uuid.UUID(r) for r in d["resource_ids"] if uuid.UUID(r) in g.mechanics.values())
    status = lambda new: g.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": new}, headers=g.admin)  # noqa: E731
    assert (await status("in-progress")).status_code == 200 and (await status("waiting-for-parts")).status_code == 200
    r = await g.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": at(g.day, 15).isoformat(), "resource_id": str(mechanic)}, headers=g.admin)
    assert r.status_code == 200, r.text  # the 10:00 segments are released; the job now runs at 15:00
    assert any(row[4] == "released" for row in await allocations(g, bid))
    r = await put_shifts(g, mechanic, start="12:00:00", brk=None)  # the old 10:00 slot is outside the new shift, the 15:00 one is not
    assert r.headers[HEADER] == "0" and await flags(g, bid) == []
    r = await put_shifts(g, mechanic, end="14:00:00", brk=None)  # now the 15:00 slot is the problem
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["outside_shift"]


# --------------------------------------------------------------------- field service (PostGIS)
@pytest.mark.usefixtures("postgis")
async def test_shrinking_the_service_radius_flags_on_site_visits_that_are_now_too_far(client: httpx.AsyncClient) -> None:
    p = await make_practice(client)  # 25 km radius around KL
    far = (await pbook(p, at(p.day, 10), "site", address=pin(SUBANG))).json()["id"]  # ~20 km
    near = (await pbook(p, at(p.day, 15), "site", address=pin(NEAR))).json()["id"]  # ~3 km
    office = (await pbook(p, at(p.day, 12), "office")).json()["id"]
    put = lambda km: p.client.put("/v1/admin/business-location", json={"latitude": KL[0], "longitude": KL[1], "service_radius_km": km}, headers=p.admin)  # noqa: E731
    r = await put(10)
    assert r.status_code == 200 and r.headers[HEADER] == "1"
    assert await flags(p, far) == ["outside_service_area"]
    assert await flags(p, near) == [] and await flags(p, office) == []
    assert (await put(25)).headers[HEADER] == "0" and await flags(p, far) == []


@pytest.mark.usefixtures("postgis")
async def test_when_postgis_cannot_answer_existing_flags_are_left_alone(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    p = await make_practice(client)
    far = (await pbook(p, at(p.day, 10), "site", address=pin(SUBANG))).json()["id"]
    put = lambda km: p.client.put("/v1/admin/business-location", json={"latitude": KL[0], "longitude": KL[1], "service_radius_km": km}, headers=p.admin)  # noqa: E731
    await put(10)
    assert await flags(p, far) == ["outside_service_area"]

    async def broken(*a: Any, **k: Any) -> Any:
        raise PostgisUnavailable("no PostGIS")

    monkeypatch.setattr(flagging, "measure_km", broken)
    r = await put(30)  # the new radius would clear it, but nothing can be measured right now
    assert r.status_code == 200 and r.headers[HEADER] == "0"
    assert await flags(p, far) == ["outside_service_area"]  # unknown is not "fine": the flag stays for a human


# --------------------------------------------------------------------- salon
async def test_add_ons_do_not_count_when_checking_a_stylists_qualification(client: httpx.AsyncClient) -> None:
    from tests.test_salon import make_salon, sbook

    s = await make_salon(client)
    mei, lin = s.stylists["Mei"], s.stylists["Lin"]
    patch = lambda name, *ids: s.client.patch(f"/v1/admin/services/{s.svc[name]}", json={"resource_ids": [str(i) for i in ids]}, headers=s.admin)  # noqa: E731
    await patch("cut", mei)
    bid = (await sbook(s, at(s.day, 10), ["cut", "gel"], stylist="Mei")).json()["id"]
    r = await patch("gel", lin)  # the add-on becomes Lin's; Mei still cuts
    assert r.headers[HEADER] == "0" and await flags(s, bid) == []
    await patch("gel", mei)  # Mei can do the add-on ...
    r = await patch("cut", lin)  # ... but no longer the haircut itself, which is what she was booked for
    assert r.headers[HEADER] == "1" and await flags(s, bid) == ["staff_not_qualified"]
