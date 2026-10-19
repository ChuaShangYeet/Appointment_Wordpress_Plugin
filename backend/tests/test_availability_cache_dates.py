"""Per-date invalidation of the availability cache: a booking clears only the dates around it, everything else still clears everything."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import Range

from app.core import availability_cache
from app.core.availability_cache import MAX_SPAN_DAYS, dates_around
from app.core.config import get_settings
from app.core.database import tenant_session
from app.models import Booking, BookingAllocation, OperatingHours
from app.services import booking as booking_module
from tests.conftest import PLATFORM, Seed, at, make_seed, set_zone
from tests.test_automotive import gbook, make_garage
from tests.test_availability_cache import avail, cache, hhmm, truth  # noqa: F401  (``cache`` is a fixture)
from tests.test_availability_month import counting  # noqa: F401  (a fixture)
from tests.test_customer_cancel import login, otp  # noqa: F401  (``otp`` is a fixture)
from tests.test_restaurant import book as rbook
from tests.test_restaurant import make_restaurant

DAY = dt.timedelta(days=1)


async def statuses(s: Seed, days: list[dt.date], **kw: Any) -> dict[dt.date, str]:
    """``X-Cache`` of one request per date (each request also re-warms its date)."""
    return {d: (await avail(s, day=d, **kw)).headers["X-Cache"] for d in days}


async def warm(s: Seed, days: list[dt.date], **kw: Any) -> None:
    for d in days:
        await avail(s, day=d, **kw)
    assert set((await statuses(s, days, **kw)).values()) == {"HIT"}


async def assert_all_true(s: Seed, days: list[dt.date], monkeypatch: pytest.MonkeyPatch, **kw: Any) -> None:
    """Every date the cache will answer is what a fresh computation says: nothing stale survived."""
    for d in days:
        served = await avail(s, day=d, **kw)
        assert served.json() == await truth(s, monkeypatch, day=d, **kw), f"stale answer for {d}"


def around(day: dt.date, before: int, after: int) -> list[dt.date]:
    return [day + n * DAY for n in range(-before, after + 1)]


async def tokens(cache: Any, s: Seed) -> dict[str, str | None]:
    return {k: await cache.get(k) for k in sorted(await cache.keys(f"avail:gen:{s.tenant_id}*"))}


# --------------------------------------------------------------------- pure: the dates a reservation can affect
def _t(day: str, hh: int, mm: int = 0, tz: str = "UTC") -> dt.datetime:
    return dt.datetime.combine(dt.date.fromisoformat(day), dt.time(hh, mm), tzinfo=ZoneInfo(tz))


def test_dates_around_a_reservation() -> None:
    got = dates_around(_t("2030-01-10", 10), _t("2030-01-10", 11))
    assert got == {dt.date(2030, 1, 10) + n * DAY for n in range(-3, 3)}  # from 3 days before to 2 after
    two_days = dates_around(_t("2030-01-10", 22), _t("2030-01-12", 1))
    assert two_days == {dt.date(2030, 1, 7) + n * DAY for n in range(0, 8)}  # the span, widened on both sides


def test_dates_are_taken_in_utc_whatever_zone_the_times_carry() -> None:
    kl = dates_around(_t("2030-01-10", 1, tz="Asia/Kuala_Lumpur"), _t("2030-01-10", 2, tz="Asia/Kuala_Lumpur"))  # 17:00 UTC the day before
    assert kl == dates_around(_t("2030-01-09", 17), _t("2030-01-09", 18))
    assert min(kl or set()) == dt.date(2030, 1, 6)


def test_what_cannot_be_dated_clears_everything() -> None:
    assert dates_around(None, _t("2030-01-10", 11)) is None and dates_around(_t("2030-01-10", 11), None) is None
    assert dates_around(_t("2030-01-10", 11), _t("2030-01-10", 10)) is None  # inverted
    assert dates_around(_t("2030-01-01", 0), _t("2030-01-01", 0) + (MAX_SPAN_DAYS + 1) * DAY) is None
    assert dates_around(_t("2030-01-01", 0), _t("2030-01-01", 0) + MAX_SPAN_DAYS * DAY) is not None  # exactly at the limit is still dated


# --------------------------------------------------------------------- a booking clears the dates around it, not the rest
async def test_a_booking_clears_only_the_dates_around_it(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    days = around(s.day, 10, 10)
    await warm(s, days)
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).status_code == 201
    got = await statuses(s, days)
    assert got[s.day] == "MISS" and got[s.day - DAY] == "MISS" and got[s.day + DAY] == "MISS"
    assert all(got[d] == "HIT" for d in around(s.day, 10, 10) if abs((d - s.day).days) >= 5), got  # the rest of the month is untouched
    assert "10:00" not in hhmm(await avail(s))
    await assert_all_true(s, days, monkeypatch)


async def test_cancelling_a_booking_clears_only_its_dates(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).json()["id"]
    days = around(s.day, 8, 8)
    await warm(s, days)
    assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=s.admin)).status_code == 200
    got = await statuses(s, days)  # (a bulk UPDATE of the reservations: the dates were read from the rows before it ran)
    assert got[s.day] == "MISS" and all(got[d] == "HIT" for d in days if abs((d - s.day).days) >= 5), got
    assert "10:00" in hhmm(await avail(s))
    await assert_all_true(s, days, monkeypatch)


async def test_moving_a_booking_clears_the_old_dates_and_the_new_ones(client: httpx.AsyncClient, cache: Any, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_customer_reschedule import move

    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei, customer={"name": "A", "phone": "+60123450001"}), headers=s.pk)).json()["id"]
    far = s.day + 12 * DAY
    days = sorted({*around(s.day, 8, 8), *around(far, 8, 8)})
    await warm(s, days)
    r = await move(s, await login(s), bid, at(far, 14))
    assert r.status_code == 200, r.text
    got = await statuses(s, days)
    assert got[s.day] == "MISS" and got[far] == "MISS"  # where it was and where it went
    assert got[s.day + 6 * DAY] == "HIT" and got[far + 7 * DAY] == "HIT" and got[s.day - 8 * DAY] == "HIT"
    await assert_all_true(s, days, monkeypatch)


async def test_a_hold_that_ran_out_clears_only_its_dates(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client, default_status="pending")
    mei = await s.add_staff("Mei")
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)
    assert r.status_code == 201 and r.json()["status"] == "pending"
    days = around(s.day, 8, 8)
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:
        assert await booking_module.release_expired_holds(db, dt.datetime.now(dt.UTC) + dt.timedelta(hours=2)) == 1
    got = await statuses(s, days)
    assert got[s.day] == "MISS" and got[s.day + 6 * DAY] == "HIT" and got[s.day - 6 * DAY] == "HIT"
    await assert_all_true(s, days, monkeypatch)


async def test_a_change_to_the_business_clears_every_date(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    days = around(s.day, 8, 8)
    await warm(s, days)
    shift = [{"weekday": d, "start_time": "10:00:00", "end_time": "18:00:00"} for d in range(7)]
    assert (await s.client.put(f"/v1/admin/resources/{mei}/shifts", json=shift, headers=s.admin)).status_code == 200
    assert set((await statuses(s, days)).values()) == {"MISS"}
    await assert_all_true(s, days, monkeypatch)
    assert "09:00" not in hhmm(await avail(s))


async def test_one_transaction_with_a_booking_change_and_a_settings_change_clears_every_date(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = uuid.UUID((await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).json()["id"])
    days = around(s.day, 8, 8)
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:
        booking = await db.get(Booking, bid)
        assert booking is not None
        booking.hold_expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)  # date-scoped...
        await db.execute(insert(OperatingHours).values(tenant_id=s.tenant_id, weekday=1, open_time=dt.time(9), close_time=dt.time(18)))  # ...and a tenant-wide table
    assert set((await statuses(s, days)).values()) == {"MISS"}


async def test_a_reservation_that_spans_weeks_clears_every_date(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).json()["id"]
    days = around(s.day, 8, 8)
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:  # (leave: an unusually long period is not worth dating)
        alloc = await db.scalar(select(BookingAllocation).where(BookingAllocation.booking_id == uuid.UUID(bid)))
        assert alloc is not None
        alloc.period = Range(at(s.day, 10), at(s.day, 10) + (MAX_SPAN_DAYS + 5) * DAY, bounds="[)")
    assert set((await statuses(s, days)).values()) == {"MISS"}


async def test_a_bulk_statement_with_no_condition_clears_every_date(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)
    days = around(s.day, 8, 8)
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:
        await db.execute(update(BookingAllocation).values(state="released"))  # no WHERE: which rows? all of them
    assert set((await statuses(s, days)).values()) == {"MISS"}


async def test_a_bulk_statement_whose_rows_cannot_be_read_first_clears_every_date(
    client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).json()["id"]
    days = around(s.day, 8, 8)
    await warm(s, days)

    def boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("cannot read")

    with monkeypatch.context() as m:
        m.setattr(availability_cache, "select", boom)
        assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=s.admin)).status_code == 200
    assert set((await statuses(s, days)).values()) == {"MISS"}


async def test_a_bulk_update_of_columns_availability_does_not_read_clears_nothing(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = uuid.UUID((await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).json()["id"])
    days = around(s.day, 3, 3)
    await warm(s, days)
    before = await tokens(cache, s)
    async with tenant_session(s.tenant_id) as db:
        await db.execute(update(Booking).where(Booking.id == bid).values(address_id=None, special_request="window seat", flags=[]))
    assert await tokens(cache, s) == before and set((await statuses(s, days)).values()) == {"HIT"}
    async with tenant_session(s.tenant_id) as db:
        await db.execute(update(Booking).where(Booking.id == bid).values(hold_expires_at=dt.datetime.now(dt.UTC)))  # this one is read
    assert await tokens(cache, s) != before


async def test_a_rolled_back_booking_clears_nothing(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = uuid.UUID((await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).json()["id"])
    days = around(s.day, 3, 3)
    await warm(s, days)
    before = await tokens(cache, s)
    with pytest.raises(RuntimeError):
        async with tenant_session(s.tenant_id) as db:
            await db.execute(update(BookingAllocation).where(BookingAllocation.booking_id == bid).values(state="released"))
            raise RuntimeError("something went wrong")
    assert await tokens(cache, s) == before and set((await statuses(s, days)).values()) == {"HIT"}


async def test_another_businesss_booking_clears_nothing_here(client: httpx.AsyncClient, cache: Any) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await a.add_staff("Mei")
    mei_b = await b.add_staff("Lin")
    days = around(a.day, 3, 3)
    await warm(a, days)
    assert (await b.client.post("/v1/public/bookings", json=b.booking_body(at(b.day, 10), mei_b), headers=b.pk)).status_code == 201
    assert set((await statuses(a, days)).values()) == {"HIT"}


async def test_a_reservation_whose_period_changes_clears_where_it_was_and_where_it_is(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).json()["id"]
    far = s.day + 10 * DAY
    days = sorted({*around(s.day, 6, 6), *around(far, 6, 6)})
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:  # (a truncated or stretched reservation changes through the ORM, with its old value in the history)
        alloc = await db.scalar(select(BookingAllocation).where(BookingAllocation.booking_id == uuid.UUID(bid)))
        assert alloc is not None
        alloc.period = Range(at(far, 10), at(far, 11), bounds="[)")
    got = await statuses(s, days)
    assert got[s.day] == "MISS" and got[far] == "MISS"
    assert got[s.day + 4 * DAY] == "HIT" and got[s.day - 6 * DAY] == "HIT" and got[far + 6 * DAY] == "HIT"
    await assert_all_true(s, days, monkeypatch)


# --------------------------------------------------------------------- the tokens themselves
async def test_each_date_has_its_own_short_lived_token(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    await s.add_staff("Mei")
    await avail(s)
    keys = await cache.keys(f"avail:gen:{s.tenant_id}*")
    assert f"avail:gen:{s.tenant_id}" in keys and f"avail:gen:{s.tenant_id}:d:{s.day.isoformat()}" in keys
    date_key = f"avail:gen:{s.tenant_id}:d:{s.day.isoformat()}"
    assert 0 < await cache.ttl(date_key) <= availability_cache.DATE_TOKEN_TTL_SECONDS and await cache.ttl(f"avail:gen:{s.tenant_id}") == -1


async def test_a_lost_date_token_never_revives_old_answers(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    await s.add_staff("Mei")
    await avail(s)
    assert (await avail(s)).headers["X-Cache"] == "HIT"
    date_key = f"avail:gen:{s.tenant_id}:d:{s.day.isoformat()}"
    old = await cache.get(date_key)
    await cache.delete(date_key)  # evicted, or expired
    assert (await avail(s)).headers["X-Cache"] == "MISS"
    assert await cache.get(date_key) not in (None, old)  # replaced by a fresh random token, not by anything it was before
    await cache.delete(f"avail:gen:{s.tenant_id}")  # and the same for the tenant's own
    assert (await avail(s)).headers["X-Cache"] == "MISS"


async def test_the_month_view_after_a_booking_recomputes_only_the_dates_around_it(client: httpx.AsyncClient, cache: Any, counting: Any) -> None:
    from tests.test_availability_month import _Counting, month, month_of

    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    m = month_of(s.day)
    assert (await month(s, m)).headers["X-Cache"] == "MISS"
    assert (await month(s, m)).headers["X-Cache"] == "HIT"
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).status_code == 201
    _Counting.calls.clear()
    after = await month(s, m)
    computed = set(_Counting.calls)
    assert after.headers["X-Cache"] == "MISS" and s.day in computed
    assert computed <= set(around(s.day, 4, 3)), computed  # the rest of the month came straight from the cache
    counts = {d["date"]: d["slot_count"] for d in after.json()["days"]}
    assert counts[s.day.isoformat()] < max(counts.values())  # and the booked day lost a slot


# --------------------------------------------------------------------- the margins really are enough
async def tz_spa(client: httpx.AsyncClient, tz: str, *, overnight: bool) -> tuple[Seed, uuid.UUID, dt.date]:
    """A spa in another time zone: ordinary daytime shifts, or 22:00-06:00 night shifts."""
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Spa {uuid.uuid4().hex[:6]}", "business_type": "spa"})
    assert r.status_code == 201, r.text
    s = Seed(client, r.json())
    await set_zone(s.tenant_id, tz)  # (sign-up only offers a country its own zones; the engine and the cache work in any)
    s.admin = await s.token("owner")
    policy = {"name": "D", "default_status": "confirmed", "min_notice_booking": 60, "min_notice_cancel": 3600, "booking_window": 86400 * 60, "pending_timeout": 1800, "make_default": True}
    assert (await client.post("/v1/admin/policies", headers=s.admin, json=policy)).status_code == 201
    s.service_id = uuid.UUID((await client.post("/v1/admin/services", headers=s.admin, json={"name": "Massage", "duration_minutes": 60})).json()["id"])
    staff = await s.add_staff("Mei", shifts=False)
    start, end = ("22:00:00", "06:00:00") if overnight else ("09:00:00", "18:00:00")
    body = [{"weekday": d, "start_time": start, "end_time": end} for d in range(7)]
    assert (await client.put(f"/v1/admin/resources/{staff}/shifts", json=body, headers=s.admin)).status_code == 200
    day = (dt.datetime.now(ZoneInfo(tz)) + 4 * DAY).date()
    return s, staff, day


async def local(day: dt.date, hh: int, tz: str) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh), tzinfo=ZoneInfo(tz))


async def test_a_night_booking_in_a_western_zone_clears_the_business_day_before(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """01:00 New York time is 05:00 UTC *the same* UTC date, yet the booking belongs to the business day that began the evening before."""
    tz = "America/New_York"
    s, staff, day = await tz_spa(client, tz, overnight=True)
    days = around(day, 6, 6)
    await warm(s, days)
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(await local(day, 1, tz), staff), headers=s.pk)
    assert r.status_code == 201, r.text
    await assert_all_true(s, days, monkeypatch)
    previous = await avail(s, day=day - DAY)
    assert "01:00" not in [dt.datetime.fromisoformat(x["start_at"]).astimezone(ZoneInfo(tz)).strftime("%H:%M") for x in previous.json()["slots"]]


async def test_a_morning_booking_in_a_far_eastern_zone_clears_its_own_date(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """09:00 in Kiritimati (UTC+14) is 19:00 UTC on the *previous* UTC date, but the answer to clear is the local date's."""
    tz = "Pacific/Kiritimati"
    s, staff, day = await tz_spa(client, tz, overnight=False)
    days = around(day, 6, 6)
    await warm(s, days)
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(await local(day, 9, tz), staff), headers=s.pk)
    assert r.status_code == 201, r.text
    await assert_all_true(s, days, monkeypatch)
    slots = (await avail(s, day=day)).json()["slots"]
    assert "09:00" not in [dt.datetime.fromisoformat(x["start_at"]).astimezone(ZoneInfo(tz)).strftime("%H:%M") for x in slots]


async def test_a_late_evening_booking_in_the_far_west_clears_the_next_local_date(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    tz = "Pacific/Pago_Pago"  # UTC-11: 17:00 local is 04:00 UTC the next day
    s, staff, day = await tz_spa(client, tz, overnight=False)
    days = around(day, 6, 6)
    await warm(s, days)
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(await local(day, 17, tz), staff), headers=s.pk)
    assert r.status_code == 201, r.text
    await assert_all_true(s, days, monkeypatch)


# --------------------------------------------------------------------- every vertical
async def test_a_restaurant_booking_leaves_no_stale_date(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rest = await make_restaurant(client)
    days = around(rest.day, 5, 5)
    await warm(rest, days, party_size=2)
    r = await rbook(rest, at(rest.day, 19), 2)
    assert r.status_code == 201, r.text
    got = await statuses(rest, days, party_size=2)
    assert got[rest.day] == "MISS" and got[rest.day + 5 * DAY] == "HIT"
    await assert_all_true(rest, days, monkeypatch, party_size=2)


async def test_a_stacked_workshop_job_leaves_no_stale_date(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    g = await make_garage(client, mechanics=("Ali",), bays=("Bay 1",))
    ids = [g.svc["oil"]]
    days = around(g.day, 5, 5)
    await warm(g, days, services=ids)
    r = await gbook(g, at(g.day, 9), ["brake", "oil"])
    assert r.status_code == 201, r.text
    await assert_all_true(g, days, monkeypatch, services=ids)
    cancel = await g.client.patch(f"/v1/admin/bookings/{r.json()['id']}/status", json={"status": "cancelled"}, headers=g.admin)
    assert cancel.status_code == 200
    await assert_all_true(g, days, monkeypatch, services=ids)


async def test_an_overnight_restaurant_booking_leaves_no_stale_date(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rest = await make_restaurant(client)
    hours = [{"weekday": d, "open_time": "18:00:00", "close_time": "02:00:00"} for d in range(7)]
    assert (await rest.client.put("/v1/admin/operating-hours", json=hours, headers=rest.admin)).status_code == 200
    days = around(rest.day, 5, 5)
    await warm(rest, days, party_size=2)
    r = await rbook(rest, at(rest.day, 23, 30), 2)
    assert r.status_code == 201, r.text
    await assert_all_true(rest, days, monkeypatch, party_size=2)
    r = await rbook(rest, at(rest.day + DAY, 0, 30), 2)  # after midnight: the business day that began the evening before
    assert r.status_code == 201, r.text
    await assert_all_true(rest, days, monkeypatch, party_size=2)
