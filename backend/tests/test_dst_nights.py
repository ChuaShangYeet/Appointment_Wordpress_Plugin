"""Every kind of business on the night the clocks change in Sydney (the unit maths is in ``test_dst.py``): what it offers, and what can be booked."""
from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.conftest import Seed, make_seed, set_zone
from tests.test_automotive import Garage, gslots, make_garage
from tests.test_availability_cache import avail
from tests.test_field_service import Practice, make_practice, pslots
from tests.test_restaurant import Restaurant, body, make_restaurant

SYDNEY = "Australia/Sydney"
SYD = ZoneInfo(SYDNEY)
UTC = dt.UTC
HOUR = dt.timedelta(hours=1)
NIGHT = [{"weekday": d, "start_time": "22:00:00", "end_time": "06:00:00"} for d in range(7)]
NIGHT_HOURS = [{"weekday": d, "open_time": "22:00:00", "close_time": "06:00:00"} for d in range(7)]
LATE_HOURS = [{"weekday": d, "open_time": "18:00:00", "close_time": "03:00:00"} for d in range(7)]


def next_change(*, forward: bool, zone: ZoneInfo = SYD) -> dt.date:
    """The first Saturday, a week or more from now, whose night crosses a clock change in ``zone`` (forward = spring, else autumn; the same Sundays in every south-eastern zone)."""
    day = dt.date.today() + dt.timedelta(days=7)
    for _ in range(400):
        before = dt.datetime.combine(day, dt.time(12), tzinfo=zone).utcoffset()
        after = dt.datetime.combine(day + dt.timedelta(days=1), dt.time(12), tzinfo=zone).utcoffset()
        assert before is not None and after is not None
        if (after > before) if forward else (after < before):
            assert day.isoweekday() == 6
            return day
        day += dt.timedelta(days=1)
    raise AssertionError("no clock change found")


async def in_zone(tenant_id: uuid.UUID, zone: str) -> None:
    await set_zone(tenant_id, zone, country="AU")


async def widen(s: Seed) -> None:
    """A booking window long enough to reach the next clock change (the helpers' own policy is 60 days)."""
    policy = {"name": "Long", "default_status": "confirmed", "min_notice_booking": 60, "min_notice_cancel": 86400, "booking_window": 500 * 86400,
              "pending_timeout": 1800, "make_default": True}
    assert (await s.client.post("/v1/admin/policies", headers=s.admin, json=policy)).status_code == 201


def starts(r: httpx.Response) -> list[dt.datetime]:
    return [dt.datetime.fromisoformat(x["start_at"]) for x in r.json()["slots"]]


async def night_spa(client: httpx.AsyncClient, zone: str = SYDNEY) -> Seed:
    s = await make_seed(client)
    await in_zone(s.tenant_id, zone)
    await widen(s)
    staff = await s.add_staff("Mei", shifts=False)
    assert (await client.put(f"/v1/admin/resources/{staff}/shifts", json=NIGHT, headers=s.admin)).status_code == 200
    return s


async def night_practice(client: httpx.AsyncClient, zone: str = SYDNEY) -> Practice:
    p = await make_practice(client, radius=None)
    await in_zone(p.tenant_id, zone)
    await widen(p)
    assert (await client.put("/v1/admin/operating-hours", json=NIGHT_HOURS, headers=p.admin)).status_code == 200
    for staff in p.pros.values():
        assert (await client.put(f"/v1/admin/resources/{staff}/shifts", json=NIGHT, headers=p.admin)).status_code == 200
    return p


async def night_garage(client: httpx.AsyncClient, zone: str = SYDNEY) -> Garage:
    g = await make_garage(client)
    await in_zone(g.tenant_id, zone)
    await widen(g)
    assert (await client.put("/v1/admin/operating-hours", json=NIGHT_HOURS, headers=g.admin)).status_code == 200
    for staff in g.mechanics.values():
        assert (await client.put(f"/v1/admin/resources/{staff}/shifts", json=NIGHT, headers=g.admin)).status_code == 200
    return g


async def late_restaurant(client: httpx.AsyncClient, zone: str = SYDNEY) -> Restaurant:
    r = await make_restaurant(client)
    await in_zone(r.tenant_id, zone)
    await widen(r)
    assert (await client.put("/v1/admin/operating-hours", json=LATE_HOURS, headers=r.admin)).status_code == 200
    return r


async def offered(kind: str, client: httpx.AsyncClient, day: dt.date, zone: str = SYDNEY) -> list[dt.datetime]:
    """The start times one kind of business offers on ``day`` (a one-hour service, or for the restaurant a 90-minute seating for two)."""
    if kind == "spa":
        return starts(await avail(await night_spa(client, zone), day=day))
    if kind == "professional":
        return starts(await pslots(await night_practice(client, zone), "office", date=day.isoformat()))
    if kind == "automotive":
        return [dt.datetime.fromisoformat(x["start_at"]) for x in await gslots(await night_garage(client, zone), ["oil"], date=day.isoformat())]
    return starts(await avail(await late_restaurant(client, zone), day=day, party_size=2))


def clock(t: dt.datetime) -> str:
    return t.astimezone(SYD).strftime("%H:%M")


@pytest.mark.parametrize("kind", ["spa", "professional", "automotive"])
@pytest.mark.parametrize(("forward", "real_hours"), [(True, 7), (False, 9)])
async def test_a_night_across_the_clock_change_offers_a_start_for_every_quarter_hour_that_really_passes(
    client: httpx.AsyncClient, kind: str, forward: bool, real_hours: int
) -> None:
    got = await offered(kind, client, next_change(forward=forward))
    assert len(got) == (real_hours - 1) * 4 + 1, [t.astimezone(SYD).strftime("%a %H:%M") for t in got]  # a one-hour service, a start every 15 minutes
    assert len({t.astimezone(UTC) for t in got}) == len(got)  # never two at the same moment
    assert all(t.utcoffset() == t.astimezone(SYD).utcoffset() for t in got)  # and written in Sydney's own offset (+10:00 or +11:00), not in UTC
    assert clock(got[0]) == "22:00" and clock(got[-1]) == "05:00"
    assert (got[-1] - got[0]).total_seconds() == (real_hours - 1) * 3600
    hours = [t.astimezone(SYD).hour for t in got]
    assert (2 not in hours) if forward else (hours.count(2) == 8)  # the skipped hour is not offered; the hour that happens twice is offered twice


@pytest.mark.parametrize(("forward", "count"), [(True, 14), (False, 18)])
async def test_a_restaurant_open_past_midnight_seats_for_the_real_length_of_the_night(client: httpx.AsyncClient, forward: bool, count: int) -> None:
    """18:00 to 03:00 is 8 real hours when the clocks go forward and 10 when they go back; 90-minute seatings every half hour."""
    got = await offered("restaurant", client, next_change(forward=forward))
    assert len(got) == count and len({t.astimezone(UTC) for t in got}) == count
    assert all(t.utcoffset() == t.astimezone(SYD).utcoffset() for t in got)
    assert clock(got[0]) == "18:00"
    hours = [t.astimezone(SYD).hour for t in got]
    assert (2 not in hours) if forward else (hours.count(2) == 2)  # (only the first pass of 02:00-03:00: the second is too late for a 90-minute seating before 03:00)


async def test_the_repeated_hour_can_be_booked_once_per_pass_and_never_twice(client: httpx.AsyncClient) -> None:
    s = await night_spa(client)
    day = next_change(forward=False)
    slots = starts(await avail(s, day=day))
    summer, winter = [t for t in slots if clock(t) == "02:30"]  # 02:30 on summer time (+11:00), then 02:30 again on standard time (+10:00)
    assert (summer.utcoffset(), winter.utcoffset()) == (dt.timedelta(hours=11), dt.timedelta(hours=10))
    booked = await s.client.post("/v1/public/bookings", json=s.booking_body(winter), headers=s.pk)  # the second 02:30
    assert booked.status_code == 201, booked.text
    after = {t.astimezone(UTC) for t in starts(await avail(s, day=day))}
    assert summer.astimezone(UTC) in after  # the first 02:30 ends exactly as the second begins: still free
    assert winter.astimezone(UTC) not in after and (winter - dt.timedelta(minutes=15)).astimezone(UTC) not in after
    assert (winter + HOUR).astimezone(UTC) in after
    again = await s.client.post("/v1/public/bookings", json=s.booking_body(winter), headers=s.pk)
    assert again.status_code in (409, 422), again.text  # the same moment cannot be had twice


async def test_a_booking_that_spans_the_hour_the_clocks_skip_is_an_hour_of_real_time(client: httpx.AsyncClient) -> None:
    s = await night_spa(client)
    day = next_change(forward=True)
    straddle = next(t for t in starts(await avail(s, day=day)) if clock(t) == "01:30")
    booked = await s.client.post("/v1/public/bookings", json=s.booking_body(straddle), headers=s.pk)
    assert booked.status_code == 201, booked.text
    (listed,) = (await s.client.get("/v1/admin/bookings", headers=s.admin)).json()
    start, end = dt.datetime.fromisoformat(listed["start_at"]), dt.datetime.fromisoformat(listed["end_at"])
    assert end - start == HOUR and clock(end) == "03:30"
    after = [clock(t) for t in starts(await avail(s, day=day))]
    assert "01:15" not in after and "01:30" not in after and "03:15" not in after and "03:30" in after  # (03:00-03:30 on the clock was still inside it)


async def test_a_clock_that_never_changes_is_not_affected(client: httpx.AsyncClient) -> None:
    s = await make_seed(client)
    await set_zone(s.tenant_id, "Australia/Brisbane", country="AU")
    await widen(s)
    staff = await s.add_staff("Mei", shifts=False)
    assert (await client.put(f"/v1/admin/resources/{staff}/shifts", json=NIGHT, headers=s.admin)).status_code == 200
    for forward in (True, False):
        got = starts(await avail(s, day=next_change(forward=forward)))
        assert len(got) == (8 - 1) * 4 + 1 and got[0].utcoffset() == got[-1].utcoffset() == dt.timedelta(hours=10)


async def test_an_after_midnight_seating_blocks_its_table_and_the_slots_around_it(client: httpx.AsyncClient) -> None:
    """A party of six has one table (``d6``): a booking at 00:00 on the night the clocks go forward keeps the overlapping seatings off it."""
    rest = await late_restaurant(client)
    day = next_change(forward=True)
    slots = starts(await avail(rest, day=day, party_size=6))
    midnight_slot = next(t for t in slots if clock(t) == "00:00" and t.date() > day)
    r = await client.post("/v1/public/bookings", json=body(rest, midnight_slot, 6), headers=rest.pk)
    assert r.status_code == 201, r.text
    left = [clock(t) for t in starts(await avail(rest, day=day, party_size=6))]
    assert "22:00" in left  # done with its buffer by 23:45
    assert not {"22:30", "23:00", "23:30", "00:00", "00:30"} & set(left)  # a seating is 90 minutes plus a 15-minute buffer, so each of these overlaps the booking


async def test_the_repeated_hour_can_be_booked_on_either_pass_and_each_is_an_hour_long(client: httpx.AsyncClient) -> None:
    s = await night_spa(client)
    day = next_change(forward=False)
    summer, winter = [t for t in starts(await avail(s, day=day)) if clock(t) == "02:30"]
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(winter), headers=s.pk)).status_code == 201
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(summer), headers=s.pk)).status_code == 201  # ends as the other begins
    for listed in (await s.client.get("/v1/admin/bookings", headers=s.admin)).json():
        assert dt.datetime.fromisoformat(listed["end_at"]) - dt.datetime.fromisoformat(listed["start_at"]) == HOUR


async def test_a_seating_that_runs_across_the_hour_the_clocks_go_back_still_lasts_its_real_length(client: httpx.AsyncClient) -> None:
    rest = await late_restaurant(client)
    day = next_change(forward=False)
    start = next(t for t in starts(await avail(rest, day=day, party_size=2)) if clock(t) == "01:30")
    r = await client.post("/v1/public/bookings", json=body(rest, start, 2), headers=rest.pk)
    assert r.status_code == 201, r.text
    (listed,) = (await client.get("/v1/admin/bookings", headers=rest.admin)).json()
    assert dt.datetime.fromisoformat(listed["end_at"]) - dt.datetime.fromisoformat(listed["start_at"]) == dt.timedelta(minutes=90 + 15)  # 01:30 summer time to 02:15 standard time
