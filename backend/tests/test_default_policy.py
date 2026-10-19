"""A new business can take bookings without first writing booking rules: sign-up and the operator's ``POST /auth/tenants`` give it a default policy."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
from sqlalchemy import select

from app.core.database import tenant_session
from app.models import BookingPolicy, Tenant
from tests.conftest import PLATFORM, Recorder, Seed, at
from tests.test_signup import open_signup, sign_up  # noqa: F401  (fixture)
from tests.test_ratelimit import redis  # noqa: F401  (fixture)


async def _ready(client: httpx.AsyncClient, data: dict[str, Any]) -> Seed:
    """Staff with shifts and a service, but no policy written by the owner."""
    seed = Seed(client, data)
    seed.admin = await seed.token("owner")
    r = await client.post("/v1/admin/services", headers=seed.admin, json={"name": "Cut", "duration_minutes": 60, "buffer_minutes": 0, "price": "50.00"})
    assert r.status_code == 201, r.text
    seed.service_id = uuid.UUID(r.json()["id"])
    await seed.add_staff("Mei")
    return seed


async def _takes_bookings(seed: Seed) -> None:
    q = {"date": seed.day.isoformat(), "service_ids": str(seed.service_id)}
    r = await seed.client.get("/v1/public/availability", params=q, headers=seed.pk)
    assert r.status_code == 200, r.text
    assert r.json()["slots"]
    r = await seed.client.post("/v1/public/bookings", json=seed.booking_body(at(seed.day, 10)), headers=seed.pk)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "confirmed"


async def _the_default(tenant_id: uuid.UUID) -> BookingPolicy:
    async with tenant_session(tenant_id) as db:
        tenant = await db.get(Tenant, tenant_id)
        assert tenant is not None and tenant.default_policy_id is not None
        policies = list((await db.scalars(select(BookingPolicy))).all())
    assert [p.id for p in policies] == [tenant.default_policy_id]  # exactly one, and it is the default
    return policies[0]


def _has_the_usual_rules(p: BookingPolicy) -> None:
    assert (p.name, p.default_status) == ("Default", "confirmed")
    assert (p.min_notice_booking, p.min_notice_cancel, p.booking_window, p.pending_timeout) == (
        dt.timedelta(hours=2), dt.timedelta(hours=24), dt.timedelta(days=60), dt.timedelta(minutes=30),
    )


async def test_a_business_that_signed_itself_up_can_take_bookings_at_once(client: httpx.AsyncClient, open_signup: Recorder) -> None:  # noqa: F811
    _, keys = await sign_up(client, open_signup)
    _has_the_usual_rules(await _the_default(uuid.UUID(keys["tenant_id"])))
    await _takes_bookings(await _ready(client, keys))


async def test_a_business_the_operator_made_can_take_bookings_at_once(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Spa {uuid.uuid4().hex[:6]}", "business_type": "spa"})
    assert r.status_code == 201, r.text
    _has_the_usual_rules(await _the_default(uuid.UUID(r.json()["tenant_id"])))
    seed = await _ready(client, r.json())
    listed = (await client.get("/v1/admin/policies", headers=seed.admin)).json()
    assert [p["name"] for p in listed] == ["Default"]  # the owner can see and change it
    await _takes_bookings(seed)


async def test_the_owner_can_still_make_another_policy_the_default(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Spa {uuid.uuid4().hex[:6]}"})
    seed = await _ready(client, r.json())
    r = await client.post("/v1/admin/policies", headers=seed.admin, json={"name": "Ask first", "default_status": "pending", "make_default": True})
    assert r.status_code == 201, r.text
    async with tenant_session(seed.tenant_id) as db:
        tenant = await db.get(Tenant, seed.tenant_id)
    assert tenant is not None and str(tenant.default_policy_id) == r.json()["id"]
    r = await client.post("/v1/public/bookings", json=seed.booking_body(at(seed.day, 10)), headers=seed.pk)
    assert r.status_code == 201 and r.json()["status"] == "pending"
