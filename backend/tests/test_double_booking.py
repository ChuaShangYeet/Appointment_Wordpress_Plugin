"""Concurrency tests proving the GiST exclusion constraint prevents double booking."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import Range
from sqlalchemy.exc import IntegrityError

from app.core.database import tenant_session
from app.models import Booking, BookingAllocation
from app.services.booking import is_exclusion_violation, release_expired_holds
from tests.conftest import Seed, at, make_seed


async def _active_allocations(seed: Seed, resource_id: uuid.UUID) -> int:
    async with tenant_session(seed.tenant_id) as s:
        return int(await s.scalar(
            select(func.count()).select_from(BookingAllocation)
            .where(BookingAllocation.resource_id == resource_id, BookingAllocation.state.in_(("held", "active")))
        ) or 0)


async def test_exactly_one_of_two_simultaneous_bookings_wins(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    start = at(seeded.day, 10)
    body = seeded.booking_body(start, mei)
    other = seeded.booking_body(start, mei)  # different customer, same resource + time

    r1, r2 = await asyncio.gather(
        seeded.client.post("/v1/public/bookings", json=body, headers=seeded.pk),
        seeded.client.post("/v1/public/bookings", json=other, headers=seeded.pk),
    )
    codes = sorted([r1.status_code, r2.status_code])
    assert codes == [201, 409], (r1.text, r2.text)

    loser = r1 if r1.status_code == 409 else r2
    payload = loser.json()
    assert payload["error"] in {"slot_conflict", "slot_unavailable"}
    assert "slots" in payload  # refreshed availability for the client to re-pick
    assert all(dt.datetime.fromisoformat(s["start_at"]) != start for s in payload["slots"])  # taken slot not re-offered

    # No corrupted state: one booking, one live allocation.
    assert await _active_allocations(seeded, mei) == 1
    async with tenant_session(seeded.tenant_id) as s:
        assert await s.scalar(select(func.count()).select_from(Booking)) == 1


async def test_many_way_race_has_a_single_winner(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    start = at(seeded.day, 15)
    results = await asyncio.gather(*[
        seeded.client.post("/v1/public/bookings", json=seeded.booking_body(start, mei), headers=seeded.pk)
        for _ in range(12)
    ])
    codes = [r.status_code for r in results]
    assert codes.count(201) == 1 and codes.count(409) == 11, codes
    assert await _active_allocations(seeded, mei) == 1


async def test_overlapping_but_not_identical_windows_also_conflict(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    a, b = await asyncio.gather(
        seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk),
        seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10, 30), mei), headers=seeded.pk),
    )
    assert sorted([a.status_code, b.status_code]) == [201, 409]


async def test_adjacent_bookings_are_allowed(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    a, b = await asyncio.gather(
        seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk),
        seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 11), mei), headers=seeded.pk),
    )
    assert (a.status_code, b.status_code) == (201, 201)  # half-open ranges: [10,11) and [11,12) don't overlap


async def test_race_across_any_available_uses_both_staff_then_conflicts(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    await seeded.add_staff("Lin")
    start = at(seeded.day, 10)
    results = await asyncio.gather(*[
        seeded.client.post("/v1/public/bookings", json=seeded.booking_body(start), headers=seeded.pk) for _ in range(6)
    ])
    codes = [r.status_code for r in results]
    assert codes.count(201) == 2 and codes.count(409) == 4, codes  # exactly two staff exist
    winners = [r.json()["resource_ids"][0] for r in results if r.status_code == 201]
    assert len(set(winners)) == 2


async def test_database_itself_rejects_overlap_regardless_of_application_code(seeded: Seed) -> None:
    """Bypass the service layer: raw inserts must still be refused by the constraint."""
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert r.status_code == 201
    booking_id = uuid.UUID(r.json()["id"])
    period = Range(at(seeded.day, 10, 30), at(seeded.day, 11, 30), bounds="[)")
    with pytest.raises(IntegrityError) as ei:
        async with tenant_session(seeded.tenant_id) as s:
            s.add(BookingAllocation(tenant_id=seeded.tenant_id, booking_id=booking_id, resource_id=mei, period=period, state="active"))
            await s.flush()
    assert is_exclusion_violation(ei.value)


async def test_released_allocation_frees_the_slot(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    bid = r.json()["id"]
    r = await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=seeded.admin)
    assert r.status_code == 200, r.text
    again = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert again.status_code == 201


async def test_expired_pending_hold_is_released_and_slot_rebookable(client: httpx.AsyncClient) -> None:
    seed = await make_seed(client, default_status="pending")
    mei = await seed.add_staff("Mei")
    start = at(seed.day, 10)
    r = await client.post("/v1/public/bookings", json=seed.booking_body(start, mei), headers=seed.pk)
    assert r.status_code == 201 and r.json()["status"] == "pending" and r.json()["hold_expires_at"]
    # While held, the slot is taken.
    assert (await client.post("/v1/public/bookings", json=seed.booking_body(start, mei), headers=seed.pk)).status_code == 409

    # Force the hold into the past, then run the expiry job.
    async with tenant_session(seed.tenant_id) as s:
        await s.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute'"))
    async with tenant_session(seed.tenant_id) as s:
        assert await release_expired_holds(s) == 1
    assert (await client.post("/v1/public/bookings", json=seed.booking_body(start, mei), headers=seed.pk)).status_code == 201


async def test_expired_hold_frees_slot_even_without_the_worker(client: httpx.AsyncClient) -> None:
    """The reservation path itself sweeps expired holds, so correctness never depends on the worker."""
    seed = await make_seed(client, default_status="pending")
    mei = await seed.add_staff("Mei")
    start = at(seed.day, 10)
    assert (await client.post("/v1/public/bookings", json=seed.booking_body(start, mei), headers=seed.pk)).status_code == 201
    async with tenant_session(seed.tenant_id) as s:
        await s.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute'"))
    assert (await client.post("/v1/public/bookings", json=seed.booking_body(start, mei), headers=seed.pk)).status_code == 201


class _BarrierAllocator:
    """Wraps the real allocator; every request waits here after its pre-check passed, so all
    of them reach the INSERT together. Only the exclusion constraint can then pick a winner."""

    def __init__(self, inner: object, barrier: asyncio.Barrier) -> None:
        self.inner, self.barrier = inner, barrier
        self.waited = False  # only the first decision waits; a retry after a lost race must not block

    def __getattr__(self, name: str) -> object:
        """Everything except ``allocate`` is the real allocator's, so new allocator members just work."""
        return getattr(self.inner, name)

    async def allocate(self, *a: object, **k: object) -> object:
        result = await self.inner.allocate(*a, **k)  # type: ignore[attr-defined]
        if not self.waited:
            self.waited = True
            await asyncio.wait_for(self.barrier.wait(), timeout=10)
        return result


@pytest.mark.parametrize("n", [2, 8])
async def test_constraint_alone_decides_when_every_request_passes_the_precheck(
    seeded: Seed, monkeypatch: pytest.MonkeyPatch, n: int
) -> None:
    import app.services.booking as booking_module

    mei = await seeded.add_staff("Mei")
    barrier = asyncio.Barrier(n)
    real = booking_module.get_allocator
    monkeypatch.setattr(booking_module, "get_allocator", lambda bt: _BarrierAllocator(real(bt), barrier))

    start = at(seeded.day, 10)
    results = await asyncio.gather(*[
        seeded.client.post("/v1/public/bookings", json=seeded.booking_body(start, mei), headers=seeded.pk) for _ in range(n)
    ])
    codes = sorted(r.status_code for r in results)
    assert codes == [201] + [409] * (n - 1), [r.text for r in results]
    for loser in (r for r in results if r.status_code == 409):
        body = loser.json()
        assert body["error"] == "slot_conflict"  # came from 23P01, not from the pre-check
        assert all(dt.datetime.fromisoformat(s["start_at"]) != start for s in body["slots"])
    assert await _active_allocations(seeded, mei) == 1
    async with tenant_session(seeded.tenant_id) as s:
        assert await s.scalar(select(func.count()).select_from(Booking)) == 1  # losers left nothing behind


async def test_any_available_loser_is_rerouted_to_the_free_staff_member(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both requests are forced to pick the *same* staff member (identical load, deterministic tie-break).
    The constraint rejects one insert, but another staff member is free, so that request must be
    re-routed rather than told the slot is gone."""
    import app.services.booking as booking_module

    await seeded.add_staff("Mei")
    await seeded.add_staff("Lin")
    barrier = asyncio.Barrier(2)
    real = booking_module.get_allocator
    monkeypatch.setattr(booking_module, "get_allocator", lambda bt: _BarrierAllocator(real(bt), barrier))

    a, b = await asyncio.gather(*[
        seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10)), headers=seeded.pk)
        for _ in range(2)
    ])
    assert (a.status_code, b.status_code) == (201, 201), (a.text, b.text)
    assert a.json()["resource_ids"] != b.json()["resource_ids"]

    # A third request has nobody left: that is a genuine "taken", with fresh slots for the client.
    monkeypatch.undo()
    c = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10)), headers=seeded.pk)
    assert c.status_code == 409 and "slots" in c.json()


async def _inject_deadlocks(monkeypatch: pytest.MonkeyPatch, times: int) -> dict[str, int]:
    """Make the next ``times`` allocation inserts fail exactly as PostgreSQL's deadlock victim does (SQLSTATE 40P01)."""
    from asyncpg.exceptions import DeadlockDetectedError
    from sqlalchemy.exc import DBAPIError
    from sqlalchemy.ext.asyncio import AsyncSession

    state = {"raised": 0}
    real_flush = AsyncSession.flush

    async def flush(self: AsyncSession, *a: object, **k: object) -> None:
        if state["raised"] < times and any(isinstance(o, BookingAllocation) for o in self.new):
            state["raised"] += 1
            raise DBAPIError("INSERT INTO booking_allocations ...", {}, DeadlockDetectedError("deadlock detected"))
        await real_flush(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(AsyncSession, "flush", flush)
    return state


async def test_a_deadlock_victim_is_treated_like_a_lost_race_not_a_server_error(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two simultaneous inserts into the exclusion constraint can each wait for the other; PostgreSQL then aborts
    one with ``deadlock_detected``. Nothing is double booked, and the loser must be re-decided like any other loser."""
    await seeded.add_staff("Mei")
    await seeded.add_staff("Lin")
    state = await _inject_deadlocks(monkeypatch, times=1)
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10)), headers=seeded.pk)
    assert r.status_code == 201, r.text  # re-decided after the deadlock, not a 500
    assert state["raised"] == 1
    async with tenant_session(seeded.tenant_id) as s:
        assert await s.scalar(select(func.count()).select_from(Booking)) == 1


async def test_a_deadlock_on_every_attempt_ends_as_slot_conflict_with_fresh_slots(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    await seeded.add_staff("Mei")
    state = await _inject_deadlocks(monkeypatch, times=99)
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10)), headers=seeded.pk)
    assert r.status_code == 409 and r.json()["error"] == "slot_conflict" and "slots" in r.json()
    assert state["raised"] >= 1
    monkeypatch.undo()
    async with tenant_session(seeded.tenant_id) as s:
        assert await s.scalar(select(func.count()).select_from(Booking)) == 0  # the failed attempts left nothing behind
