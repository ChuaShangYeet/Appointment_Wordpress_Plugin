"""Month view of availability: which dates have room, consistent with the day view and sharing its cache."""
from __future__ import annotations

import calendar
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.api.v1 import public as public_module
from app.core import availability_cache
from app.core import redis as redis_module
from app.core.config import get_settings
from app.services.availability_view import nothing_bookable
from tests.conftest import TZ, Seed, at, make_seed
from tests.test_automotive import make_garage
from tests.test_field_service import KL, make_practice
from tests.test_restaurant import make_restaurant
from tests.test_restaurant import book as rbook


def month_of(day: dt.date) -> str:
    return day.strftime("%Y-%m")


async def month(s: Seed, month_: str, *, services: list[uuid.UUID] | None = None, **params: Any) -> httpx.Response:
    ids = [s.service_id] if services is None and hasattr(s, "service_id") else services or []
    q: dict[str, Any] = {"month": month_, "service_ids": [str(i) for i in ids], **params}
    return await s.client.get("/v1/public/availability/month", params=q, headers=s.pk)


async def day(s: Seed, d: dt.date, *, services: list[uuid.UUID] | None = None, **params: Any) -> dict[str, Any]:
    ids = [s.service_id] if services is None and hasattr(s, "service_id") else services or []
    q: dict[str, Any] = {"date": d.isoformat(), "service_ids": [str(i) for i in ids], **params}
    r = await s.client.get("/v1/public/availability", params=q, headers=s.pk)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


class _Counting:
    """Wraps an allocator and counts how many dates it was really asked to compute."""

    calls: list[dt.date] = []

    def __init__(self, inner: Any) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    async def find_slots(self, db: Any, req: Any) -> Any:
        _Counting.calls.append(req.day)
        return await self.inner.find_slots(db, req)


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> type[_Counting]:
    real = public_module.get_allocator
    _Counting.calls = []
    monkeypatch.setattr(public_module, "get_allocator", lambda bt: _Counting(real(bt)))
    return _Counting


@pytest.fixture
async def cache(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis", fake)
    monkeypatch.setattr(get_settings(), "availability_cache_ttl_seconds", 30)
    availability_cache.reset_breaker()
    yield fake
    availability_cache.reset_breaker()


# --------------------------------------------------------------------- pure
def test_a_day_is_skippable_only_when_no_slot_on_its_business_day_could_be_bookable() -> None:
    """A business day can run past midnight (overnight hours), so it is taken to last up to 24 hours beyond the date."""
    policy = type("P", (), {"min_notice_booking": dt.timedelta(hours=2), "booking_window": dt.timedelta(days=10)})()
    now = dt.datetime(2030, 1, 10, 12, 0, tzinfo=TZ)
    d = dt.date
    assert nothing_bookable(d(2030, 1, 8), TZ, policy, now)  # two days ago: even its after-midnight hours are over
    assert nothing_bookable(d(2030, 1, 9), TZ, policy, now) is False  # yesterday's night may still run to the small hours of today
    assert nothing_bookable(d(2030, 1, 10), TZ, policy, now) is False  # today
    assert nothing_bookable(d(2030, 1, 20), TZ, policy, now) is False  # the last day that starts inside the window
    assert nothing_bookable(d(2030, 1, 21), TZ, policy, now)  # starts after the window closes
    late = dt.datetime(2030, 1, 11, 22, 30, tzinfo=TZ)  # 2 hours' notice from 22:30 reaches 00:30 on the 12th
    assert nothing_bookable(d(2030, 1, 10), TZ, policy, late)  # the 10th's hours cannot run beyond the end of the 11th
    assert nothing_bookable(d(2030, 1, 11), TZ, policy, late) is False  # but the 11th's can run into the small hours of the 12th


# --------------------------------------------------------------------- shape and agreement with the day view
async def test_every_date_of_the_month_agrees_with_the_day_view(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert r.status_code == 201
    m = await month(seeded, month_of(seeded.day))
    assert m.status_code == 200 and m.headers["X-Cache"] == "BYPASS"  # the suite runs with the cache off
    body = m.json()
    y, mo = seeded.day.year, seeded.day.month
    assert [d["date"] for d in body["days"]] == [dt.date(y, mo, n).isoformat() for n in range(1, calendar.monthrange(y, mo)[1] + 1)]
    assert body["month"] == month_of(seeded.day) and body["timezone"] == "Asia/Kuala_Lumpur"
    for entry in body["days"]:
        slots = (await day(seeded, dt.date.fromisoformat(entry["date"])))["slots"]
        assert entry["slot_count"] == len(slots) and entry["available"] == bool(slots), entry
    booked = next(e for e in body["days"] if e["date"] == seeded.day.isoformat())
    free = next(e for e in body["days"] if e["date"] == (seeded.day + dt.timedelta(days=1)).isoformat())
    assert booked["available"] and 0 < booked["slot_count"] < free["slot_count"]  # Mei's 10:00 hour is gone from that day


async def test_closed_days_holidays_and_full_days_are_unavailable(client: httpx.AsyncClient) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    target = s.day
    hours = [{"weekday": w, "open_time": "09:00:00", "close_time": "18:00:00"} for w in range(7) if w != target.isoweekday() % 7]
    assert (await s.client.put("/v1/admin/operating-hours", json=hours, headers=s.admin)).status_code == 200  # closed that weekday
    holiday = target + dt.timedelta(days=1)
    assert (await s.client.post("/v1/admin/days-off", json={"name": "Off", "start_date": holiday.isoformat(), "end_date": holiday.isoformat()}, headers=s.admin)).status_code == 201
    full = target + dt.timedelta(days=2)
    for h in range(9, 18):
        if h != 13:  # the 13:00-14:00 break
            await s.client.post("/v1/public/bookings", json=s.booking_body(at(full, h), mei), headers=s.pk)
    got = {e["date"]: e for e in (await month(s, month_of(target))).json()["days"] if e["date"] in {d.isoformat() for d in (target, holiday, full)}}
    assert got[target.isoformat()] == {"date": target.isoformat(), "available": False, "slot_count": 0}
    if month_of(holiday) == month_of(target):
        assert got[holiday.isoformat()]["available"] is False
    if month_of(full) == month_of(target):
        assert got[full.isoformat()]["available"] is False


async def test_dates_that_cannot_hold_a_bookable_slot_are_not_computed(seeded: Seed, counting: type[_Counting]) -> None:
    await seeded.add_staff("Mei")
    past = seeded.day - dt.timedelta(days=60)
    r = await month(seeded, month_of(past))
    assert r.status_code == 200 and not any(d["available"] for d in r.json()["days"]) and len(r.json()["days"]) >= 28
    assert counting.calls == []  # a month entirely in the past costs nothing
    far = seeded.day + dt.timedelta(days=200)  # the default booking window is 60 days
    assert not any(d["available"] for d in (await month(seeded, month_of(far))).json()["days"])
    assert counting.calls == []
    counting.calls.clear()
    await month(seeded, month_of(seeded.day))
    now = dt.datetime.now(TZ)
    horizon = now.date() + dt.timedelta(days=61)
    # only dates that could matter: today, later days inside the window, and yesterday (a business day can run past midnight, so it
    # counts as bookable until a day later - which is why this test used to pass only when "yesterday" fell in the previous month)
    assert counting.calls and all(now.date() - dt.timedelta(days=1) <= d <= horizon for d in counting.calls)


async def test_leap_year_and_year_end_months_have_the_right_number_of_days(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    assert len((await month(seeded, "2032-02")).json()["days"]) == 29
    assert len((await month(seeded, "2031-02")).json()["days"]) == 28
    dec = (await month(seeded, "2030-12")).json()["days"]
    assert len(dec) == 31 and dec[-1]["date"] == "2030-12-31"


@pytest.mark.parametrize("bad", ["2030-13", "2030-00", "2030-1", "30-01", "abc", "2030-01-15", "1969-12", "2300-01", ""])
async def test_the_month_parameter_is_validated(seeded: Seed, bad: str) -> None:
    assert (await month(seeded, bad)).status_code == 422


async def test_unknown_services_are_refused_like_the_day_view(seeded: Seed) -> None:
    r = await month(seeded, month_of(seeded.day), services=[uuid.uuid4()])
    assert r.status_code == 404
    assert (await seeded.client.get("/v1/public/availability", params={"date": seeded.day.isoformat(), "service_ids": [str(uuid.uuid4())]}, headers=seeded.pk)).status_code == 404


# --------------------------------------------------------------------- other verticals
async def test_a_restaurant_month_needs_a_party_size_and_reflects_reservations(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    assert (await month(rest, month_of(rest.day))).status_code == 422  # like the day view: no party_size
    before = next(d for d in (await month(rest, month_of(rest.day), party_size=2)).json()["days"] if d["date"] == rest.day.isoformat())
    for _ in range(4):  # a2, b2, c4 and d6: every normal table is taken at 19:00
        assert (await rbook(rest, at(rest.day, 19), 2)).status_code == 201
    after = next(d for d in (await month(rest, month_of(rest.day), party_size=2)).json()["days"] if d["date"] == rest.day.isoformat())
    assert after["slot_count"] < before["slot_count"]
    vip = await month(rest, month_of(rest.day), party_size=2, table_type="vip")
    assert vip.status_code == 200 and any(d["available"] for d in vip.json()["days"])


async def test_a_workshop_month_carries_the_parts_notice_even_when_no_day_is_bookable(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    pad = (await g.client.post("/v1/admin/products", json={"name": "Brake pad", "stock_qty": 0, "price": "50.00", "stock_status": "available_on_order", "estimated_lead_time_days": 5}, headers=g.admin)).json()["id"]
    assert (await g.client.put(f"/v1/admin/services/{g.svc['brake']}/parts", json={"product_ids": [pad]}, headers=g.admin)).status_code == 200
    ok = await month(g, month_of(g.day), services=[g.svc["brake"]])
    assert [n["name"] for n in ok.json()["lead_time_notice"]] == ["Brake pad"]
    past = await month(g, month_of(g.day - dt.timedelta(days=90)), services=[g.svc["brake"]])
    assert not any(d["available"] for d in past.json()["days"])
    assert [n["name"] for n in past.json()["lead_time_notice"]] == ["Brake pad"]


async def test_requests_with_a_map_pin_are_never_cached(client: httpx.AsyncClient, cache: Any) -> None:
    p = await make_practice(client)
    r = await month(p, month_of(p.day), services=[p.svc["office"]], latitude=KL[0], longitude=KL[1])
    assert r.status_code == 200 and r.headers["X-Cache"] == "BYPASS" and any(d["available"] for d in r.json()["days"])
    assert [k for k in await cache.keys("avail:*") if not k.startswith("avail:gen:")] == []


# --------------------------------------------------------------------- the cache
async def test_the_month_and_day_views_share_cache_entries(client: httpx.AsyncClient, cache: Any, counting: type[_Counting]) -> None:
    s = await make_seed(client)
    await s.add_staff("Mei")
    m = month_of(s.day)
    first = await month(s, m)
    assert first.headers["X-Cache"] == "MISS" and counting.calls
    warmed = len(counting.calls)
    counting.calls.clear()
    second = await month(s, m)
    assert second.headers["X-Cache"] == "HIT" and counting.calls == [] and second.json() == first.json()
    r = await s.client.get("/v1/public/availability", params={"date": s.day.isoformat(), "service_ids": str(s.service_id)}, headers=s.pk)
    assert r.headers["X-Cache"] == "HIT"  # the month view warmed the day view

    fresh = await make_seed(client)
    await fresh.add_staff("Mei")
    await s.client.get("/v1/public/availability", params={"date": fresh.day.isoformat(), "service_ids": str(fresh.service_id)}, headers=fresh.pk)
    counting.calls.clear()
    await month(fresh, month_of(fresh.day))
    assert len(counting.calls) == warmed - 1  # and the day view warmed the month: one date was already cached


async def test_a_booking_invalidates_the_month_view(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    m = month_of(s.day)
    before = {d["date"]: d["slot_count"] for d in (await month(s, m)).json()["days"]}
    assert (await month(s, m)).headers["X-Cache"] == "HIT"
    assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).status_code == 201
    after = await month(s, m)
    assert after.headers["X-Cache"] == "MISS"  # a write anywhere clears the tenant's answers
    counts = {d["date"]: d["slot_count"] for d in after.json()["days"]}
    assert counts[s.day.isoformat()] < before[s.day.isoformat()]
    assert {k: v for k, v in counts.items() if k != s.day.isoformat()} == {k: v for k, v in before.items() if k != s.day.isoformat()}


async def test_cached_month_answers_are_rechecked_against_the_clock(client: httpx.AsyncClient, cache: Any) -> None:
    """A cached day whose only slots have since slipped inside the minimum notice must read as unavailable."""
    s = await make_seed(client)
    await s.add_staff("Mei")
    m = month_of(s.day)
    await month(s, m)
    keys = [k for k in await cache.keys("avail:*") if not k.startswith("avail:gen:")]
    assert keys
    import json

    for k in keys:  # rewrite every entry so that its notice is far longer than any slot's distance from now
        entry = json.loads(await cache.get(k))
        entry["min_notice_s"] = 10**9
        await cache.set(k, json.dumps(entry), ex=30)
    r = await month(s, m)
    assert r.headers["X-Cache"] == "HIT" and not any(d["available"] for d in r.json()["days"])


async def test_the_last_day_of_the_booking_window_is_judged_in_the_businesss_timezone(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    """With the clock at 02:00 on 1 March (Kuala Lumpur), the 60-day window closes at 02:00 on 30 April local time. A
    00:00-03:00 shift makes 30 April bookable, although that date only *starts* after the window's end when the day is
    (wrongly) taken to begin at midnight UTC, eight hours later."""
    mei = await seeded.add_staff("Mei", shifts=False)
    shift = [{"weekday": w, "start_time": "00:00:00", "end_time": "03:00:00"} for w in range(7)]
    assert (await seeded.client.put(f"/v1/admin/resources/{mei}/shifts", json=shift, headers=seeded.admin)).status_code == 200
    now = dt.datetime(2030, 3, 1, 2, 0, tzinfo=TZ)
    monkeypatch.setattr(public_module, "_utcnow", lambda: now.astimezone(dt.UTC))
    r = await month(seeded, "2030-04")
    days = {d["date"]: d for d in r.json()["days"]}
    assert days["2030-04-30"]["available"] is True and days["2030-04-30"]["slot_count"] > 0
    assert days["2030-04-29"]["available"] is True
    assert (await month(seeded, "2030-05")).json()["days"][0]["available"] is False  # 1 May starts after the window closed
    assert (await month(seeded, "2030-03")).json()["days"][0]["available"] is False  # 1 March 00:00-01:00 has already passed
