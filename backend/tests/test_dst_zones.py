"""Daylight saving in the other four zones that change their clocks: Melbourne, Hobart, Adelaide and Lord Howe (Sydney is in ``test_dst.py`` and ``test_dst_nights.py``).

All four change on the same Sundays as Sydney (the first Sunday of October forward, the first Sunday of April back), but not in the same way:

* **Melbourne and Hobart** keep Sydney's hours (+10:00 / +11:00), so they must give exactly Sydney's answers: 02:00 becomes 03:00, and 03:00 becomes 02:00.
* **Adelaide** is on half-hour offsets (+9:30 / +10:30): a whole-hour jump on a clock that is half an hour off UTC.
* **Lord Howe Island** is the odd one out: +10:30 / +11:00, and the clocks move by **30 minutes**, not 60 (02:00 becomes 02:30 in October; 02:00 becomes 01:30 in April).
  So the night is 7.5 or 8.5 real hours, the hour that does not exist is only half an hour, and the repeated stretch is 01:30-02:00. A booking made in the first pass of
  a repeated half hour *overlaps* the second pass (they are 30 minutes apart, not an hour), which cannot happen with a one-hour jump.

The expected figures are written out per zone below; they are not read back from the code under test.
"""
from __future__ import annotations

import datetime as dt
from collections import Counter
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services.availability import AvailabilityRequest, Interval, generate_slots, intersect_intervals, shift_windows, subtract_intervals, window_on
from tests.test_availability_cache import avail
from tests.test_dst_nights import (
    LATE_HOURS,
    NIGHT,
    late_restaurant,
    next_change,
    night_spa,
    offered,
    starts,
)
from tests.test_restaurant import body, put_slots

UTC = dt.UTC
HOUR = dt.timedelta(hours=1)
QUARTER = dt.timedelta(minutes=15)
SPRING = dt.date(2026, 10, 3)  # a Saturday: the night that starts here crosses 02:00 on Sunday 4 October, when the clocks go forward
AUTUMN = dt.date(2027, 4, 3)  # a Saturday: the night that starts here crosses the change on Sunday 4 April, when the clocks go back
ORDINARY = dt.date(2026, 9, 5)
EARLY, LATE = dt.datetime(2000, 1, 1, tzinfo=UTC), dt.datetime(2100, 1, 1, tzinfo=UTC)

# The wall-clock quarter hours of a 22:00-06:00 shift, from 22:00 to the last start of a one-hour service (05:00).
NIGHT_QUARTERS = [f"{(22 + i // 4) % 24:02d}:{i % 4 * 15:02d}" for i in range(0, 7 * 4 + 1)]
WHOLE_HOUR_SKIP = frozenset({"02:00", "02:15", "02:30", "02:45"})


@dataclass(frozen=True)
class Zone:
    name: str
    summer: dt.timedelta  # the offset while the clocks are forward
    winter: dt.timedelta  # ... and when they are back
    #: Wall-clock quarter hours that do not exist on the night the clocks go forward.
    skipped: frozenset[str]
    #: Wall-clock quarter hours that happen twice on the night they go back.
    repeated: frozenset[str]
    #: Real minutes of the 22:00-06:00 night when the clocks go forward / back.
    spring_night: int
    autumn_night: int
    #: A quarter hour in the repeated stretch to book, and whether a booking at its first pass overlaps the second pass.
    pick: str
    passes_overlap: bool
    #: The wall clock one minute before the clocks go forward and at the moment they do (spring), and the same when they go back (autumn).
    spring_clock: tuple[str, str]
    autumn_clock: tuple[str, str]
    #: The clock reading one real hour after a start at 01:30 on the night the clocks go forward.
    straddle_end: str
    #: The clock reading 105 real minutes (a 90-minute seating and its 15-minute buffer) after the first 01:30 on the night the clocks go back.
    seating_end: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.name)

    @property
    def jump(self) -> dt.timedelta:
        return self.summer - self.winter


ZONES = [
    Zone("Australia/Melbourne", dt.timedelta(hours=11), dt.timedelta(hours=10), WHOLE_HOUR_SKIP, WHOLE_HOUR_SKIP, 7 * 60, 9 * 60, "02:30", False, ("01:59", "03:00"), ("02:59", "02:00"), "03:30", "02:15"),
    Zone("Australia/Hobart", dt.timedelta(hours=11), dt.timedelta(hours=10), WHOLE_HOUR_SKIP, WHOLE_HOUR_SKIP, 7 * 60, 9 * 60, "02:30", False, ("01:59", "03:00"), ("02:59", "02:00"), "03:30", "02:15"),
    Zone("Australia/Adelaide", dt.timedelta(hours=10, minutes=30), dt.timedelta(hours=9, minutes=30), WHOLE_HOUR_SKIP, WHOLE_HOUR_SKIP, 7 * 60, 9 * 60, "02:30", False,
         ("01:59", "03:00"), ("02:59", "02:00"), "03:30", "02:15"),
    Zone("Australia/Lord_Howe", dt.timedelta(hours=11), dt.timedelta(hours=10, minutes=30), frozenset({"02:00", "02:15"}), frozenset({"01:30", "01:45"}), 7 * 60 + 30, 8 * 60 + 30,
         "01:30", True, ("01:59", "02:30"), ("01:59", "01:30"), "03:00", "02:45"),
]
names = [z.name.split("/")[1] for z in ZONES]
by_zone = pytest.mark.parametrize("z", ZONES, ids=names)


def wall(t: dt.datetime, z: Zone) -> str:
    return t.astimezone(z.zone).strftime("%H:%M")


def midnight(day: dt.date, z: Zone) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=z.zone)


def real(t: dt.datetime) -> dt.datetime:
    return t.astimezone(UTC)


def transition(z: Zone, around: dt.date) -> dt.datetime:
    """The real instant (to the minute) at which this zone's offset changes on the Sunday after ``around``, found by walking the zone database minute by minute."""
    t = dt.datetime.combine(around, dt.time(0), tzinfo=UTC)
    for _ in range(3 * 24 * 60):
        if (t + dt.timedelta(minutes=1)).astimezone(z.zone).utcoffset() != t.astimezone(z.zone).utcoffset():
            return t + dt.timedelta(minutes=1)
        t += dt.timedelta(minutes=1)
    raise AssertionError("no change found")


def slots_in(window: Interval, day: dt.date, z: Zone, block: dt.timedelta = HOUR, step: dt.timedelta = QUARTER) -> list[Interval]:
    return generate_slots([window], block, step, midnight(day, z), EARLY, LATE)


# --------------------------------------------------------------------- the facts the rest relies on
@by_zone
def test_the_dates_the_tests_rely_on_really_are_the_clock_changes_in_this_zone(z: Zone) -> None:
    assert midnight(SPRING, z).utcoffset() == z.winter and midnight(SPRING + dt.timedelta(days=2), z).utcoffset() == z.summer
    assert midnight(AUTUMN, z).utcoffset() == z.summer and midnight(AUTUMN + dt.timedelta(days=2), z).utcoffset() == z.winter
    # the figures typed into ZONES for the length of the night agree with the zone database (the 22:00-06:00 night, real minutes)
    for day, minutes in ((SPRING, z.spring_night), (AUTUMN, z.autumn_night), (ORDINARY, 8 * 60)):
        end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(6), tzinfo=z.zone)
        assert (real(end) - real(dt.datetime.combine(day, dt.time(22), tzinfo=z.zone))) == dt.timedelta(minutes=minutes)


@by_zone
def test_the_clocks_move_at_the_moment_and_by_the_amount_this_zone_says(z: Zone) -> None:
    assert z.jump == (dt.timedelta(minutes=30) if z.name.endswith("Lord_Howe") else HOUR)
    for day, (before, after), offsets in ((SPRING, z.spring_clock, (z.winter, z.summer)), (AUTUMN, z.autumn_clock, (z.summer, z.winter))):
        t = transition(z, day)
        assert t.astimezone(z.zone).utcoffset() == offsets[1] and (t - dt.timedelta(minutes=1)).astimezone(z.zone).utcoffset() == offsets[0]
        assert (wall(t - dt.timedelta(minutes=1), z), wall(t, z)) == (before, after)
        assert t.astimezone(z.zone).date() == day + dt.timedelta(days=1)  # on the Sunday


# --------------------------------------------------------------------- how long a window really is
@by_zone
def test_a_night_window_is_as_long_as_the_hours_that_really_pass(z: Zone) -> None:
    for day, minutes in ((ORDINARY, 8 * 60), (SPRING, z.spring_night), (AUTUMN, z.autumn_night)):
        window = window_on(day, dt.time(22), dt.time(6), z.zone)
        assert window.minutes == minutes
        assert real(window.end) - real(window.start) == dt.timedelta(minutes=minutes)
        assert wall(window.start, z) == "22:00" and wall(window.end, z) == "06:00"
        assert window.end.astimezone(z.zone).date() == day + dt.timedelta(days=1)


@by_zone
def test_a_daytime_window_is_not_affected(z: Zone) -> None:
    for day in (ORDINARY, SPRING, AUTUMN):
        assert window_on(day, dt.time(9), dt.time(17), z.zone).minutes == 8 * 60


@by_zone
def test_a_business_day_and_a_night_shift_that_runs_to_the_end_of_the_next(z: Zone) -> None:
    for day, extra in ((ORDINARY, dt.timedelta(0)), (SPRING, -z.jump), (AUTUMN, z.jump)):
        req = AvailabilityRequest(tenant=SimpleNamespace(timezone=z.name), services=[], policy=None, day=day)  # type: ignore[arg-type]
        assert req.day_start == midnight(day, z) and wall(req.day_start, z) == "00:00"
        assert req.overnight_end == midnight(day + dt.timedelta(days=2), z)
        assert req.overnight_end - req.day_start == dt.timedelta(hours=48) + extra


@by_zone
def test_a_break_across_the_change_is_still_an_hour_of_real_time(z: Zone) -> None:
    """A break written 01:00-02:00. Where 02:00 does not exist (Lord Howe: 02:00-02:30 is skipped) it ends at the moment after the jump, so it is still an hour of real time."""
    shift: Any = SimpleNamespace(start_time=dt.time(22), end_time=dt.time(6), break_start=dt.time(1), break_end=dt.time(2))
    before, after = shift_windows(shift, SPRING, z.zone)
    assert before.minutes == 3 * 60 and real(after.start) - real(before.end) == HOUR
    assert after.minutes == z.spring_night - 3 * 60 - 60
    assert wall(after.end, z) == "06:00"


@by_zone
def test_a_break_after_the_change_is_the_length_it_was_written(z: Zone) -> None:
    shift: Any = SimpleNamespace(start_time=dt.time(22), end_time=dt.time(6), break_start=dt.time(3), break_end=dt.time(4))  # after the change in both directions
    before, after = shift_windows(shift, AUTUMN, z.zone)
    assert real(after.start) - real(before.end) == HOUR
    assert before.minutes + after.minutes + 60 == z.autumn_night


# --------------------------------------------------------------------- the slots offered
@by_zone
def test_spring_forward_offers_no_start_in_the_time_that_does_not_exist(z: Zone) -> None:
    window = window_on(SPRING, dt.time(22), dt.time(6), z.zone)
    slots = slots_in(window, SPRING, z)
    begins = [real(s.start) for s in slots]
    assert len(set(begins)) == len(begins), "two slots at the same moment"
    assert len(slots) == (z.spring_night - 60) // 15 + 1  # a one-hour block, a start every 15 minutes
    assert all(real(s.end) - real(s.start) == HOUR for s in slots)
    assert all(real(window.start) <= real(s.start) and real(s.end) <= real(window.end) for s in slots)
    assert all(b - a == QUARTER for a, b in zip(begins, begins[1:]))  # the grid does not hiccup across the jump
    counts = Counter(wall(s.start, z) for s in slots)
    assert not (set(counts) & z.skipped)
    assert {q: counts[q] for q in NIGHT_QUARTERS if q not in z.skipped} == {q: 1 for q in NIGHT_QUARTERS if q not in z.skipped}


@by_zone
def test_autumn_back_offers_both_passes_of_the_repeated_stretch(z: Zone) -> None:
    window = window_on(AUTUMN, dt.time(22), dt.time(6), z.zone)
    slots = slots_in(window, AUTUMN, z)
    begins = [real(s.start) for s in slots]
    assert len(set(begins)) == len(begins)
    assert len(slots) == (z.autumn_night - 60) // 15 + 1
    assert all(real(s.end) - real(s.start) == HOUR for s in slots)
    assert all(b - a == QUARTER for a, b in zip(begins, begins[1:]))
    counts = Counter(wall(s.start, z) for s in slots)
    assert {q: counts[q] for q in NIGHT_QUARTERS} == {q: 2 if q in z.repeated else 1 for q in NIGHT_QUARTERS}
    first, second = [s for s in slots if wall(s.start, z) == z.pick]
    assert (first.start.astimezone(z.zone).utcoffset(), second.start.astimezone(z.zone).utcoffset()) == (z.summer, z.winter)
    assert real(second.start) - real(first.start) == z.jump  # the two passes are one jump apart: an hour in Adelaide, half an hour on Lord Howe


@by_zone
def test_the_slot_grid_stays_on_the_clock_after_the_change(z: Zone) -> None:
    for day in (SPRING, AUTUMN):
        window = window_on(day, dt.time(22), dt.time(6), z.zone)
        assert {s.start.astimezone(z.zone).minute for s in slots_in(window, day, z)} == {0, 15, 30, 45}


@by_zone
def test_a_slot_that_starts_before_the_jump_and_ends_after_it_is_a_full_hour_long(z: Zone) -> None:
    window = window_on(SPRING, dt.time(22), dt.time(6), z.zone)
    (straddle,) = [s for s in slots_in(window, SPRING, z) if wall(s.start, z) == "01:30"]
    assert real(straddle.end) - real(straddle.start) == HOUR
    assert wall(straddle.end, z) == z.straddle_end


@by_zone
def test_ordinary_days_are_unchanged(z: Zone) -> None:
    window = window_on(ORDINARY, dt.time(9), dt.time(17), z.zone)
    slots = slots_in(window, ORDINARY, z)
    assert len(slots) == (8 - 1) * 4 + 1 and wall(slots[0].start, z) == "09:00" and wall(slots[-1].end, z) == "17:00"
    assert {s.start.astimezone(z.zone).utcoffset() for s in slots} == {z.winter}  # September: the clocks have not yet gone forward


# --------------------------------------------------------------------- the interval maths on those windows
@by_zone
def test_opening_hours_and_a_shift_intersect_in_real_time(z: Zone) -> None:
    shift = window_on(SPRING, dt.time(22), dt.time(6), z.zone)
    opening = window_on(SPRING, dt.time(20), dt.time(4), z.zone)
    both = intersect_intervals([shift], [opening])
    assert len(both) == 1 and both[0].minutes == 6 * 60 - z.jump.seconds // 60  # 22:00 to 04:00 on the clock, less what the jump skipped


@by_zone
def test_a_booking_inside_the_skipped_time_is_cut_out_correctly(z: Zone) -> None:
    window = window_on(SPRING, dt.time(22), dt.time(6), z.zone)
    busy = Interval(real(window.start) + 4 * HOUR, real(window.start) + 5 * HOUR)  # four to five real hours into the night: 02:00 winter time and the hour after
    left = subtract_intervals([window], [busy])
    assert [i.minutes for i in left] == [4 * 60, z.spring_night - 5 * 60]


@by_zone
def test_an_interval_given_local_times_still_measures_real_time(z: Zone) -> None:
    lo, hi = dt.datetime(2026, 10, 3, 22, 0, tzinfo=z.zone), dt.datetime(2026, 10, 4, 6, 0, tzinfo=z.zone)
    interval = Interval(lo, hi)
    assert interval.minutes == z.spring_night
    assert interval.start.utcoffset() == dt.timedelta(0) and interval.end.utcoffset() == dt.timedelta(0)
    assert [i.minutes for i in subtract_intervals([interval], [Interval(real(lo) + HOUR, real(lo) + 2 * HOUR)])] == [60, z.spring_night - 120]


@by_zone
def test_a_local_earliest_start_is_stepped_in_real_time(z: Zone) -> None:
    """The notice cut-off given in the business's zone, ten minutes before the clocks go back: the next start is ten real minutes later, in the repeated stretch too."""
    window = window_on(AUTUMN, dt.time(22), dt.time(6), z.zone)
    earliest = (transition(z, AUTUMN) - dt.timedelta(minutes=10)).astimezone(z.zone)
    slots = generate_slots([window], HOUR, QUARTER, midnight(AUTUMN, z), earliest, LATE)
    assert real(slots[0].start) - real(earliest) == dt.timedelta(minutes=10)  # the next quarter hour of real time is the moment the clocks change
    assert all(real(b.start) - real(a.start) == QUARTER for a, b in zip(slots, slots[1:]))
    assert wall(slots[0].start, z) == z.autumn_clock[1]


# --------------------------------------------------------------------- what each kind of business offers on the night
@pytest.mark.parametrize("kind", ["spa", "professional", "automotive"])
@pytest.mark.parametrize("forward", [True, False], ids=["spring", "autumn"])
@by_zone
async def test_a_night_across_the_clock_change_offers_a_start_for_every_quarter_hour_that_really_passes(
    client: httpx.AsyncClient, z: Zone, forward: bool, kind: str
) -> None:
    got = await offered(kind, client, next_change(forward=forward, zone=z.zone), z.name)
    night = z.spring_night if forward else z.autumn_night
    assert len(got) == (night - 60) // 15 + 1, [wall(t, z) for t in got]  # a one-hour service, a start every 15 minutes
    assert len({real(t) for t in got}) == len(got)  # never two at the same moment
    assert all(t.utcoffset() == t.astimezone(z.zone).utcoffset() for t in got)  # written in the zone's own offset, not in UTC
    assert wall(got[0], z) == "22:00" and wall(got[-1], z) == "05:00"
    assert (got[-1] - got[0]).total_seconds() == (night - 60) * 60
    assert all(b - a == QUARTER for a, b in zip(got, got[1:]))
    counts = Counter(wall(t, z) for t in got)
    expected = {q: (0 if q in z.skipped else 1) for q in NIGHT_QUARTERS} if forward else {q: (2 if q in z.repeated else 1) for q in NIGHT_QUARTERS}
    assert {q: counts[q] for q in NIGHT_QUARTERS} == expected
    assert set(counts) <= set(NIGHT_QUARTERS)


@pytest.mark.parametrize("forward", [True, False], ids=["spring", "autumn"])
@by_zone
async def test_every_slot_is_an_hour_of_real_time_and_ends_on_the_clock_of_its_own_zone(client: httpx.AsyncClient, z: Zone, forward: bool) -> None:
    s = await night_spa(client, z.name)
    slots = (await avail(s, day=next_change(forward=forward, zone=z.zone))).json()["slots"]
    assert len(slots) == ((z.spring_night if forward else z.autumn_night) - 60) // 15 + 1
    for slot in slots:
        begin, end = dt.datetime.fromisoformat(slot["start_at"]), dt.datetime.fromisoformat(slot["end_at"])
        assert end - begin == HOUR  # (aware datetimes subtract by instant, whatever offset each is written in)
        assert end.utcoffset() == end.astimezone(z.zone).utcoffset() and begin.utcoffset() == begin.astimezone(z.zone).utcoffset()
    assert {dt.datetime.fromisoformat(x["end_at"]).utcoffset() for x in slots} == ({z.winter, z.summer} if forward else {z.summer, z.winter})


@pytest.mark.parametrize("forward", [True, False], ids=["spring", "autumn"])
@by_zone
async def test_a_restaurant_with_a_fixed_grid_seats_for_the_real_length_of_the_night_too(client: httpx.AsyncClient, z: Zone, forward: bool) -> None:
    rest = await late_restaurant(client, z.name)
    await put_slots(rest, booking_mode="fixed", slot_duration_minutes=90, slot_interval_minutes=30, first_slot_time="18:00:00", last_slot_time="03:00:00", buffer_minutes=15)
    got = starts(await avail(rest, day=next_change(forward=forward, zone=z.zone), party_size=2))
    night = 9 * 60 + (-1 if forward else 1) * z.jump.seconds // 60
    assert len(got) == (night - 90) // 30 + 1 and len({real(t) for t in got}) == len(got)
    assert all(t.utcoffset() == t.astimezone(z.zone).utcoffset() for t in got)
    assert wall(got[0], z) == "18:00" and all(b - a == dt.timedelta(minutes=30) for a, b in zip(got, got[1:]))
    if forward:
        assert not {wall(t, z) for t in got} & z.skipped


@pytest.mark.parametrize("forward", [True, False], ids=["spring", "autumn"])
@by_zone
async def test_a_restaurant_open_past_midnight_seats_for_the_real_length_of_the_night(client: httpx.AsyncClient, z: Zone, forward: bool) -> None:
    """18:00 to 03:00 is nine hours on the clock; 90-minute seatings every half hour."""
    got = await offered("restaurant", client, next_change(forward=forward, zone=z.zone), z.name)
    night = 9 * 60 + (-1 if forward else 1) * z.jump.seconds // 60
    assert len(got) == (night - 90) // 30 + 1 and len({real(t) for t in got}) == len(got)
    assert all(t.utcoffset() == t.astimezone(z.zone).utcoffset() for t in got)
    assert wall(got[0], z) == "18:00"
    assert all(b - a == dt.timedelta(minutes=30) for a, b in zip(got, got[1:]))
    if forward:
        assert not {wall(t, z) for t in got} & z.skipped


# --------------------------------------------------------------------- what can be booked
@by_zone
async def test_the_repeated_stretch_can_be_booked_on_each_pass_and_a_double_booking_is_refused(client: httpx.AsyncClient, z: Zone) -> None:
    s = await night_spa(client, z.name)
    day = next_change(forward=False, zone=z.zone)
    before = starts(await avail(s, day=day))
    first, second = [t for t in before if wall(t, z) == z.pick]
    assert (first.utcoffset(), second.utcoffset()) == (z.summer, z.winter)
    booked = await s.client.post("/v1/public/bookings", json=s.booking_body(second), headers=s.pk)  # the later pass of the repeated time
    assert booked.status_code == 201, booked.text
    after = {real(t) for t in starts(await avail(s, day=day))}
    # a start is free exactly when its hour does not overlap the booking (computed here from the instants, not from the zone)
    assert after == {real(t) for t in before if real(t) + HOUR <= real(second) or real(t) >= real(second) + HOUR}
    assert (real(first) in after) is (not z.passes_overlap)  # an hour apart: back to back; half an hour apart: the first pass now overlaps the booking
    assert real(second) not in after and real(second + HOUR) in after
    again = await s.client.post("/v1/public/bookings", json=s.booking_body(second), headers=s.pk)
    assert again.status_code in (409, 422), again.text  # the same moment cannot be had twice
    for listed in (await s.client.get("/v1/admin/bookings", headers=s.admin)).json():
        assert dt.datetime.fromisoformat(listed["end_at"]) - dt.datetime.fromisoformat(listed["start_at"]) == HOUR


@by_zone
async def test_the_first_pass_is_booked_first_and_the_second_pass_follows_only_when_it_does_not_overlap(client: httpx.AsyncClient, z: Zone) -> None:
    s = await night_spa(client, z.name)
    day = next_change(forward=False, zone=z.zone)
    first, second = [t for t in starts(await avail(s, day=day)) if wall(t, z) == z.pick]
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(first), headers=s.pk)).status_code == 201
    later = await s.client.post("/v1/public/bookings", json=s.booking_body(second), headers=s.pk)
    if z.passes_overlap:
        assert later.status_code in (409, 422), later.text  # 01:30 the second time begins half an hour into the first booking
    else:
        assert later.status_code == 201, later.text  # the first booking ends as the second begins
    assert len((await s.client.get("/v1/admin/bookings", headers=s.admin)).json()) == (1 if z.passes_overlap else 2)


@by_zone
async def test_a_booking_that_spans_the_time_the_clocks_skip_is_an_hour_of_real_time(client: httpx.AsyncClient, z: Zone) -> None:
    s = await night_spa(client, z.name)
    day = next_change(forward=True, zone=z.zone)
    before = starts(await avail(s, day=day))
    straddle = next(t for t in before if wall(t, z) == "01:30")
    booked = await s.client.post("/v1/public/bookings", json=s.booking_body(straddle), headers=s.pk)
    assert booked.status_code == 201, booked.text
    (listed,) = (await s.client.get("/v1/admin/bookings", headers=s.admin)).json()
    start, end = dt.datetime.fromisoformat(listed["start_at"]), dt.datetime.fromisoformat(listed["end_at"])
    assert end - start == HOUR and wall(end, z) == z.straddle_end
    assert end.astimezone(z.zone).utcoffset() == z.summer and start.astimezone(z.zone).utcoffset() == z.winter  # it starts on the old clock and ends on the new
    after = {real(t) for t in starts(await avail(s, day=day))}
    assert after == {real(t) for t in before if real(t) + HOUR <= real(start) or real(t) >= real(end)}  # what is left is what does not overlap the booking, by the instants
    assert real(straddle) not in after and len(after) < len(before)


@by_zone
async def test_a_seating_that_runs_across_the_stretch_the_clocks_go_back_still_lasts_its_real_length(client: httpx.AsyncClient, z: Zone) -> None:
    rest = await late_restaurant(client, z.name)
    day = next_change(forward=False, zone=z.zone)
    start = next(t for t in starts(await avail(rest, day=day, party_size=2)) if wall(t, z) == "01:30")  # the first of its two passes
    assert start.utcoffset() == z.summer
    r = await client.post("/v1/public/bookings", json=body(rest, start, 2), headers=rest.pk)
    assert r.status_code == 201, r.text
    (listed,) = (await client.get("/v1/admin/bookings", headers=rest.admin)).json()
    begin, end = dt.datetime.fromisoformat(listed["start_at"]), dt.datetime.fromisoformat(listed["end_at"])
    assert end - begin == dt.timedelta(minutes=90 + 15) and wall(end, z) == z.seating_end


@by_zone
async def test_an_after_midnight_seating_on_the_night_the_clocks_go_forward_blocks_its_table_and_the_slots_around_it(client: httpx.AsyncClient, z: Zone) -> None:
    """One table seats six: a booking at 00:00 keeps every overlapping seating off it, wherever the jump falls."""
    rest = await late_restaurant(client, z.name)
    day = next_change(forward=True, zone=z.zone)
    slots = starts(await avail(rest, day=day, party_size=6))
    midnight_slot = next(t for t in slots if wall(t, z) == "00:00" and t.astimezone(z.zone).date() > day)
    assert (await client.post("/v1/public/bookings", json=body(rest, midnight_slot, 6), headers=rest.pk)).status_code == 201
    left = [wall(t, z) for t in starts(await avail(rest, day=day, party_size=6))]
    assert "22:00" in left  # done with its buffer by 23:45
    assert not {"22:30", "23:00", "23:30", "00:00", "00:30"} & set(left)


# --------------------------------------------------------------------- the shared set-up is what it says
def test_the_helpers_use_the_fixed_widths_the_expectations_assume() -> None:
    assert NIGHT[0] == {"weekday": 0, "start_time": "22:00:00", "end_time": "06:00:00"}
    assert LATE_HOURS[0] == {"weekday": 0, "open_time": "18:00:00", "close_time": "03:00:00"}
    assert NIGHT_QUARTERS[0] == "22:00" and NIGHT_QUARTERS[-1] == "05:00" and len(NIGHT_QUARTERS) == 29


def test_the_search_for_the_next_change_looks_at_the_zone_it_is_asked_about() -> None:
    """Lord Howe and Sydney change on the same Saturday night; Brisbane never does."""
    for z in ZONES:
        for forward in (True, False):
            assert next_change(forward=forward, zone=z.zone) == next_change(forward=forward)
    with pytest.raises(AssertionError, match="no clock change found"):
        next_change(forward=True, zone=ZoneInfo("Australia/Brisbane"))


# --------------------------------------------------------------------- clock times that do not exist, or happen twice
#: A shift start written for a time the jump skips: what the clock reads when it starts, and how long until 06:00.  ``(time, reading, real minutes)``
SKIPPED_START = {
    "Australia/Melbourne": (dt.time(2, 30), "03:30", 150), "Australia/Hobart": (dt.time(2, 30), "03:30", 150),
    "Australia/Adelaide": (dt.time(2, 30), "03:30", 150), "Australia/Lord_Howe": (dt.time(2, 15), "02:45", 195),
}
#: A break written inside the stretch that happens twice, and its real length (it starts the first time round): ``(start, end, real minutes)``
REPEATED_BREAK = {
    "Australia/Melbourne": (dt.time(2, 30), dt.time(3), 90), "Australia/Hobart": (dt.time(2, 30), dt.time(3), 90),
    "Australia/Adelaide": (dt.time(2, 30), dt.time(3), 90), "Australia/Lord_Howe": (dt.time(1, 45), dt.time(2, 15), 60),
}


@by_zone
def test_a_shift_written_for_a_time_the_clocks_skip_starts_at_the_moment_after_the_jump(z: Zone) -> None:
    at, reading, minutes = SKIPPED_START[z.name]
    window = window_on(SPRING + dt.timedelta(days=1), at, dt.time(6), z.zone)  # the Sunday itself: the skipped time is on it
    assert wall(window.start, z) == reading and window.minutes == minutes
    # the time typed is read with the clock as it was before the jump and so lands that far past the jump (02:30 is 30 minutes past 02:00 on a one-hour jump)
    assert real(window.start) - transition(z, SPRING) == dt.timedelta(hours=at.hour, minutes=at.minute) - dt.timedelta(hours=2)


@by_zone
def test_a_break_written_inside_the_stretch_that_happens_twice_starts_the_first_time_round(z: Zone) -> None:
    start, end, minutes = REPEATED_BREAK[z.name]
    shift: Any = SimpleNamespace(start_time=dt.time(22), end_time=dt.time(6), break_start=start, break_end=end)
    before, after = shift_windows(shift, AUTUMN, z.zone)
    assert real(after.start) - real(before.end) == dt.timedelta(minutes=minutes)
    assert before.end.astimezone(z.zone).utcoffset() == z.summer  # the first pass is on the summer clock


# --------------------------------------------------------------------- the admin list for one date
@pytest.mark.parametrize("forward", [True, False], ids=["spring", "autumn"])
@by_zone
async def test_the_admin_list_for_a_date_covers_the_whole_local_day_even_when_it_is_23_or_25_hours_long(client: httpx.AsyncClient, z: Zone, forward: bool) -> None:
    """Sunday is 23 real hours when the clocks go forward and 25 when they go back: a booking at its very end belongs to it, one just after midnight does not."""
    s = await night_spa(client, z.name)
    saturday = next_change(forward=forward, zone=z.zone)
    sunday, monday = saturday + dt.timedelta(days=1), saturday + dt.timedelta(days=2)

    def pick(slots: list[dt.datetime], reading: str, date: dt.date) -> dt.datetime:
        return next(t for t in slots if wall(t, z) == reading and t.astimezone(z.zone).date() == date)

    early = pick(starts(await avail(s, day=saturday)), "00:30", sunday)  # still Saturday's night shift
    sunday_slots = starts(await avail(s, day=sunday))
    late = pick(sunday_slots, "22:00" if forward else "23:30", sunday)  # the last start of Sunday that the 23-hour or 25-hour day can hold
    over = pick(sunday_slots, "00:15" if forward else "01:00", monday)  # (one staff member: the two must not overlap)
    for t in (early, late, over):
        assert (await s.client.post("/v1/public/bookings", json=s.booking_body(t), headers=s.pk)).status_code == 201

    async def listed(day: dt.date) -> set[dt.datetime]:
        r = await s.client.get(f"/v1/admin/bookings?date={day.isoformat()}", headers=s.admin)
        assert r.status_code == 200, r.text
        return {real(dt.datetime.fromisoformat(b["start_at"])) for b in r.json()}

    assert await listed(sunday) == {real(early), real(late)}
    assert await listed(monday) == {real(over)}
    assert await listed(saturday) == set()


# --------------------------------------------------------------------- a known limit: a grid whose step does not divide the clock change
@by_zone
def test_an_hourly_grid_stays_on_the_hour_after_a_one_hour_change_but_not_after_lord_howes_half_hour_change(z: Zone) -> None:
    """The slot grid steps in real time from midnight (``generate_slots``), so it keeps to the wall clock only when the step divides the change.

    With the default 15-minute step (or 30) that is always so. With an hourly step (``SLOT_STEP_MINUTES=60``, or a restaurant interval of an hour) it is so in
    Melbourne, Hobart and Adelaide, whose clocks move by an hour, but NOT on Lord Howe, whose clocks move by half an hour: after the change its slots read
    xx:30 instead of xx:00 until the clocks change back. This test records that; it is a limit of the design, not something to be relied on.
    """
    for day in (SPRING, AUTUMN):
        window = window_on(day, dt.time(22), dt.time(6), z.zone)
        slots = slots_in(window, day, z, step=HOUR)
        minutes = [s.start.astimezone(z.zone).minute for s in slots]
        if z.jump % HOUR == dt.timedelta(0):
            assert set(minutes) == {0}
        else:
            assert set(minutes) == {0, 30} and minutes[:4] == [0, 0, 0, 0] and minutes[-3:] == [30, 30, 30]
