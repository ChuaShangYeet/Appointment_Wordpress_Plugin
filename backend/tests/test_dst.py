"""Daylight saving: a business day in Australia is sometimes 23 or 25 real hours long (Malaysia and Singapore never change their clocks).

Sydney, Melbourne, Hobart, Adelaide and Canberra put their clocks forward on the first Sunday of October (02:00 becomes 03:00) and back on the
first Sunday of April (03:00 becomes 02:00). Brisbane, Perth and Darwin do not. A shift or opening hours are written as a clock time, so the
night that crosses the change must come out as 7 real hours in spring and 9 in autumn, and no start time may be offered that does not exist
(02:30 in spring) or that is two different moments (02:30 in autumn: it happens twice).
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services.availability import Interval, generate_slots, intersect_intervals, shift_windows, subtract_intervals, window_on
from tests.conftest import PLATFORM, Seed

SYD = ZoneInfo("Australia/Sydney")
UTC = dt.UTC
SPRING = dt.date(2026, 10, 3)  # a Saturday: the night that starts here crosses 02:00 on Sunday 4 October, when the clocks go forward
AUTUMN = dt.date(2027, 4, 3)  # a Saturday: the night that starts here crosses 03:00 on Sunday 4 April, when the clocks go back
ORDINARY = dt.date(2026, 9, 5)
EARLY, LATE = dt.datetime(2000, 1, 1, tzinfo=UTC), dt.datetime(2100, 1, 1, tzinfo=UTC)
HOUR = dt.timedelta(hours=1)


def midnight(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=SYD)


def real(t: dt.datetime) -> dt.datetime:
    return t.astimezone(UTC)


def slots_in(window: Interval, day: dt.date, block: dt.timedelta = HOUR, step: dt.timedelta = dt.timedelta(minutes=15)) -> list[Interval]:
    return generate_slots([window], block, step, midnight(day), EARLY, LATE)


def test_the_dates_the_tests_rely_on_really_are_the_clock_changes() -> None:
    assert midnight(SPRING).utcoffset() == dt.timedelta(hours=10) and midnight(SPRING + dt.timedelta(days=2)).utcoffset() == dt.timedelta(hours=11)
    assert midnight(AUTUMN).utcoffset() == dt.timedelta(hours=11) and midnight(AUTUMN + dt.timedelta(days=2)).utcoffset() == dt.timedelta(hours=10)


# --------------------------------------------------------------------- how long a window really is
@pytest.mark.parametrize(("day", "hours"), [(ORDINARY, 8), (SPRING, 7), (AUTUMN, 9)])
def test_a_night_window_is_as_long_as_the_hours_that_really_pass(day: dt.date, hours: int) -> None:
    window = window_on(day, dt.time(22), dt.time(6), SYD)
    assert window.minutes == hours * 60
    assert real(window.end) - real(window.start) == dt.timedelta(hours=hours)


@pytest.mark.parametrize(("day", "hours"), [(ORDINARY, 8), (SPRING, 8), (AUTUMN, 8)])
def test_a_daytime_window_is_not_affected(day: dt.date, hours: int) -> None:
    assert window_on(day, dt.time(9), dt.time(17), SYD).minutes == hours * 60


def test_a_window_ends_at_the_clock_time_it_names() -> None:
    for day in (SPRING, AUTUMN, ORDINARY):
        window = window_on(day, dt.time(22), dt.time(6), SYD)
        assert window.start.astimezone(SYD).strftime("%H:%M") == "22:00" and window.end.astimezone(SYD).strftime("%H:%M") == "06:00"
        assert window.end.astimezone(SYD).date() == day + dt.timedelta(days=1)


def test_a_break_across_the_change_is_still_an_hour_of_real_time() -> None:
    from types import SimpleNamespace

    shift: Any = SimpleNamespace(start_time=dt.time(22), end_time=dt.time(6), break_start=dt.time(1), break_end=dt.time(2))
    before, after = shift_windows(shift, SPRING, SYD)
    assert (before.minutes, after.minutes) == (3 * 60, 3 * 60)  # 22:00-01:00, then 03:00 on the clock (written 02:00, which was skipped) to 06:00
    assert real(after.start) - real(before.end) == HOUR


# --------------------------------------------------------------------- the slots offered
def test_spring_forward_offers_no_start_in_the_hour_that_does_not_exist() -> None:
    window = window_on(SPRING, dt.time(22), dt.time(6), SYD)
    slots = slots_in(window, SPRING)
    starts = [real(s.start) for s in slots]
    assert len(set(starts)) == len(starts), "two slots at the same moment"
    assert len(slots) == (7 - 1) * 4 + 1  # seven real hours, one-hour blocks, a start every 15 minutes
    assert all(real(s.end) - real(s.start) == HOUR for s in slots)
    assert all(real(window.start) <= real(s.start) and real(s.end) <= real(window.end) for s in slots)
    assert "02" not in {s.start.astimezone(SYD).strftime("%H") for s in slots}  # that hour was skipped


def test_autumn_back_offers_both_passes_of_the_repeated_hour() -> None:
    window = window_on(AUTUMN, dt.time(22), dt.time(6), SYD)
    slots = slots_in(window, AUTUMN)
    starts = [real(s.start) for s in slots]
    assert len(set(starts)) == len(starts)
    assert len(slots) == (9 - 1) * 4 + 1
    assert all(real(s.end) - real(s.start) == HOUR for s in slots)
    twice = [s for s in slots if s.start.astimezone(SYD).strftime("%H:%M") == "02:30"]
    assert len(twice) == 2 and real(twice[1].start) - real(twice[0].start) == HOUR  # 02:30 summer time, then 02:30 standard time


def test_the_slot_grid_stays_on_the_clock_after_the_change() -> None:
    """Starts are on the quarter hour of the wall clock the day after the clocks changed, as they were before."""
    window = window_on(SPRING, dt.time(22), dt.time(6), SYD)
    assert {s.start.astimezone(SYD).minute for s in slots_in(window, SPRING)} == {0, 15, 30, 45}


def test_a_slot_that_starts_before_the_jump_and_ends_after_it_is_a_full_hour_long() -> None:
    window = window_on(SPRING, dt.time(22), dt.time(6), SYD)
    straddle = [s for s in slots_in(window, SPRING) if s.start.astimezone(SYD).strftime("%H:%M") == "01:30"]
    assert len(straddle) == 1
    assert straddle[0].end.astimezone(SYD).strftime("%H:%M") == "03:30"  # 01:30 to 02:30 AEST, which the clocks now call 03:30
    assert real(straddle[0].end) - real(straddle[0].start) == HOUR


def test_ordinary_days_are_unchanged() -> None:
    window = window_on(ORDINARY, dt.time(9), dt.time(17), SYD)
    slots = slots_in(window, ORDINARY)
    assert len(slots) == (8 - 1) * 4 + 1 and slots[0].start.astimezone(SYD).strftime("%H:%M") == "09:00" and slots[-1].end.astimezone(SYD).strftime("%H:%M") == "17:00"


# --------------------------------------------------------------------- the interval maths on those windows
def test_opening_hours_and_a_shift_intersect_in_real_time() -> None:
    shift = window_on(SPRING, dt.time(22), dt.time(6), SYD)
    opening = window_on(SPRING, dt.time(20), dt.time(4), SYD)  # 20:00 to 04:00 on the clock = 7 real hours
    both = intersect_intervals([shift], [opening])
    assert len(both) == 1 and both[0].minutes == 5 * 60  # 22:00 to 04:00 on the clock = 5 real hours (one was skipped)


def test_a_booking_inside_the_skipped_hour_is_cut_out_correctly() -> None:
    window = window_on(SPRING, dt.time(22), dt.time(6), SYD)
    busy = Interval(real(window.start) + 4 * HOUR, real(window.start) + 5 * HOUR)  # 02:00 AEST = 03:00 on the clock afterwards
    left = subtract_intervals([window], [busy])
    assert [i.minutes for i in left] == [4 * 60, 2 * 60]


# --------------------------------------------------------------------- the safeguards under the maths
def test_an_interval_given_local_times_still_measures_real_time() -> None:
    lo, hi = dt.datetime(2026, 10, 3, 22, 0, tzinfo=SYD), dt.datetime(2026, 10, 4, 6, 0, tzinfo=SYD)  # 22:00 to 06:00 on the clock, across the jump
    interval = Interval(lo, hi)
    assert interval.minutes == 7 * 60
    assert interval.start.utcoffset() == dt.timedelta(0) and interval.end.utcoffset() == dt.timedelta(0)  # held as UTC whatever it was given
    assert [i.minutes for i in subtract_intervals([interval], [Interval(real(lo) + HOUR, real(lo) + 2 * HOUR)])] == [60, 5 * 60]


def test_an_interval_cannot_end_before_it_starts() -> None:
    now = dt.datetime(2026, 10, 3, 22, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="before start"):
        Interval(now, now - dt.timedelta(minutes=1))
    assert Interval(now, now).minutes == 0  # an empty one is fine


def test_a_local_earliest_start_is_stepped_in_real_time() -> None:
    """``earliest`` (the notice cut-off) may be given in the business's zone: the first slot is the next quarter hour of real time after it, in the repeated hour too."""
    window = window_on(AUTUMN, dt.time(22), dt.time(6), SYD)
    earliest = dt.datetime.combine(AUTUMN + dt.timedelta(days=1), dt.time(2, 50), tzinfo=SYD)  # 02:50 on summer time, ten minutes before the clocks go back
    slots = generate_slots([window], HOUR, dt.timedelta(minutes=15), midnight(AUTUMN), earliest, LATE)
    assert real(slots[0].start) - real(earliest) == dt.timedelta(minutes=10)  # 03:00 summer time = 02:00 standard time, not an hour later
    assert all(real(b.start) - real(a.start) == dt.timedelta(minutes=15) for a, b in zip(slots, slots[1:]))


@pytest.mark.parametrize(("day", "hours"), [(ORDINARY, 48), (SPRING, 47), (AUTUMN, 49)])
def test_a_business_day_starts_at_midnight_on_the_clock_and_a_night_shift_may_run_to_the_end_of_the_next(day: dt.date, hours: int) -> None:
    from types import SimpleNamespace

    from app.services.availability import AvailabilityRequest

    req = AvailabilityRequest(tenant=SimpleNamespace(timezone="Australia/Sydney"), services=[], policy=None, day=day)  # type: ignore[arg-type]
    assert req.day_start == dt.datetime.combine(day, dt.time.min, tzinfo=SYD) and req.day_start.astimezone(SYD).strftime("%H:%M") == "00:00"
    assert req.overnight_end == dt.datetime.combine(day + dt.timedelta(days=2), dt.time.min, tzinfo=SYD)
    assert req.overnight_end - req.day_start == dt.timedelta(hours=hours)
