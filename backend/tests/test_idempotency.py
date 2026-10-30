"""Idempotency-Key on ``POST /public/bookings``: the same request sent twice (a double click, a retry after a time-out) is one booking."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.database import tenant_session
from app.models import Tenant
from app.schemas.public import BookingCreate, CustomerIn
from app.services import booking as booking_service
from tests.conftest import Seed, at, make_seed

REPLAYED = "idempotent-replayed"


def key() -> str:
    return str(uuid.uuid4())


async def send(s: Seed, body: dict[str, Any], idempotency_key: str | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    extra = {"Idempotency-Key": idempotency_key} if idempotency_key is not None else {}
    return await s.client.post("/v1/public/bookings", json=body, headers={**(headers or s.pk), **extra})


async def count(s: Seed, table: str = "bookings", where: str = "true") -> int:
    async with tenant_session(s.tenant_id) as db:
        return int((await db.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"))).scalar_one())  # noqa: S608 - test-only, fixed arguments


async def test_the_same_request_twice_is_one_booking(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    body = seeded.booking_body(at(seeded.day, 10), mei)
    k = key()
    first, second = await send(seeded, body, k), await send(seeded, body, k)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() and REPLAYED not in first.headers and second.headers[REPLAYED] == "true"
    assert await count(seeded) == 1 and await count(seeded, "customers") == 1
    assert await count(seeded, "outbox_events", "event_type = 'booking.confirmed'") == 1  # nothing was announced twice
    assert await count(seeded, "booking_allocations") == 1


async def test_without_a_key_every_request_is_its_own_booking(seeded: Seed) -> None:
    """Unchanged: a client that sends no key gets no protection (two staff, same time, same person)."""
    await seeded.add_staff("Mei")
    await seeded.add_staff("Lin")
    body = seeded.booking_body(at(seeded.day, 10), None, customer={"name": "Aisha", "phone": "+601111110009"})
    first, second = await send(seeded, body), await send(seeded, body)
    assert first.status_code == second.status_code == 201 and first.json()["id"] != second.json()["id"]
    assert await count(seeded) == 2


async def test_the_second_copy_does_not_use_up_the_plans_monthly_bookings(client: httpx.AsyncClient) -> None:
    from tests.test_plans import on

    s = await on(client, bookings_per_month=1)
    mei = await s.add_staff("Mei")
    body = s.booking_body(at(s.day, 10), mei)
    k = key()
    assert (await send(s, body, k)).status_code == 201
    assert (await send(s, body, k)).status_code == 201  # replayed: no second place is needed
    other = s.booking_body(at(s.day, 12), mei)
    assert (await send(s, other, key())).status_code == 402  # a genuinely new booking is over the limit


async def test_the_same_key_with_another_request_is_refused_and_changes_nothing(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    k = key()
    assert (await send(seeded, seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": "+601111110001"}), k)).status_code == 201
    for changed in (
        seeded.booking_body(at(seeded.day, 12), mei, customer={"name": "Aisha", "phone": "+601111110001"}),  # another time
        seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": "+601111110002"}),  # another person
        seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "Aisha K", "phone": "+601111110001"}),  # another name
        seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": "+601111110001"}, special_request="window seat"),
    ):
        r = await send(seeded, changed, k)
        assert r.status_code == 409 and r.json()["error"] == "idempotency_key_reused", changed
    assert await count(seeded) == 1


async def test_spacing_and_key_order_in_the_json_do_not_matter(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    body = seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": "+601111110001"})
    k = key()
    first = await send(seeded, body, k)
    reordered = json.dumps(dict(reversed(list(body.items()))), indent=4)
    second = await seeded.client.post("/v1/public/bookings", content=reordered, headers={**seeded.pk, "Idempotency-Key": k, "Content-Type": "application/json"})
    assert second.status_code == 201 and second.json() == first.json() and second.headers[REPLAYED] == "true"


async def test_two_copies_at_the_same_moment_make_one_booking(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    await seeded.add_staff("Lin")
    body = seeded.booking_body(at(seeded.day, 10), None)  # "any available": without the key the two would take one stylist each
    k = key()
    answers = await asyncio.gather(*(send(seeded, body, k) for _ in range(4)))
    assert [a.status_code for a in answers] == [201] * 4 and len({a.json()["id"] for a in answers}) == 1
    assert sorted(REPLAYED in a.headers for a in answers) == [False, True, True, True]
    assert await count(seeded) == 1


async def test_a_request_that_failed_leaves_its_key_free(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    k = key()
    nobody_works_at_3am = await send(seeded, seeded.booking_body(at(seeded.day, 3), mei), k)
    assert nobody_works_at_3am.status_code == 409
    assert await count(seeded) == 0
    retry = await send(seeded, seeded.booking_body(at(seeded.day, 10), mei), k)  # a different time, same key: nothing was made, so nothing is reused
    assert retry.status_code == 201 and await count(seeded) == 1


async def test_a_slot_taken_in_between_is_not_hidden_by_a_replay_of_another_key(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    body = seeded.booking_body(at(seeded.day, 10), mei)
    assert (await send(seeded, body, key())).status_code == 201
    again = await send(seeded, body, key())  # a new key is a new request: the slot is gone
    assert again.status_code == 409 and await count(seeded) == 1


async def test_a_replay_returns_the_booking_as_it_is_now(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    body = seeded.booking_body(at(seeded.day, 10), mei)
    k = key()
    first = (await send(seeded, body, k)).json()
    assert (await seeded.client.patch(f"/v1/admin/bookings/{first['id']}/status", json={"status": "cancelled"}, headers=seeded.admin)).status_code == 200
    replay = await send(seeded, body, k)
    assert replay.status_code == 201 and replay.json()["id"] == first["id"] and replay.json()["status"] == "cancelled"
    assert await count(seeded) == 1  # the key is the customer's one submission: it does not rebook what was cancelled


async def test_keys_belong_to_one_business(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    k = key()
    for s in (a, b):
        mei = await s.add_staff("Mei")
        assert (await send(s, s.booking_body(at(s.day, 10), mei), k)).status_code == 201
    assert await count(a) == 1 and await count(b) == 1


async def test_a_secret_key_request_and_a_site_key_request_share_the_business_keys(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    body = seeded.booking_body(at(seeded.day, 10), mei)
    k = key()
    first = await send(seeded, body, k, headers=seeded.sk)
    second = await send(seeded, body, k)
    assert first.status_code == second.status_code == 201 and first.json()["id"] == second.json()["id"]


@pytest.mark.parametrize("good", [str(uuid.uuid4()), "a" * 8, "a" * 100, "order:2026-10-14.abc_def-1", "ABCDEFGH"])
async def test_ordinary_keys_are_accepted(seeded: Seed, good: str) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await send(seeded, seeded.booking_body(at(seeded.day, 10), mei), good)).status_code == 201


@pytest.mark.parametrize("bad", ["", "short", "a" * 7, "a" * 101, "has space 12345", "semi;colon-123", "slash/12345678", "quote\"12345678"])
async def test_odd_keys_are_refused_before_anything_is_made(seeded: Seed, bad: str) -> None:
    mei = await seeded.add_staff("Mei")
    r = await send(seeded, seeded.booking_body(at(seeded.day, 10), mei), bad)
    assert r.status_code == 422 and "Idempotency-Key" in r.json()["detail"]
    assert await count(seeded) == 0


async def test_the_database_allows_one_booking_per_business_and_key(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")

    async def make(hour: int) -> None:
        async with tenant_session(seeded.tenant_id) as db:
            tenant = await db.get(Tenant, seeded.tenant_id)
            assert tenant is not None
            data = BookingCreate(service_ids=[seeded.service_id], start_at=at(seeded.day, hour), resource_id=mei, customer=CustomerIn(name="A", phone="+601111110001"))
            await booking_service.create_booking(db, tenant, data, idempotency=("same-key-12345", "hash"))

    await make(10)
    with pytest.raises(IntegrityError):
        await make(12)


async def test_bookings_without_a_key_are_not_limited_by_the_index(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    for hour in (10, 12, 14):
        assert (await send(seeded, seeded.booking_body(at(seeded.day, hour), mei))).status_code == 201
    assert await count(seeded, "bookings", "idempotency_key IS NULL") == 3


async def test_the_replay_header_can_be_read_by_a_browser() -> None:
    from fastapi.middleware.cors import CORSMiddleware

    from app.main import app

    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    assert "Idempotent-Replayed" in cors.kwargs["expose_headers"] and cors.kwargs["allow_headers"] == ["*"]
