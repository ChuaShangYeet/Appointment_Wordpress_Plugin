"""The periodic sweep: pending holds expire without traffic, and review flags catch bookings that raced a settings change."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from app import worker
from app.core import availability_cache
from app.core import redis as redis_module
from app.core.config import get_settings
from app.core.database import tenant_session
from app.models import Booking
from app.services import booking as booking_module
from app.services import flagging
from app.services import sweep as sweep_module
from app.services.sweep import SWEEP_LOCK_KEY, SweepReport, TenantSweep, sweep_all
from tests.conftest import Seed, allocations, at, make_seed
from tests.test_automotive import gbook, make_garage
from tests.test_flagging import HEADER, book, flags, get, put_hours


def soon(minutes: int = 5) -> dt.datetime:
    """A clock reading past the sweep's grace period, so bookings made a moment ago count as old enough."""
    return dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes)


async def sweep(s: Seed, **kw: Any) -> SweepReport:
    report = await sweep_all(tenant_ids=[s.tenant_id], **kw)
    assert report.ran and not report.errors, report
    return report


async def checked_at(s: Seed, bid: str) -> dt.datetime | None:
    async with tenant_session(s.tenant_id) as db:
        return await db.scalar(select(Booking.flags_checked_at).where(Booking.id == uuid.UUID(bid)))


@pytest.fixture
async def cache(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis", fake)
    monkeypatch.setattr(get_settings(), "availability_cache_ttl_seconds", 30)
    availability_cache.reset_breaker()
    yield fake
    availability_cache.reset_breaker()


async def avail(s: Seed) -> httpx.Response:
    r = await s.client.get("/v1/public/availability", params={"date": s.day.isoformat(), "service_ids": str(s.service_id)}, headers=s.pk)
    assert r.status_code == 200, r.text
    return r


def starts(r: httpx.Response) -> list[str]:
    from tests.conftest import TZ

    return [dt.datetime.fromisoformat(x["start_at"]).astimezone(TZ).strftime("%H:%M") for x in r.json()["slots"]]


# --------------------------------------------------------------------- expiring pending holds
async def test_an_unanswered_pending_booking_is_rejected_and_its_slot_freed_without_any_traffic(client: httpx.AsyncClient) -> None:
    s = await make_seed(client, default_status="pending")
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, at(s.day, 10))
    async with tenant_session(s.tenant_id) as db:  # the hold's time runs out
        await db.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute'"))
    assert (await get(s, bid))["status"] == "pending"  # nothing has touched it yet
    report = await sweep(s)
    assert report.tenants[0].holds_released == 1
    assert (await get(s, bid))["status"] == "rejected"
    assert [row[4] for row in await allocations(s, bid)] == ["released"]
    assert (await sweep(s)).tenants[0].holds_released == 0  # and it is done once


async def test_the_sweep_refreshes_the_cached_availability_a_hold_expiry_leaves_stale(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client, default_status="pending")
    mei = await s.add_staff("Mei")
    await book(s, mei, at(s.day, 10))
    assert "10:00" not in starts(await avail(s)) and (await avail(s)).headers["X-Cache"] == "HIT"  # held, and cached that way
    async with tenant_session(s.tenant_id) as db:
        await db.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute'"))  # time passes; no write is seen
    assert "10:00" not in starts(await avail(s))  # the stale answer is still being served
    await sweep(s)
    fresh = await avail(s)
    assert fresh.headers["X-Cache"] == "MISS" and "10:00" in starts(fresh)


# --------------------------------------------------------------------- flags for bookings that raced a settings change
async def test_a_booking_that_raced_a_settings_change_is_flagged_by_the_sweep(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The booking is validated against the old opening hours, the hours then change and re-check the bookings that
    exist (this one is not committed yet), and only then does the booking commit. Nothing flags it - until the sweep."""
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    await put_hours(s, "09:00:00", "18:00:00")
    decided, changed = asyncio.Event(), asyncio.Event()
    real = booking_module.get_allocator

    class PauseAfterDeciding:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def allocate(self, *a: Any, **k: Any) -> Any:
            result = await self.inner.allocate(*a, **k)  # 16:00 is inside 09:00-18:00
            decided.set()
            await asyncio.wait_for(changed.wait(), timeout=10)
            return result

    async def close_earlier() -> httpx.Response:
        await asyncio.wait_for(decided.wait(), timeout=10)
        r = await put_hours(s, "09:00:00", "15:00:00")  # commits while the booking is still in flight
        changed.set()
        return r

    with monkeypatch.context() as m:
        m.setattr(booking_module, "get_allocator", lambda bt: PauseAfterDeciding(real(bt)))
        booked, change = await asyncio.gather(s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 16), mei), headers=s.pk), close_earlier())
    assert booked.status_code == 201 and change.headers[HEADER] == "0"  # the change found nothing to flag
    bid = str(booked.json()["id"])
    assert await flags(s, bid) == []  # the gap: it now sits outside the opening hours, unflagged
    assert await checked_at(s, bid) is None

    report = await sweep(s, now=soon())
    assert report.tenants[0].bookings_flagged == 1
    assert await flags(s, bid) == ["outside_operating_hours"]
    assert await checked_at(s, bid) is not None


async def test_a_dismissed_flag_is_not_raised_again_by_the_next_sweep(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 16))
    async with tenant_session(seeded.tenant_id) as db:  # hours change without the change-time re-check seeing this booking
        await db.execute(text("INSERT INTO operating_hours (tenant_id, weekday, open_time, close_time) SELECT :t, d, '09:00', '15:00' FROM generate_series(0, 6) d"), {"t": seeded.tenant_id})
    assert await flags(seeded, bid) == []
    await sweep(seeded, now=soon())
    assert await flags(seeded, bid) == ["outside_operating_hours"]
    assert (await seeded.client.post(f"/v1/admin/bookings/{bid}/clear-flags", headers=seeded.admin)).status_code == 200
    report = await sweep(seeded, now=soon(10))
    assert report.tenants[0].bookings_checked == 0 and await flags(seeded, bid) == []  # looked at once, then left alone
    newcomer = await book(seeded, mei, at(seeded.day, 10))  # a new booking is swept ...
    report = await sweep(seeded, now=soon(20))
    assert report.tenants[0].bookings_checked == 1 and await checked_at(seeded, newcomer) is not None
    assert await flags(seeded, bid) == []  # ... and only it: the dismissed booking is still outside the hours, and still unflagged


async def test_a_booking_is_only_looked_at_once_it_is_older_than_the_grace_period(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    assert (await sweep(seeded)).tenants[0].bookings_checked == 0 and await checked_at(seeded, bid) is None  # too fresh
    assert (await sweep(seeded, now=soon())).tenants[0].bookings_checked == 1 and await checked_at(seeded, bid) is not None


async def test_bookings_that_no_longer_hold_a_slot_are_marked_without_being_checked(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    assert (await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=seeded.admin)).status_code == 200
    report = await sweep(seeded, now=soon())
    assert report.tenants[0].bookings_checked == 0 and await checked_at(seeded, bid) is not None  # off the queue, no rule run


async def test_a_resumed_job_is_checked_again(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    bid = (await gbook(g, at(g.day, 10), ["brake"])).json()["id"]
    await sweep(g, now=soon())
    assert await checked_at(g, bid) is not None
    d = await get(g, bid)
    mechanic = next(r for r in d["resource_ids"] if uuid.UUID(r) in g.mechanics.values())
    status = lambda new: g.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": new}, headers=g.admin)  # noqa: E731
    assert (await status("in-progress")).status_code == 200 and (await status("waiting-for-parts")).status_code == 200
    r = await g.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": at(g.day, 15).isoformat(), "resource_id": mechanic}, headers=g.admin)
    assert r.status_code == 200, r.text
    assert await checked_at(g, bid) is None  # new allocations: it needs another look
    assert (await sweep(g, now=soon(10))).tenants[0].bookings_checked == 1


async def test_a_large_backlog_is_worked_off_over_several_ticks(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    ids = [await book(seeded, mei, at(seeded.day, h)) for h in (9, 10, 11, 14, 15)]
    monkeypatch.setattr(sweep_module, "BATCH_SIZE", 2)
    monkeypatch.setattr(sweep_module, "MAX_BATCHES_PER_TENANT", 2)
    assert (await sweep(seeded, now=soon())).tenants[0].bookings_checked == 4  # two batches of two
    assert len([i for i in ids if await checked_at(seeded, i) is None]) == 1
    assert (await sweep(seeded, now=soon())).tenants[0].bookings_checked == 1
    assert (await sweep(seeded, now=soon())).tenants[0].bookings_checked == 0


# --------------------------------------------------------------------- several tenants, several workers
async def test_one_failing_tenant_does_not_stop_the_others(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    a, b = await make_seed(client), await make_seed(client)
    bids = {}
    for s in (a, b):
        bids[s.tenant_id] = await book(s, await s.add_staff("Mei"), at(s.day, 10))
    real = flagging.reevaluate

    async def reevaluate(db: Any, tenant: Any, *args: Any, **kw: Any) -> Any:
        if tenant.id == a.tenant_id:
            raise RuntimeError("boom")
        return await real(db, tenant, *args, **kw)

    monkeypatch.setattr(flagging, "reevaluate", reevaluate)
    report = await sweep_all(now=soon(), tenant_ids=[a.tenant_id, b.tenant_id])
    assert report.ran and [t.error for t in report.tenants if t.tenant_id == a.tenant_id] == ["RuntimeError: boom"]
    assert await checked_at(b, bids[b.tenant_id]) is not None  # the healthy tenant was swept
    assert await checked_at(a, bids[a.tenant_id]) is None  # the failed one was rolled back whole and will be retried


async def test_only_one_worker_sweeps_at_a_time(seeded: Seed) -> None:
    async with tenant_session(None) as other_worker:
        await other_worker.execute(select(func.pg_advisory_xact_lock(SWEEP_LOCK_KEY)))  # another worker is mid-sweep
        skipped = await sweep_all(tenant_ids=[seeded.tenant_id])
    assert skipped.ran is False and skipped.tenants == []
    assert (await sweep_all(tenant_ids=[seeded.tenant_id])).ran is True  # and the next tick runs normally


async def test_without_a_tenant_filter_every_active_tenant_is_swept(client: httpx.AsyncClient) -> None:
    a = await make_seed(client)
    report = await sweep_all()
    assert report.ran and a.tenant_id in {t.tenant_id for t in report.tenants}


# --------------------------------------------------------------------- the sweep must not wreck the availability cache
async def test_a_sweep_that_changes_only_flags_and_markers_leaves_the_cache_alone(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, at(s.day, 16))
    async with tenant_session(s.tenant_id) as db:
        await db.execute(text("INSERT INTO operating_hours (tenant_id, weekday, open_time, close_time) SELECT :t, d, '09:00', '15:00' FROM generate_series(0, 6) d"), {"t": s.tenant_id})
    await avail(s)
    assert (await avail(s)).headers["X-Cache"] == "HIT"
    report = await sweep(s, now=soon())
    assert report.tenants[0].bookings_flagged == 1 and await flags(s, bid) == ["outside_operating_hours"]
    assert (await avail(s)).headers["X-Cache"] == "HIT"  # a flag and a marker are not things availability depends on


async def test_only_changes_that_can_alter_availability_invalidate_for_a_booking(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    bid = uuid.UUID(await book(s, await s.add_staff("Mei"), at(s.day, 10)))
    async def token() -> dict[str, str | None]:  # the tenant's token and every date's token
        return {k: await cache.get(k) for k in sorted(await cache.keys(f"avail:gen:{s.tenant_id}*"))}

    await avail(s)
    before = await token()
    async with tenant_session(s.tenant_id) as db:
        b = await db.get(Booking, bid)
        assert b is not None
        b.flags = ["day_off"]
        b.flags_checked_at = dt.datetime.now(dt.UTC)
        b.schedule_conflict = True
        b.special_request = "window seat"
    assert await token() == before  # none of those is something availability reads
    async with tenant_session(s.tenant_id) as db:
        b = await db.get(Booking, bid)
        assert b is not None
        b.hold_expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)  # this one is
    assert await token() != before


async def test_expiring_holds_writes_nothing_when_nothing_has_expired(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    await book(s, await s.add_staff("Mei"), at(s.day, 10))
    await avail(s)
    tokens = lambda: _all_tokens(cache, s)  # noqa: E731
    before = await tokens()
    await sweep(s, now=soon())
    assert await tokens() == before  # a tick with nothing to do costs the cache nothing


# --------------------------------------------------------------------- the worker loop
def test_the_worker_summary_and_arguments() -> None:
    assert "skipped" in worker.summarise(SweepReport(ran=False))
    tenant_id = uuid.uuid4()
    line = worker.summarise(SweepReport(True, [TenantSweep(tenant_id, 2, 3, 1), TenantSweep(uuid.uuid4(), error="X: y")]))
    assert "swept 2 tenants: 2 holds expired, 3 new bookings checked, 1 flagged, 0 events published, 0 reminders queued, 0 webhooks delivered, 0 calendar events written, 0 health notes purged, 1 failed" in line
    assert worker.parse_args([]).once is False and worker.parse_args(["--once"]).once is True


async def test_the_worker_keeps_going_after_a_failed_sweep_and_stops_when_told(monkeypatch: pytest.MonkeyPatch) -> None:
    stop = asyncio.Event()
    calls: list[int] = []

    async def fake_sweep_all() -> SweepReport:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("database restarting")
        if len(calls) == 3:
            stop.set()
        return SweepReport(ran=True)

    monkeypatch.setattr(worker, "sweep_all", fake_sweep_all)
    await asyncio.wait_for(worker.run(0.01, stop), timeout=5)
    assert len(calls) == 3  # the error did not end the loop, and the stop request did


async def test_the_worker_can_sweep_once_and_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    async def fake_sweep_all() -> SweepReport:
        calls.append(1)
        return SweepReport(ran=True)

    monkeypatch.setattr(worker, "sweep_all", fake_sweep_all)
    await asyncio.wait_for(worker.run(3600, asyncio.Event(), once=True), timeout=5)
    assert calls == [1]


async def _all_tokens(cache: Any, s: Seed) -> dict[str, str | None]:
    return {k: await cache.get(k) for k in sorted(await cache.keys(f"avail:gen:{s.tenant_id}*"))}
