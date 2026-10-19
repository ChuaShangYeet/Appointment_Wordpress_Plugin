"""Per-date invalidation: the edges found by mutation testing."""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import Range

from app.core import availability_cache
from app.core.availability_cache import MAX_DATES, DATE_TOKEN_TTL_SECONDS, _bulk_update_matters, _mark
from app.core.database import tenant_session
from app.models import Booking, BookingAllocation
from tests.conftest import Seed, at, make_seed
from tests.test_availability_cache import avail, cache  # noqa: F401  (``cache`` is a fixture)
from tests.test_availability_cache_dates import DAY, around, assert_all_true, statuses, warm


async def booking_at(s: Seed, staff: uuid.UUID, day: dt.date, hour: int = 10) -> uuid.UUID:
    r = await s.client.post("/v1/public/bookings", json=s.booking_body(at(day, hour), staff), headers=s.pk)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


# --------------------------------------------------------------------- several writes in one transaction
async def test_two_dated_writes_in_one_transaction_clear_both_sets_of_dates(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    near = await booking_at(s, mei, s.day)
    far_day = s.day + 20 * DAY
    far = await booking_at(s, mei, far_day)
    days = sorted({*around(s.day, 6, 6), *around(far_day, 6, 6)})
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:
        for bid in (near, far):
            booking = await db.get(Booking, bid)
            assert booking is not None
            booking.hold_expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
    got = await statuses(s, days)
    assert got[s.day] == "MISS" and got[far_day] == "MISS" and got[s.day + 6 * DAY] == "HIT" and got[far_day - 6 * DAY] == "HIT"
    await assert_all_true(s, days, monkeypatch)


def test_marking_many_dates_escalates_to_the_whole_tenant() -> None:
    tenant = uuid.uuid4()
    session: Any = SimpleNamespace(info={})
    base = dt.date(2030, 1, 1)
    _mark(session, tenant, {base + n * DAY for n in range(MAX_DATES - 10)})
    assert session.info[availability_cache._INFO_DIRTY][tenant] is not None
    _mark(session, tenant, {base + n * DAY for n in range(MAX_DATES - 10, MAX_DATES + 5)})
    assert session.info[availability_cache._INFO_DIRTY][tenant] is None  # too many to list: the whole tenant
    _mark(session, tenant, {base})
    assert session.info[availability_cache._INFO_DIRTY][tenant] is None  # and a whole-tenant mark stays whole
    other = uuid.uuid4()
    _mark(session, other, {base})
    _mark(session, other, {base + DAY})
    assert session.info[availability_cache._INFO_DIRTY][other] == {base, base + DAY}  # (and ordinary marks add up)
    _mark(session, other, None)
    assert session.info[availability_cache._INFO_DIRTY][other] is None
    _mark(session, None, {base})  # no tenant: nothing to remember
    assert None not in session.info[availability_cache._INFO_DIRTY]


# --------------------------------------------------------------------- a booking row that moves, by itself
async def test_a_booking_row_that_moves_clears_where_it_was_and_where_it_is(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = await booking_at(s, mei, s.day)
    far = s.day + 10 * DAY
    days = sorted({*around(s.day, 6, 6), *around(far, 6, 6)})
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:  # (only the booking row: its reservations are untouched)
        booking = await db.get(Booking, bid)
        assert booking is not None
        booking.start_at, booking.end_at = at(far, 10), at(far, 11)
    got = await statuses(s, days)
    assert got[s.day] == "MISS" and got[far] == "MISS" and got[s.day + 4 * DAY] == "HIT" and got[s.day - 6 * DAY] == "HIT"


# --------------------------------------------------------------------- bulk statements
async def test_a_bulk_delete_of_reservations_clears_their_dates(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = await booking_at(s, mei, s.day)
    days = around(s.day, 8, 8)
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:
        await db.execute(delete(BookingAllocation).where(BookingAllocation.booking_id == bid))
    got = await statuses(s, days)
    assert got[s.day] == "MISS" and got[s.day + 6 * DAY] == "HIT" and got[s.day - 6 * DAY] == "HIT"
    await assert_all_true(s, days, monkeypatch)


async def test_a_bulk_update_that_reaches_an_undatable_row_clears_every_date(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = await booking_at(s, mei, s.day)
    async with tenant_session(s.tenant_id) as db:  # a reservation too long to date
        alloc = await db.scalar(select(BookingAllocation).where(BookingAllocation.booking_id == bid))
        assert alloc is not None
        alloc.period = Range(at(s.day, 10), at(s.day, 10) + 30 * DAY, bounds="[)")
    days = around(s.day, 8, 8)
    await warm(s, days)
    async with tenant_session(s.tenant_id) as db:
        await db.execute(update(BookingAllocation).where(BookingAllocation.booking_id == bid).values(state="released"))
    assert set((await statuses(s, days)).values()) == {"MISS"}


def test_a_bulk_update_whose_columns_are_unknown_is_assumed_to_matter() -> None:
    def state(values: Any, *, update_: bool = True) -> Any:
        return SimpleNamespace(is_update=update_, statement=SimpleNamespace(_values=values))

    assert _bulk_update_matters(state({}), "bookings") is True  # (nothing to go by)
    assert _bulk_update_matters(state(None), "bookings") is True
    assert _bulk_update_matters(state({"address_id": None}), "bookings") is False
    assert _bulk_update_matters(state({"address_id": None, "status": "x"}), "bookings") is True
    assert _bulk_update_matters(state({"flags": []}), "booking_allocations") is True  # only bookings have columns that do not matter
    assert _bulk_update_matters(state({}, update_=False), "bookings") is True  # a delete always matters


# --------------------------------------------------------------------- tokens
async def test_replaced_date_tokens_expire_and_the_tenant_token_never_does(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    await avail(s)
    await booking_at(s, mei, s.day)
    date_key = f"avail:gen:{s.tenant_id}:d:{s.day.isoformat()}"
    assert 0 < await cache.ttl(date_key) <= DATE_TOKEN_TTL_SECONDS  # replaced by the booking, and still short-lived
    tenant_key = f"avail:gen:{s.tenant_id}"
    await cache.delete(tenant_key)
    await avail(s)  # a missing tenant token is created again by the next read ...
    assert await cache.get(tenant_key) is not None and await cache.ttl(tenant_key) == -1  # ... and, like any tenant token, lasts
    assert availability_cache.DATE_TOKEN_TTL_SECONDS > availability_cache._ttl()  # a date token outlives the answers stored under it
