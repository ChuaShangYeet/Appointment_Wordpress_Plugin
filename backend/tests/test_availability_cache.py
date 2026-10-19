"""Redis availability cache (blueprint §13.1): hits, invalidation on every kind of write, and failing safe."""
from __future__ import annotations

import datetime as dt
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import redis.exceptions
from sqlalchemy import event

from app.services.availability_view import still_bookable as _still_bookable
from app.core import availability_cache
from app.core import redis as redis_module
from app.core.config import get_settings
from app.core.database import get_engine
from tests.conftest import Seed, at, make_seed
from tests.test_automotive import gbook, make_garage
from tests.test_field_service import KL, make_practice
from tests.test_restaurant import add_table, make_restaurant, put_slots
from tests.test_restaurant import book as rbook
from tests.test_salon import make_salon, sbook, status, upsell

TTL = 30


@pytest.fixture
async def cache(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """An in-memory Redis with the availability cache switched on."""
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis", fake)
    monkeypatch.setattr(get_settings(), "availability_cache_ttl_seconds", TTL)
    availability_cache.reset_breaker()
    yield fake
    availability_cache.reset_breaker()


async def avail(s: Seed, *, services: list[uuid.UUID] | None = None, day: dt.date | None = None, **params: Any) -> httpx.Response:
    ids = [s.service_id] if services is None and hasattr(s, "service_id") else services or []
    q: dict[str, Any] = {"date": (day or s.day).isoformat(), "service_ids": [str(i) for i in ids], **params}
    r = await s.client.get("/v1/public/availability", params=q, headers=s.pk)
    assert r.status_code == 200, r.text
    return r


async def truth(s: Seed, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> dict[str, Any]:
    """What availability really is right now: the same request with the cache off."""
    with monkeypatch.context() as m:
        m.setattr(get_settings(), "availability_cache_ttl_seconds", 0)
        r = await avail(s, **kw)
    assert r.headers["X-Cache"] == "BYPASS"
    return r.json()  # type: ignore[no-any-return]


async def assert_invalidates(s: Seed, monkeypatch: pytest.MonkeyPatch, write: Any, *, changes: bool = True, **kw: Any) -> None:
    """Warm the cache, make a committed write, and require the very next answer to be recomputed and correct."""
    before = await avail(s, **kw)  # (a miss, or a hit if an earlier check left the answer cached)
    assert (await avail(s, **kw)).headers["X-Cache"] == "HIT"
    await write()
    after = await avail(s, **kw)
    assert after.headers["X-Cache"] == "MISS", "a stale answer was served after a committed write"
    assert after.json() == await truth(s, monkeypatch, **kw)
    if changes:
        assert after.json() != before.json(), "the write should have changed availability; the check would be vacuous"
    assert (await avail(s, **kw)).headers["X-Cache"] == "HIT"  # and the fresh answer is cached again


def hhmm(r: httpx.Response) -> list[str]:
    from tests.conftest import TZ

    return [dt.datetime.fromisoformat(x["start_at"]).astimezone(TZ).strftime("%H:%M") for x in r.json()["slots"]]


# --------------------------------------------------------------------- pure
def _payload(slots: list[tuple[dt.datetime, dt.datetime]], notice: int = 3600, window: int = 30 * 86400) -> dict[str, Any]:
    return {"min_notice_s": notice, "window_s": window, "response": {
        "date": "2030-01-07", "timezone": "Asia/Kuala_Lumpur",
        "slots": [{"start_at": a.isoformat(), "end_at": b.isoformat(), "resource_ids": []} for a, b in slots]}}


def test_cached_slots_that_the_clock_has_made_unbookable_are_dropped() -> None:
    now = dt.datetime(2030, 1, 7, 8, tzinfo=dt.UTC)
    hour = dt.timedelta(hours=1)
    slots = [(now + d, now + d + hour) for d in (dt.timedelta(minutes=30), dt.timedelta(minutes=60), dt.timedelta(days=29), dt.timedelta(days=31))]
    kept = _still_bookable(_payload(slots), now)
    assert kept is not None
    # 30 minutes ahead is inside the 60-minute notice; exactly 60 is still fine; 31 days is beyond the 30-day window
    assert [s.start_at for s in kept.slots] == [slots[1][0], slots[2][0]]


def test_an_unreadable_cache_entry_is_ignored_not_trusted() -> None:
    now = dt.datetime(2030, 1, 7, 8, tzinfo=dt.UTC)
    assert _still_bookable({"response": {"nope": 1}, "min_notice_s": 1, "window_s": 1}, now) is None
    assert _still_bookable({}, now) is None
    assert _still_bookable({"response": _payload([])["response"], "min_notice_s": "x", "window_s": 1}, now) is None


# --------------------------------------------------------------------- hits and keys
async def test_the_second_identical_request_is_served_from_the_cache(seeded: Seed, cache: Any) -> None:
    await seeded.add_staff("Mei")
    first, second = await avail(seeded), await avail(seeded)
    assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("MISS", "HIT")
    assert first.json() == second.json() and first.json()["slots"]
    key = next(k for k in await cache.keys("avail:*") if not k.startswith("avail:gen:"))
    assert 0 < await cache.ttl(key) <= TTL


async def test_different_requests_and_different_tenants_never_share_an_answer(client: httpx.AsyncClient, cache: Any) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await a.add_staff("Mei")  # tenant b has nobody, so an answer leaking across tenants would show up
    assert (await avail(a)).headers["X-Cache"] == "MISS"
    other_day = await avail(a, day=a.day + dt.timedelta(days=1))
    assert other_day.headers["X-Cache"] == "MISS"  # another date is another question
    theirs = await avail(b)
    assert theirs.headers["X-Cache"] == "MISS" and theirs.json()["slots"] == []
    assert (await avail(a)).json()["slots"] and (await avail(b)).json()["slots"] == []


async def test_requests_with_coordinates_are_never_cached_or_keyed(client: httpx.AsyncClient, cache: Any) -> None:
    p = await make_practice(client)
    r = await avail(p, services=[p.svc["office"]], latitude=KL[0], longitude=KL[1])
    assert r.headers["X-Cache"] == "BYPASS"
    assert [k for k in await cache.keys("avail:*") if not k.startswith("avail:gen:")] == []
    assert (await avail(p, services=[p.svc["office"]])).headers["X-Cache"] == "MISS"  # the same question without a pin is cacheable


async def test_the_cache_is_off_when_the_ttl_is_zero(seeded: Seed, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "availability_cache_ttl_seconds", 0)
    await seeded.add_staff("Mei")
    assert (await avail(seeded)).headers["X-Cache"] == "BYPASS" and (await avail(seeded)).headers["X-Cache"] == "BYPASS"
    assert await cache.keys("avail:*") == []


async def test_errors_and_validation_failures_are_not_cached(client: httpx.AsyncClient, cache: Any) -> None:
    rest = await make_restaurant(client)
    bad = await rest.client.get("/v1/public/availability", params={"date": rest.day.isoformat()}, headers=rest.pk)  # no party_size
    assert bad.status_code == 422
    assert [k for k in await cache.keys("avail:*") if not k.startswith("avail:gen:")] == []


async def test_a_corrupt_entry_is_recomputed(seeded: Seed, cache: Any) -> None:
    await seeded.add_staff("Mei")
    await avail(seeded)
    key = next(k for k in await cache.keys("avail:*") if not k.startswith("avail:gen:"))
    await cache.set(key, "{not json", ex=TTL)
    r = await avail(seeded)
    assert r.headers["X-Cache"] == "MISS" and r.json()["slots"]


# --------------------------------------------------------------------- invalidation: every kind of write
async def test_a_new_booking_invalidates(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")

    async def book() -> None:
        r = await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)
        assert r.status_code == 201, r.text

    await assert_invalidates(s, monkeypatch, book)
    assert "10:00" not in hhmm(await avail(s))


async def test_cancelling_releases_the_slot_through_a_bulk_update(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).json()["id"]

    async def cancel() -> None:
        assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=s.admin)).status_code == 200

    await assert_invalidates(s, monkeypatch, cancel)
    assert "10:00" in hhmm(await avail(s))


async def test_a_lost_race_that_rolls_back_invalidates_nothing(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    body = s.booking_body(at(s.day, 10), mei)
    assert (await s.client.post("/v1/public/bookings", json=body, headers=s.pk)).status_code == 201
    assert (await avail(s)).headers["X-Cache"] == "MISS" and (await avail(s)).headers["X-Cache"] == "HIT"
    refused = await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)
    assert refused.status_code == 409  # nothing was committed...
    assert (await avail(s)).headers["X-Cache"] == "HIT"  # ...so nothing was invalidated


async def test_shift_hours_and_days_off_invalidate(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")

    async def shorter_shift() -> None:
        body = [{"weekday": d, "start_time": "09:00:00", "end_time": "12:00:00"} for d in range(7)]
        assert (await s.client.put(f"/v1/admin/resources/{mei}/shifts", json=body, headers=s.admin)).status_code == 200

    async def closing_early() -> None:
        body = [{"weekday": d, "open_time": "09:00:00", "close_time": "10:30:00"} for d in range(7)]
        assert (await s.client.put("/v1/admin/operating-hours", json=body, headers=s.admin)).status_code == 200

    async def holiday() -> None:
        body = {"name": "Off", "start_date": s.day.isoformat(), "end_date": s.day.isoformat()}
        assert (await s.client.post("/v1/admin/days-off", json=body, headers=s.admin)).status_code == 201

    await assert_invalidates(s, monkeypatch, shorter_shift)  # a bulk delete plus inserts
    await assert_invalidates(s, monkeypatch, closing_early)  # another bulk delete
    await assert_invalidates(s, monkeypatch, holiday)
    assert (await avail(s)).json()["slots"] == []

    async def hours_removed() -> None:  # nothing but a bulk DELETE reaches the database
        assert (await s.client.put("/v1/admin/operating-hours", json=[], headers=s.admin)).status_code == 200

    other_day = s.day + dt.timedelta(days=1)
    await avail(s, day=other_day)  # a date the holiday does not cover, still limited by the 09:00-10:30 hours
    before = hhmm(await avail(s, day=other_day))
    await hours_removed()
    after = await avail(s, day=other_day)
    assert after.headers["X-Cache"] == "MISS" and len(hhmm(after)) > len(before)


async def test_service_and_staff_changes_invalidate(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    svc = f"/v1/admin/services/{s.service_id}"

    async def longer() -> None:
        assert (await s.client.patch(svc, json={"duration_minutes": 180}, headers=s.admin)).status_code == 200

    async def only_someone_else() -> None:
        lin = await s.add_staff("Lin", shifts=False)  # no shifts: nobody qualified is ever free
        assert (await s.client.patch(svc, json={"resource_ids": [str(lin)]}, headers=s.admin)).status_code == 200

    async def a_new_colleague() -> None:
        await s.add_staff("Bala")
        assert (await s.client.patch(svc, json={"resource_ids": []}, headers=s.admin)).status_code == 200

    await assert_invalidates(s, monkeypatch, longer)
    await assert_invalidates(s, monkeypatch, only_someone_else)
    await assert_invalidates(s, monkeypatch, a_new_colleague)
    assert mei is not None


async def test_policy_and_tenant_level_changes_invalidate(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    await s.add_staff("Mei")

    async def stricter_notice() -> None:
        body = {"name": "Strict", "default_status": "confirmed", "min_notice_booking": 86400 * 3, "min_notice_cancel": 86400,
                "booking_window": 60 * 86400, "pending_timeout": 1800, "make_default": True}
        assert (await s.client.post("/v1/admin/policies", json=body, headers=s.admin)).status_code == 201  # inserts a policy, updates the tenant

    await assert_invalidates(s, monkeypatch, stricter_notice, changes=False)  # (the 3-day notice may or may not empty a +3-day date)

    async def moved_business() -> None:  # a bulk UPDATE of the tenants table
        r = await s.client.put("/v1/admin/business-location", json={"latitude": 3.1, "longitude": 101.6, "service_radius_km": 5}, headers=s.admin)
        assert r.status_code == 200

    await assert_invalidates(s, monkeypatch, moved_business, changes=False)


async def test_restaurant_settings_and_tables_invalidate(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    rest = await make_restaurant(client)
    kw = {"party_size": 2}

    async def longer_seatings() -> None:
        await put_slots(rest, slot_duration_minutes=180)

    async def one_table_open() -> None:
        wd = rest.day.isoweekday() % 7
        r = await rest.client.put("/v1/admin/table-open-rules", json=[{"weekday": wd, "table_type": "normal", "open_tables": 0}], headers=rest.admin)
        assert r.status_code == 200

    async def new_table() -> None:
        await add_table(rest, "big", 8, {})

    async def reservation() -> None:
        assert (await rbook(rest, at(rest.day, 19), 2)).status_code == 201

    await assert_invalidates(rest, monkeypatch, longer_seatings, **kw)
    await assert_invalidates(rest, monkeypatch, one_table_open, **kw)
    assert (await avail(rest, **kw)).json()["slots"] == []

    async def rules_removed() -> None:  # nothing but a bulk DELETE reaches the database
        assert (await rest.client.put("/v1/admin/table-open-rules", json=[], headers=rest.admin)).status_code == 200

    await assert_invalidates(rest, monkeypatch, rules_removed, **kw)
    await assert_invalidates(rest, monkeypatch, new_table, changes=False, party_size=7)  # a party of 7 now has a table
    await assert_invalidates(rest, monkeypatch, reservation, changes=False, **kw)


async def test_a_workshops_parts_notice_follows_stock_changes(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    g = await make_garage(client)
    pad = (await g.client.post("/v1/admin/products", json={"name": "Brake pad", "stock_qty": 4, "price": "50.00", "stock_status": "in_stock"}, headers=g.admin)).json()["id"]
    assert (await g.client.put(f"/v1/admin/services/{g.svc['brake']}/parts", json={"product_ids": [pad]}, headers=g.admin)).status_code == 200
    kw = {"services": [g.svc["brake"]]}
    assert (await avail(g, **kw)).json()["lead_time_notice"] == []

    async def out_of_stock() -> None:
        r = await g.client.put(f"/v1/admin/products/{pad}", json={"name": "Brake pad", "stock_qty": 0, "price": "50.00", "stock_status": "available_on_order", "estimated_lead_time_days": 5}, headers=g.admin)
        assert r.status_code == 200, r.text

    await assert_invalidates(g, monkeypatch, out_of_stock, changes=True, **kw)
    assert (await avail(g, **kw)).json()["lead_time_notice"][0]["name"] == "Brake pad"


async def test_an_upsell_extending_an_appointment_invalidates(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_salon(client, stylists=("Mei",))
    bid = (await sbook(s, at(s.day, 10), ["cut"], stylist="Mei")).json()["id"]
    assert (await status(s, bid, "in-progress")).status_code == 200
    kw = {"services": [s.svc["cut"]]}

    async def add_gel() -> None:  # extends the allocation with an ORM update inside a savepoint
        assert (await upsell(s, bid, ["treat"])).status_code == 200

    await assert_invalidates(s, monkeypatch, add_gel, changes=True, **kw)


async def test_a_workshop_booking_invalidates_every_resource_it_touches(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    g = await make_garage(client, mechanics=("Ali",), bays=("Bay 1",))
    kw = {"services": [g.svc["oil"]]}

    async def book() -> None:
        assert (await gbook(g, at(g.day, 10), ["oil"])).status_code == 201

    await assert_invalidates(g, monkeypatch, book, **kw)


# --------------------------------------------------------------------- correctness under races
async def test_an_answer_computed_before_a_write_is_never_served_after_it(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader computes, then a booking commits, then the reader stores. The stored answer is stale on
    arrival; it must sit under the *old* token so the next request cannot see it."""
    from app.api.v1 import public as public_module

    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    real = public_module.get_allocator

    class WriteAfterComputing:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def find_slots(self, *a: Any, **k: Any) -> Any:
            slots = await self.inner.find_slots(*a, **k)  # computed against the state before the booking...
            r = await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)  # ...then one commits
            assert r.status_code == 201, r.text
            return slots

    with monkeypatch.context() as m:
        m.setattr(public_module, "get_allocator", lambda bt: WriteAfterComputing(real(bt)))
        stale = await avail(s)
    assert "10:00" in hhmm(stale)  # this caller got a pre-booking answer, which is fine for a request already in flight
    after = await avail(s)
    assert after.headers["X-Cache"] == "MISS" and "10:00" not in hhmm(after)


async def test_the_cache_is_only_invalidated_once_the_write_is_visible_to_readers(
    client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reader that arrives the instant the token changes must already be able to see the committed booking;
    if the token changed before the commit it would recompute from old data and cache that under the new token."""
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    real = availability_cache.invalidate
    readers: list[httpx.Response] = []

    async def invalidate_then_read(tenant_id: uuid.UUID, dates: Any = None) -> None:
        await real(tenant_id, dates)
        readers.append(await avail(s))  # a concurrent reader, right after the token was replaced

    with monkeypatch.context() as m:  # scoped: undo() would also remove the fixture's Redis and TTL patches
        m.setattr(availability_cache, "invalidate", invalidate_then_read)
        assert (await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)).status_code == 201
    assert readers and "10:00" not in hhmm(readers[0])
    after = await avail(s)
    assert after.headers["X-Cache"] == "HIT" and "10:00" not in hhmm(after)  # what that reader cached is right


# --------------------------------------------------------------------- Redis failing
class _Broken:
    """A Redis that has gone away."""

    def __init__(self) -> None:
        self.calls = 0

    def pipeline(self, *a: Any, **k: Any) -> Any:
        broken = self

        class Pipe:  # (a real pipeline queues commands and only talks to Redis in ``execute``)
            def __getattr__(self, name: str) -> Any:
                return lambda *a, **k: self

            async def execute(self) -> Any:
                broken.calls += 1
                raise redis.exceptions.ConnectionError("Redis is down")

        return Pipe()

    def __getattr__(self, name: str) -> Any:
        async def fail(*a: Any, **k: Any) -> Any:
            self.calls += 1
            raise redis.exceptions.ConnectionError("Redis is down")

        return fail


async def test_a_dead_redis_never_breaks_availability_or_booking_and_is_not_retried_on_every_request(
    client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    broken = _Broken()
    monkeypatch.setattr(redis_module, "_redis", broken)
    first = await avail(s)
    assert first.headers["X-Cache"] == "BYPASS" and first.json()["slots"]
    calls = broken.calls
    assert calls >= 1
    await avail(s)
    assert broken.calls == calls  # the breaker is open: no further attempts (and no 0.25 s waits) while it is
    booked = await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)
    assert booked.status_code == 201  # a write with Redis down still succeeds
    assert "10:00" not in hhmm(await avail(s))


async def test_entries_stored_before_an_outage_are_not_trusted_when_redis_comes_back(client: httpx.AsyncClient, cache: Any) -> None:
    """Writes during an outage cannot invalidate, so after any failure the cache stays off for at least the TTL."""
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    assert (await avail(s)).headers["X-Cache"] == "MISS" and (await avail(s)).headers["X-Cache"] == "HIT"
    real = redis_module._redis
    redis_module._redis = _Broken()  # type: ignore[assignment]
    try:
        booked = await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei), headers=s.pk)
        assert booked.status_code == 201  # the write commits; its invalidation could not reach Redis
    finally:
        redis_module._redis = real
    healthy_again = await avail(s)  # Redis is back and still holds the pre-booking entry
    assert healthy_again.headers["X-Cache"] == "BYPASS" and "10:00" not in hhmm(healthy_again)


# --------------------------------------------------------------------- the invalidation list is complete
IGNORED_READS = {"api_keys", "sites"}  # read to authenticate the request, not inputs to availability


async def _tables_read(s: Seed, **kw: Any) -> set[str]:
    """Every table the SQL of one uncached availability request reads."""
    seen: list[str] = []
    engine = get_engine().sync_engine

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with pytest.MonkeyPatch.context() as m:
            m.setattr(get_settings(), "availability_cache_ttl_seconds", 0)
            await avail(s, **kw)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return {t for sql in seen for t in re.findall(r"\b(?:FROM|JOIN)\s+([a-z_]+)", sql, re.I)} - IGNORED_READS


@pytest.mark.parametrize("vertical", ["spa", "restaurant", "automotive", "professional", "salon"])
async def test_every_table_an_availability_request_reads_is_watched_for_changes(client: httpx.AsyncClient, vertical: str) -> None:
    """If a future change makes availability depend on a new table, this fails until that table is added to
    ``TRACKED_TABLES`` - otherwise writes to it would silently leave stale answers in Redis."""
    if vertical == "spa":
        s = await make_seed(client)
        await s.add_staff("Mei")
        kw: dict[str, Any] = {}
    elif vertical == "restaurant":
        s, kw = await make_restaurant(client), {"party_size": 2}
    elif vertical == "automotive":
        s = await make_garage(client)
        kw = {"services": [s.svc["oil"], s.svc["brake"]]}  # type: ignore[attr-defined]
    elif vertical == "professional":
        s = await make_practice(client)
        kw = {"services": [s.svc["office"]]}  # type: ignore[attr-defined]
    else:
        s = await make_salon(client)
        kw = {"services": [s.svc["cut"], s.svc["gel"]]}  # type: ignore[attr-defined]
    read = await _tables_read(s, **kw)
    assert {"resources"} <= read or vertical == "restaurant", read  # the probe really saw the availability queries
    missing = read - availability_cache.TRACKED_TABLES
    assert not missing, f"availability reads {sorted(missing)} but writes to them would not invalidate the cache"
