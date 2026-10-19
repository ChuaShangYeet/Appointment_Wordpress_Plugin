"""Overnight opening hours and shifts (blueprint §6-7): an end before the start means the next day, and time after
midnight belongs to the business day on which the window started (the Business Day Rule)."""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.v1 import public as public_module
from app.core.database import tenant_session
from app.schemas.scheduling import OperatingHoursIn, ShiftIn, TimeSlotSettingIn
from app.services import booking as booking_module
from app.services import flagging
from app.services.allocators.restaurant import candidate_starts
from app.services.availability import Interval, is_overnight, shift_windows, window_on
from tests.conftest import TZ, Seed, allocations, at, make_seed
from tests.test_automotive import gbook, make_garage
from tests.test_field_service import make_practice, pbook
from tests.test_flagging import HEADER, _ctx, _table_booking, book, day_off, flags, put_hours, put_shifts
from tests.test_restaurant import make_restaurant, put_slots
from tests.test_restaurant import book as rbook

D = dt.date(2030, 1, 4)  # a Friday
T = dt.time


def t(hh: int, mm: int = 0) -> dt.time:
    return dt.time(hh, mm)


def fmt(iv: Interval) -> str:
    return f"{iv.start.astimezone(TZ):%a %H:%M}-{iv.end.astimezone(TZ):%a %H:%M}"


def shift(start: dt.time, end: dt.time, brk: tuple[dt.time, dt.time] | None = None) -> Any:
    return SimpleNamespace(weekday=5, start_time=start, end_time=end, break_start=brk[0] if brk else None, break_end=brk[1] if brk else None)


# --------------------------------------------------------------------- window maths
def test_hours_that_end_before_they_start_run_into_the_next_day() -> None:
    assert is_overnight(t(18), t(2)) and not is_overnight(t(9), t(18))
    assert fmt(window_on(D, t(9), t(18), TZ)) == "Fri 09:00-Fri 18:00"
    assert fmt(window_on(D, t(18), t(2), TZ)) == "Fri 18:00-Sat 02:00"  # Friday's window, including Saturday's early hours
    assert fmt(window_on(D, t(22), t(6), TZ)) == "Fri 22:00-Sat 06:00"


@pytest.mark.parametrize(("brk", "expected"), [
    (None, ["Fri 22:00-Sat 06:00"]),
    ((t(23), t(23, 30)), ["Fri 22:00-Fri 23:00", "Fri 23:30-Sat 06:00"]),  # a break before midnight
    ((t(2), t(3)), ["Fri 22:00-Sat 02:00", "Sat 03:00-Sat 06:00"]),  # a break after midnight
    ((t(23, 30), t(0, 30)), ["Fri 22:00-Fri 23:30", "Sat 00:30-Sat 06:00"]),  # a break across midnight
    ((t(5), t(6)), ["Fri 22:00-Sat 05:00", "Sat 06:00-Sat 06:00"]),  # a break that runs to the end of the shift
])
def test_a_break_inside_an_overnight_shift_is_cut_out_wherever_it_falls(brk: Any, expected: list[str]) -> None:
    assert [fmt(w) for w in shift_windows(shift(t(22), t(6), brk), D, TZ)] == expected


def test_an_ordinary_shift_with_a_break_is_unchanged() -> None:
    assert [fmt(w) for w in shift_windows(shift(t(9), t(18), (t(13), t(14))), D, TZ)] == ["Fri 09:00-Fri 13:00", "Fri 14:00-Fri 18:00"]


def test_fixed_slots_can_run_past_midnight() -> None:
    hours = [window_on(D, t(18), t(3), TZ)]
    day_start = dt.datetime.combine(D, dt.time.min, tzinfo=TZ)
    kw: dict[str, Any] = {"windows": hours, "day_start": day_start, "duration": dt.timedelta(minutes=90), "interval": dt.timedelta(minutes=60)}
    starts = candidate_starts(mode="fixed", first=t(18), last=t(1), **kw)
    assert [f"{s.astimezone(TZ):%a %H:%M}" for s in starts] == ["Fri 18:00", "Fri 19:00", "Fri 20:00", "Fri 21:00", "Fri 22:00", "Fri 23:00", "Sat 00:00", "Sat 01:00"]
    daytime = candidate_starts(mode="fixed", first=t(18), last=t(22), **kw)  # unchanged when last is after first
    assert [f"{s.astimezone(TZ):%H:%M}" for s in daytime] == ["18:00", "19:00", "20:00", "21:00", "22:00"]
    flexible = candidate_starts(mode="flexible", first=t(0), last=t(0), **kw)  # from opening every interval while the seating fits
    assert f"{flexible[-1].astimezone(TZ):%a %H:%M}" == "Sat 01:00"


# --------------------------------------------------------------------- validation and constraints
def test_overnight_hours_and_shifts_are_accepted_but_zero_length_ones_are_not() -> None:
    assert OperatingHoursIn(weekday=5, open_time=t(18), close_time=t(2))
    with pytest.raises(ValidationError):
        OperatingHoursIn(weekday=5, open_time=t(18), close_time=t(18))
    assert ShiftIn(weekday=5, start_time=t(22), end_time=t(6))
    with pytest.raises(ValidationError):
        ShiftIn(weekday=5, start_time=t(22), end_time=t(22))
    assert TimeSlotSettingIn(booking_mode="fixed", slot_duration_minutes=90, slot_interval_minutes=60, first_slot_time=t(18), last_slot_time=t(1))


@pytest.mark.parametrize(("brk", "ok"), [
    ((t(23), t(23, 30)), True), ((t(2), t(3)), True), ((t(23, 30), t(0, 30)), True), ((t(5), t(6)), True),
    ((t(7), t(8)), False),  # after the shift
    ((t(21), t(22, 30)), False),  # starts before the shift
    ((t(3), t(2)), False),  # ends before it starts (it would wrap all the way round)
    ((t(2), t(2)), False),
    ((t(23), None), False),
])
def test_a_break_must_lie_inside_an_overnight_shift(brk: Any, ok: bool) -> None:
    kw: dict[str, Any] = {"weekday": 5, "start_time": t(22), "end_time": t(6), "break_start": brk[0], "break_end": brk[1]}
    if ok:
        assert ShiftIn(**kw)
    else:
        with pytest.raises(ValidationError):
            ShiftIn(**kw)


async def test_the_database_enforces_the_same_rules_as_the_api(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei", shifts=False)

    async def insert(start: str, end: str, brk: tuple[str, str] | None = None) -> None:
        async with tenant_session(seeded.tenant_id) as db:
            await db.execute(
                text("INSERT INTO resource_shifts (tenant_id, resource_id, weekday, start_time, end_time, break_start, break_end) "
                     "VALUES (:t, :r, 1, :s, :e, :bs, :be)"),
                {"t": seeded.tenant_id, "r": mei, "s": t_(start), "e": t_(end), "bs": t_(brk[0]) if brk else None, "be": t_(brk[1]) if brk else None},
            )

    def t_(v: str) -> dt.time:
        return dt.time.fromisoformat(v)

    await insert("22:00", "06:00", ("02:00", "03:00"))  # an overnight shift with a break after midnight
    await insert("09:00", "18:00", ("13:00", "14:00"))
    for bad in [("22:00", "22:00", None), ("22:00", "06:00", ("07:00", "08:00")), ("09:00", "18:00", ("17:00", "19:00")), ("22:00", "06:00", ("03:00", "02:00"))]:
        with pytest.raises(IntegrityError):
            await insert(*bad)


# --------------------------------------------------------------------- staff with a night shift
async def night_clinic(client: httpx.AsyncClient, *, brk: tuple[str, str] | None = None) -> tuple[Seed, uuid.UUID]:
    s = await make_seed(client)
    mei = await s.add_staff("Mei", shifts=False)
    await put_shifts(s, mei, start="22:00:00", end="06:00:00", brk=brk)
    return s, mei


async def slot_times(s: Seed, day: dt.date | None = None, **params: Any) -> list[str]:
    q: dict[str, Any] = {"date": (day or s.day).isoformat(), "service_ids": [str(s.service_id)], **params}
    r = await s.client.get("/v1/public/availability", params=q, headers=s.pk)
    assert r.status_code == 200, r.text
    return [f"{dt.datetime.fromisoformat(x['start_at']).astimezone(TZ):%d %H:%M}" for x in r.json()["slots"]]


async def test_a_night_shift_offers_slots_across_midnight_under_the_day_it_started(client: httpx.AsyncClient) -> None:
    s, _ = await night_clinic(client)
    slots = await slot_times(s)
    nxt = s.day + dt.timedelta(days=1)
    assert slots[0] == f"{s.day:%d} 22:00" and slots[-1] == f"{nxt:%d} 05:00"  # the last 60-minute start that ends by 06:00
    assert f"{nxt:%d} 00:00" in slots and f"{nxt:%d} 01:15" in slots
    assert len(slots) == 29  # 22:00 .. 05:00 every 15 minutes
    later = await slot_times(s, nxt)  # the next date is its own night: nothing before 22:00
    assert later[0] == f"{nxt:%d} 22:00" and not any(x.startswith(f"{nxt:%d} 0") for x in later)


async def test_an_after_midnight_slot_can_be_booked_and_blocks_the_right_time(client: httpx.AsyncClient) -> None:
    s, mei = await night_clinic(client)
    nxt = s.day + dt.timedelta(days=1)
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(at(nxt, 1), mei), headers=s.pk)
    assert r.status_code == 201, r.text
    assert [(a[2], a[3]) for a in await allocations(s, r.json()["id"])] == [("01:00", "02:00")]
    slots = await slot_times(s)
    assert f"{nxt:%d} 00:15" not in slots and f"{nxt:%d} 01:45" not in slots and f"{nxt:%d} 00:00" in slots and f"{nxt:%d} 02:00" in slots
    overlapping = await s.client.post("/v1/public/bookings", json=s.booking_body(at(nxt, 1, 30), mei), headers=s.pk)
    assert overlapping.status_code == 409  # a booking that straddles it is refused
    outside = await s.client.post("/v1/public/bookings", json=s.booking_body(at(nxt, 8), mei), headers=s.pk)
    assert outside.status_code == 409  # 08:00 is nobody's business day


async def test_bookings_either_side_of_midnight_cannot_overlap(client: httpx.AsyncClient) -> None:
    s, mei = await night_clinic(client)
    nxt = s.day + dt.timedelta(days=1)
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 23, 30), mei), headers=s.pk)).status_code == 201
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(nxt, 0, 0), mei), headers=s.pk)).status_code == 409  # 23:30-00:30 vs 00:00-01:00
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(nxt, 0, 30), mei), headers=s.pk)).status_code == 201  # back to back is fine


async def test_a_break_after_midnight_is_respected(client: httpx.AsyncClient) -> None:
    s, _ = await night_clinic(client, brk=("02:00:00", "03:00:00"))
    nxt = s.day + dt.timedelta(days=1)
    slots = await slot_times(s)
    assert f"{nxt:%d} 01:00" in slots and f"{nxt:%d} 01:15" not in slots and f"{nxt:%d} 02:45" not in slots and f"{nxt:%d} 03:00" in slots


async def test_a_day_off_belongs_to_the_business_day_the_night_began_on(client: httpx.AsyncClient) -> None:
    """Blueprint §6: a day off on Friday also blocks the early hours of Saturday that belong to Friday's shift."""
    s, mei = await night_clinic(client)
    nxt = s.day + dt.timedelta(days=1)
    await day_off(s, nxt)  # the holiday is the *second* date
    slots = await slot_times(s)
    assert f"{nxt:%d} 01:00" in slots and f"{nxt:%d} 05:00" in slots  # the first night is untouched, right up to 06:00
    assert await slot_times(s, nxt) == []  # the holiday's own night is gone
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(nxt, 1), mei), headers=s.pk)).status_code == 201

    other, _ = await night_clinic(client)
    await day_off(other, other.day)  # the holiday is the *first* date
    assert await slot_times(other) == []  # all of that night, including the hours after midnight
    assert await slot_times(other, other.day + dt.timedelta(days=1))  # the next night is open


async def test_flags_follow_the_business_day_too(client: httpx.AsyncClient) -> None:
    s, mei = await night_clinic(client)
    nxt = s.day + dt.timedelta(days=1)
    bid = await book(s, mei, at(nxt, 1))  # after midnight: Friday-night business, Saturday on the clock
    r = await day_off(s, nxt)
    assert r.headers[HEADER] == "0" and await flags(s, bid) == []  # a holiday on the calendar date does not touch it
    r = await day_off(s, s.day)
    assert r.headers[HEADER] == "1" and await flags(s, bid) == ["day_off"]  # the day the night began on does
    r = await put_shifts(s, mei, start="22:00:00", end="00:30:00", brk=None)  # the shift now ends before that booking
    assert await flags(s, bid) == ["day_off", "outside_shift"]
    await put_shifts(s, mei, start="22:00:00", end="06:00:00", brk=None)
    assert await flags(s, bid) == ["day_off"]  # undoing the shift change lifts its own flag


# --------------------------------------------------------------------- restaurant with overnight hours
async def bar(client: httpx.AsyncClient, *, close: str = "02:00:00") -> Any:
    rest = await make_restaurant(client)
    await put_hours(rest, "18:00:00", close)
    return rest


async def rslots(rest: Seed, party: int, day: dt.date | None = None) -> list[str]:
    r = await rest.client.get("/v1/public/availability", params={"date": (day or rest.day).isoformat(), "party_size": party}, headers=rest.pk)
    assert r.status_code == 200, r.text
    return [f"{dt.datetime.fromisoformat(x['start_at']).astimezone(TZ):%d %H:%M}" for x in r.json()["slots"]]


async def test_a_bar_takes_reservations_up_to_the_last_seating_that_fits_before_it_closes(client: httpx.AsyncClient) -> None:
    rest = await bar(client)  # 18:00-02:00, 90-minute seatings every 30 minutes
    nxt = rest.day + dt.timedelta(days=1)
    slots = await rslots(rest, 2)
    assert slots[0] == f"{rest.day:%d} 18:00" and slots[-1] == f"{nxt:%d} 00:30"  # 00:30 + 90 minutes = 02:00
    later = await rslots(rest, 2, day=nxt)  # the next date is its own night, starting again at 18:00
    assert later[0] == f"{nxt:%d} 18:00" and not any(x.startswith(f"{nxt:%d} 0") for x in later)


async def test_fixed_reservation_times_may_run_past_midnight(client: httpx.AsyncClient) -> None:
    rest = await bar(client, close="03:00:00")
    nxt = rest.day + dt.timedelta(days=1)
    await put_slots(rest, booking_mode="fixed", first_slot_time="18:00:00", last_slot_time="01:00:00", slot_interval_minutes=60)  # rejected before overnight hours
    assert await rslots(rest, 2) == [f"{rest.day:%d} {h}:00" for h in range(18, 24)] + [f"{nxt:%d} 00:00", f"{nxt:%d} 01:00"]


async def test_the_open_table_cap_holds_across_midnight(client: httpx.AsyncClient) -> None:
    rest = await bar(client)
    nxt = rest.day + dt.timedelta(days=1)
    wd = rest.day.isoweekday() % 7
    rules = await rest.client.put("/v1/admin/table-open-rules", json=[{"weekday": wd, "table_type": "normal", "open_tables": 1}], headers=rest.admin)
    assert rules.status_code == 200
    assert (await rbook(rest, at(rest.day, 23), 2)).status_code == 201
    assert (await rbook(rest, at(nxt, 0, 30), 3)).status_code == 409  # 23:00-00:45 and 00:30-02:15 overlap, and it is the same business night


async def test_reservations_after_midnight_follow_hours_slot_times_and_holidays(client: httpx.AsyncClient) -> None:
    rest = await bar(client)
    nxt = rest.day + dt.timedelta(days=1)
    late = (await rbook(rest, at(nxt, 0, 30), 2)).json()["id"]  # seated 00:30-02:00 (the 02:15 table reset does not count)
    early = (await rbook(rest, at(rest.day, 19), 2)).json()["id"]
    assert (await put_hours(rest, "18:00:00", "02:00:00")).headers[HEADER] == "0"  # the same hours again: still fine
    r = await put_hours(rest, "18:00:00", "01:00:00")  # closing an hour earlier cuts the 00:30 seating short
    assert r.headers[HEADER] == "1" and await flags(rest, late) == ["outside_operating_hours"] and await flags(rest, early) == []
    await put_hours(rest, "18:00:00", "02:00:00")
    assert await flags(rest, late) == []

    fixed = await rest.client.put("/v1/admin/time-slot-settings", headers=rest.admin, json={
        "booking_mode": "fixed", "slot_duration_minutes": 90, "slot_interval_minutes": 60, "first_slot_time": "18:00:00", "last_slot_time": "23:00:00", "buffer_minutes": 15})
    assert fixed.headers[HEADER] == "1" and await flags(rest, late) == ["outside_slot_times"]  # 00:30 is after the last fixed slot
    wrap = await rest.client.put("/v1/admin/time-slot-settings", headers=rest.admin, json={
        "booking_mode": "fixed", "slot_duration_minutes": 90, "slot_interval_minutes": 60, "first_slot_time": "18:00:00", "last_slot_time": "01:00:00", "buffer_minutes": 15})
    assert wrap.headers["X-Flags-Lifted"] == "1" and await flags(rest, late) == []  # a last slot at 01:00 (next day) includes 00:30

    assert (await day_off(rest, nxt)).headers[HEADER] == "0" and await flags(rest, late) == []
    assert (await day_off(rest, rest.day)).headers[HEADER] == "2" and await flags(rest, late) == ["day_off"]


async def test_the_table_cap_flag_groups_a_night_together(client: httpx.AsyncClient) -> None:
    rest = await bar(client)
    nxt = rest.day + dt.timedelta(days=1)
    a = (await rbook(rest, at(rest.day, 23), 2)).json()["id"]
    b = (await rbook(rest, at(nxt, 0, 30), 3)).json()["id"]
    wd = rest.day.isoweekday() % 7
    r = await rest.client.put("/v1/admin/table-open-rules", json=[{"weekday": wd, "table_type": "normal", "open_tables": 1}], headers=rest.admin)
    assert r.headers[HEADER] == "2" and await flags(rest, a) == ["table_cap_exceeded"] and await flags(rest, b) == ["table_cap_exceeded"]


# --------------------------------------------------------------------- workshop and field service
async def test_a_night_workshop_books_mechanic_and_bay_after_midnight(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    await put_hours(g, "20:00:00", "04:00:00")
    for m in g.mechanics.values():
        await put_shifts(g, m, start="22:00:00", end="06:00:00", brk=None)
    nxt = g.day + dt.timedelta(days=1)
    q = {"date": g.day.isoformat(), "service_ids": [str(g.svc["oil"])]}
    slots = [f"{dt.datetime.fromisoformat(x['start_at']).astimezone(TZ):%d %H:%M}" for x in (await g.client.get("/v1/public/availability", params=q, headers=g.pk)).json()["slots"]]
    assert slots[0] == f"{g.day:%d} 22:00" and slots[-1] == f"{nxt:%d} 03:00" and len(slots) == 21  # the hours (04:00) end it, not the shifts (06:00)
    r = await gbook(g, at(nxt, 1), ["oil"])
    assert r.status_code == 201, r.text
    assert sorted((a[0], a[2], a[3]) for a in await allocations(g, r.json()["id"])) == [("bay", "01:00", "02:00"), ("staff", "01:00", "02:00")]


async def test_a_night_practice_books_a_professional_after_midnight(client: httpx.AsyncClient) -> None:
    p = await make_practice(client)
    await put_hours(p, "20:00:00", "04:00:00")
    pro = next(iter(p.pros.values()))
    await put_shifts(p, pro, start="22:00:00", end="06:00:00", brk=None)
    nxt = p.day + dt.timedelta(days=1)
    q = {"date": p.day.isoformat(), "service_ids": str(p.svc["office"])}
    r = await p.client.get("/v1/public/availability", params=q, headers=p.pk)
    slots = [f"{dt.datetime.fromisoformat(x['start_at']).astimezone(TZ):%d %H:%M}" for x in r.json()["slots"]]
    assert slots[0] == f"{p.day:%d} 22:00" and slots[-1] == f"{nxt:%d} 03:00"
    booked = await pbook(p, at(nxt, 2), "office")
    assert booked.status_code == 201, booked.text


async def test_any_available_counts_a_stylists_after_midnight_bookings_as_part_of_that_nights_load(client: httpx.AsyncClient) -> None:
    """Both stylists are free at 23:00; one already has three bookings after midnight on the same business night, so
    the other must be given the new one. (Ties are broken by id, so the busier stylist is the one that sorts first:
    ignoring their after-midnight hours would pick them.)"""
    s = await make_seed(client)
    a, b = await s.add_staff("A", shifts=False), await s.add_staff("B", shifts=False)
    busy, free = sorted((a, b), key=str)[0], sorted((a, b), key=str)[1]
    for r in (a, b):
        await put_shifts(s, r, start="22:00:00", end="06:00:00", brk=None)
    nxt = s.day + dt.timedelta(days=1)
    for hh in (1, 2, 3):
        assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(nxt, hh), busy), headers=s.pk)).status_code == 201
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 23)), headers=s.pk)  # any available
    assert r.status_code == 201 and r.json()["resource_ids"] == [str(free)]


async def test_night_shifts_work_without_any_opening_hours(client: httpx.AsyncClient) -> None:
    """With no opening hours the shifts alone decide, so a night shift must not be cut off at midnight."""
    g = await make_garage(client)
    assert (await g.client.put("/v1/admin/operating-hours", json=[], headers=g.admin)).status_code == 200
    for m in g.mechanics.values():
        await put_shifts(g, m, start="22:00:00", end="06:00:00", brk=None)
    q = {"date": g.day.isoformat(), "service_ids": [str(g.svc["oil"])]}
    slots = [f"{dt.datetime.fromisoformat(x['start_at']).astimezone(TZ):%d %H:%M}" for x in (await g.client.get("/v1/public/availability", params=q, headers=g.pk)).json()["slots"]]
    assert slots[-1] == f"{g.day + dt.timedelta(days=1):%d} 05:00" and len(slots) == 29
    assert (await gbook(g, at(g.day + dt.timedelta(days=1), 4), ["oil"])).status_code == 201

    p = await make_practice(client)
    assert (await p.client.put("/v1/admin/operating-hours", json=[], headers=p.admin)).status_code == 200
    pro = next(iter(p.pros.values()))
    await put_shifts(p, pro, start="22:00:00", end="06:00:00", brk=None)
    booked = await pbook(p, at(p.day + dt.timedelta(days=1), 3), "office")
    assert booked.status_code == 201, booked.text


# --------------------------------------------------------------------- the booking side
async def test_yesterdays_business_day_is_only_tried_when_something_of_it_runs_past_midnight(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei", shifts=False)
    friday = dt.date(2030, 1, 4)
    body = [{"weekday": 5, "start_time": "22:00:00", "end_time": "06:00:00"}, {"weekday": 1, "start_time": "09:00:00", "end_time": "18:00:00"}]
    assert (await seeded.client.put(f"/v1/admin/resources/{mei}/shifts", json=body, headers=seeded.admin)).status_code == 200
    async with tenant_session(seeded.tenant_id) as db:
        assert await booking_module._has_overnight_window(db, friday) is True
        assert await booking_module._has_overnight_window(db, friday + dt.timedelta(days=1)) is False  # Saturday: no rows
        assert await booking_module._has_overnight_window(db, dt.date(2030, 1, 7)) is False  # Monday: a day shift only
    assert (await put_hours(seeded, "18:00:00", "02:00:00")).status_code == 200
    async with tenant_session(seeded.tenant_id) as db:
        assert await booking_module._has_overnight_window(db, dt.date(2030, 1, 7)) is True  # overnight opening hours count too


async def test_a_business_without_overnight_hours_makes_one_allocation_attempt_per_request(
    client: httpx.AsyncClient, seeded: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trying yesterday's business day costs a second allocation, so it must only happen where it can matter."""
    mei = await seeded.add_staff("Mei")  # a day shift
    night, night_mei = await night_clinic(client)
    calls: list[dt.date] = []
    real = booking_module.get_allocator

    class Counting:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def allocate(self, db: Any, req: Any, start: Any) -> Any:
            calls.append(req.day)
            return await self.inner.allocate(db, req, start)

    with monkeypatch.context() as m:
        m.setattr(booking_module, "get_allocator", lambda bt: Counting(real(bt)))
        refused = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 3), mei), headers=seeded.pk)
        assert refused.status_code == 409 and calls == [seeded.day]  # 03:00: nobody works then, and yesterday cannot help
        calls.clear()
        nxt = night.day + dt.timedelta(days=1)
        refused = await night.client.post("/v1/public/bookings", json=night.booking_body(at(nxt, 9), night_mei), headers=night.pk)
        assert refused.status_code == 409 and calls == [nxt, night.day]  # the night shift could have covered it, so yesterday was tried
        calls.clear()
        ok = await night.client.post("/v1/public/bookings", json=night.booking_body(at(nxt, 1), night_mei), headers=night.pk)
        assert ok.status_code == 201 and calls == [nxt, night.day]  # ... and found the slot there


async def test_an_unavailable_after_midnight_start_reports_the_requested_dates_slots(client: httpx.AsyncClient) -> None:
    s, mei = await night_clinic(client)
    nxt = s.day + dt.timedelta(days=1)
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(at(nxt, 9), mei), headers=s.pk)
    assert r.status_code == 409 and r.json()["error"] == "slot_unavailable" and "slots" in r.json()


# --------------------------------------------------------------------- the month view
async def test_a_business_day_is_still_bookable_in_its_after_midnight_tail(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """At 00:30 on Saturday, Friday's night still has slots (01:00-05:00), so Friday must not be skipped as 'past'."""
    s, _ = await night_clinic(client)
    now = at(dt.date(2030, 3, 2), 0, 30)  # a Saturday, 00:30 in Kuala Lumpur
    monkeypatch.setattr(public_module, "_utcnow", lambda: now.astimezone(dt.UTC))
    r = await s.client.get("/v1/public/availability/month", params={"month": "2030-03", "service_ids": str(s.service_id)}, headers=s.pk)
    days = {d["date"]: d for d in r.json()["days"]}
    assert days["2030-03-01"]["available"] is True and days["2030-03-01"]["slot_count"] == 18  # 00:45 .. 05:00
    assert days["2030-03-02"]["available"] is True  # Saturday's own night
    feb = await s.client.get("/v1/public/availability/month", params={"month": "2030-02", "service_ids": str(s.service_id)}, headers=s.pk)
    assert {d["date"]: d["available"] for d in feb.json()["days"]}["2030-02-28"] is False  # Thursday night ended at 06:00 on Friday
    assert days["2030-03-05"]["available"] is True
