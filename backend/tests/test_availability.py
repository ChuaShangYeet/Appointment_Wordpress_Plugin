"""Slot generation and shift-boundary tests (pure logic first, then end-to-end)."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from tests.conftest import TZ, Seed, at
from app.services.availability import (
    Interval,
    covers_date,
    generate_slots,
    intersect_intervals,
    shift_windows,
    subtract_intervals,
    weekday_index,
)

D = dt.date(2030, 1, 7)  # a Monday


def iv(h1: int, m1: int, h2: int, m2: int) -> Interval:
    return Interval(at(D, h1, m1), at(D, h2, m2))


# --------------------------------------------------------------------- pure logic
def test_subtract_cuts_holes_and_keeps_edges() -> None:
    free = subtract_intervals([iv(9, 0, 18, 0)], [iv(10, 0, 11, 0), iv(17, 0, 19, 0)])
    assert free == [iv(9, 0, 10, 0), iv(11, 0, 17, 0)]


def test_subtract_touching_busy_does_not_shrink() -> None:
    assert subtract_intervals([iv(9, 0, 10, 0)], [iv(10, 0, 11, 0)]) == [iv(9, 0, 10, 0)]


def test_subtract_fully_covered_window_disappears() -> None:
    assert subtract_intervals([iv(9, 0, 10, 0)], [iv(8, 0, 11, 0)]) == []


def test_intersect() -> None:
    assert intersect_intervals([iv(9, 0, 18, 0)], [iv(10, 0, 12, 0), iv(17, 0, 20, 0)]) == [
        iv(10, 0, 12, 0), iv(17, 0, 18, 0)
    ]


def test_slots_align_to_grid_and_fit_before_window_end() -> None:
    day0 = at(D, 0)
    slots = generate_slots([iv(9, 10, 11, 0)], dt.timedelta(hours=1), dt.timedelta(minutes=15), day0, day0, day0 + dt.timedelta(days=9))
    # first grid point at/after 09:10 is 09:15; last start that still fits a 60min block is 10:00
    assert [s.start.astimezone(TZ).strftime("%H:%M") for s in slots] == ["09:15", "09:30", "09:45", "10:00"]


def test_slot_must_end_exactly_at_or_before_shift_end() -> None:
    day0 = at(D, 0)
    slots = generate_slots([iv(9, 0, 10, 0)], dt.timedelta(hours=1), dt.timedelta(minutes=30), day0, day0, day0 + dt.timedelta(days=1))
    assert [s.start.astimezone(TZ).strftime("%H:%M") for s in slots] == ["09:00"]


def test_earliest_and_latest_bounds() -> None:
    day0 = at(D, 0)
    slots = generate_slots(
        [iv(9, 0, 18, 0)], dt.timedelta(hours=1), dt.timedelta(minutes=60), day0, at(D, 11, 30), at(D, 14, 0)
    )
    assert [s.start.astimezone(TZ).strftime("%H:%M") for s in slots] == ["12:00", "13:00", "14:00"]


def test_shift_break_splits_window() -> None:
    shift = SimpleNamespace(
        start_time=dt.time(9), end_time=dt.time(18), break_start=dt.time(13), break_end=dt.time(14)
    )
    assert shift_windows(shift, D, TZ) == [iv(9, 0, 13, 0), iv(14, 0, 18, 0)]  # type: ignore[arg-type]


def test_weekday_index_is_sunday_zero() -> None:
    assert weekday_index(dt.date(2030, 1, 6)) == 0  # Sunday
    assert weekday_index(D) == 1
    assert weekday_index(dt.date(2030, 1, 12)) == 6


def _off(s: dt.date, e: dt.date, yearly: bool = False) -> SimpleNamespace:
    return SimpleNamespace(start_date=s, end_date=e, repeat_yearly=yearly)


def test_days_off_single_and_yearly_and_year_boundary() -> None:
    assert covers_date(_off(dt.date(2030, 5, 1), dt.date(2030, 5, 1)), dt.date(2030, 5, 1))  # type: ignore[arg-type]
    assert not covers_date(_off(dt.date(2030, 5, 1), dt.date(2030, 5, 1)), dt.date(2031, 5, 1))  # type: ignore[arg-type]
    assert covers_date(_off(dt.date(2020, 5, 1), dt.date(2020, 5, 1), True), dt.date(2031, 5, 1))  # type: ignore[arg-type]
    span = _off(dt.date(2020, 12, 30), dt.date(2021, 1, 2), True)
    assert covers_date(span, dt.date(2031, 1, 1))  # type: ignore[arg-type]
    assert not covers_date(span, dt.date(2031, 1, 3))  # type: ignore[arg-type]


# --------------------------------------------------------------------- end to end (needs PostgreSQL)
async def _slots(seed: Seed, **params: object) -> list[dict[str, object]]:
    q = {"date": seed.day.isoformat(), "service_ids": str(seed.service_id), **{k: v for k, v in params.items() if v is not None}}
    r = await seed.client.get("/v1/public/availability", params=q, headers=seed.pk)
    assert r.status_code == 200, r.text
    return r.json()["slots"]  # type: ignore[no-any-return]


def _hhmm(slots: list[dict[str, object]]) -> list[str]:
    return [dt.datetime.fromisoformat(str(s["start_at"])).astimezone(TZ).strftime("%H:%M") for s in slots]


async def test_slots_respect_shift_and_lunch_break(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    starts = _hhmm(await _slots(seeded, resource_id=None))
    assert starts[0] == "09:00" and starts[-1] == "17:00"  # 60-min service must end by 18:00
    assert "12:15" not in starts and "12:45" not in starts  # would run into the 13:00 break
    assert "12:00" in starts and "14:00" in starts
    assert not any(s in starts for s in ("13:00", "13:15", "13:45"))


async def test_company_holiday_removes_all_slots(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    r = await seeded.client.post(
        "/v1/admin/days-off", headers=seeded.admin,
        json={"name": "Public holiday", "start_date": seeded.day.isoformat(), "end_date": seeded.day.isoformat()},
    )
    assert r.status_code == 201, r.text
    assert await _slots(seeded) == []


async def test_operating_hours_clip_shifts(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    hours = [{"weekday": d, "open_time": "10:00:00", "close_time": "16:00:00"} for d in range(7)]
    assert (await seeded.client.put("/v1/admin/operating-hours", json=hours, headers=seeded.admin)).status_code == 200
    starts = _hhmm(await _slots(seeded))
    assert starts[0] == "10:00" and starts[-1] == "15:00"


async def test_booked_period_is_subtracted(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert r.status_code == 201, r.text
    starts = _hhmm(await _slots(seeded, resource_id=str(mei)))
    assert "10:00" not in starts and "09:15" not in starts and "10:45" not in starts  # overlap the 10-11 booking
    assert "09:00" in starts and "11:00" in starts


async def test_booking_window_enforced(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    far = at(seeded.day + dt.timedelta(days=90), 10)
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(far), headers=seeded.pk)
    assert r.status_code == 422 and r.json()["error"] == "outside_booking_window"
    soon = dt.datetime.now(TZ) + dt.timedelta(seconds=30)  # seeded min notice is 60s
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(soon), headers=seeded.pk)
    assert r.status_code == 422 and r.json()["error"] == "outside_booking_window"
