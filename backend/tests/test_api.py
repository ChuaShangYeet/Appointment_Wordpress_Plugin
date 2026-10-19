"""Tenant isolation (RLS), PDPA, auth, state machine and webhook tests. Need PostgreSQL."""
from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.core.database import get_sessionmaker, tenant_session
from app.core.security import sign_payload
from app.models import Booking, Customer, Resource
from tests.conftest import PLATFORM, Seed, at, make_seed


# ------------------------------------------------------------------ RLS
async def test_rls_hides_other_tenants_rows(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    ra = await a.add_staff("A-staff")
    assert (await client.post("/v1/public/bookings", json=a.booking_body(at(a.day, 10), ra), headers=a.pk)).status_code == 201

    async with tenant_session(a.tenant_id) as s:
        assert await s.scalar(select(func.count()).select_from(Booking)) == 1
    async with tenant_session(b.tenant_id) as s:
        assert await s.scalar(select(func.count()).select_from(Booking)) == 0
        assert await s.scalar(select(func.count()).select_from(Resource)) == 0
    # No tenant bound => nothing visible at all (fail closed).
    async with tenant_session(None) as s:
        assert await s.scalar(select(func.count()).select_from(Booking)) == 0
        assert await s.scalar(select(func.count()).select_from(Customer)) == 0


async def test_rls_blocks_writing_into_another_tenant(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    with pytest.raises(DBAPIError):
        async with tenant_session(a.tenant_id) as s:
            s.add(Resource(tenant_id=b.tenant_id, type="staff", name="smuggled"))
            await s.flush()


async def test_app_role_is_not_superuser_so_rls_is_really_enforced(database: str) -> None:
    async with get_sessionmaker()() as s:
        assert await s.scalar(text("select rolsuper or rolbypassrls from pg_roles where rolname = current_user")) is False


async def test_public_catalog_is_per_tenant(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    ids_a = {x["id"] for x in (await client.get("/v1/public/services", headers=a.pk)).json()}
    ids_b = {x["id"] for x in (await client.get("/v1/public/services", headers=b.pk)).json()}
    assert ids_a == {str(a.service_id)} and ids_b == {str(b.service_id)}
    # Tenant B cannot book tenant A's service.
    r = await client.post("/v1/public/bookings", json=a.booking_body(at(a.day, 10)), headers=b.pk)
    assert r.status_code == 404


# ------------------------------------------------------------------ PDPA
async def test_health_notes_encrypted_at_rest_and_role_gated(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    note = "Type-2 diabetes; latex allergy"
    r = await seeded.client.post(
        "/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei, health_notes=note), headers=seeded.pk
    )
    assert r.status_code == 201 and "health" not in r.text
    bid = r.json()["id"]

    async with tenant_session(seeded.tenant_id) as s:
        raw = await s.scalar(text("select health_notes_enc from bookings where id = :i"), {"i": uuid.UUID(bid)})
    assert raw is not None and b"diabetes" not in bytes(raw) and b"latex" not in bytes(raw)

    owner = await seeded.client.get(f"/v1/admin/bookings/{bid}", headers=seeded.admin)
    assert owner.json()["health_notes"] == note
    staff = await seeded.client.get(f"/v1/admin/bookings/{bid}", headers=await seeded.token("staff"))
    assert staff.status_code == 200 and staff.json()["health_notes"] is None and staff.json()["has_health_notes"] is True
    listing = await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)
    assert "diabetes" not in listing.text


# ------------------------------------------------------------------ auth
async def test_auth_failures(client: httpx.AsyncClient, seeded: Seed) -> None:
    assert (await client.get("/v1/public/services")).status_code == 401
    assert (await client.get("/v1/public/services", headers={"X-Site-Key": "pk_nope"})).status_code == 401
    # a secret key must not be accepted as a publishable key, and vice versa
    assert (await client.get("/v1/public/services", headers={"X-Site-Key": seeded.sk["X-API-Key"]})).status_code == 401
    assert (await client.post("/v1/auth/token", json={"user_id": "u", "role": "owner"}, headers={"X-API-Key": seeded.pk["X-Site-Key"]})).status_code == 401
    assert (await client.get("/v1/admin/bookings")).status_code == 401
    assert (await client.get("/v1/admin/bookings", headers={"Authorization": "Bearer junk"})).status_code == 401
    assert (await client.post("/v1/auth/tenants", json={"name": "x"}, headers={"X-Platform-Token": "wrong"})).status_code == 401


async def test_role_hierarchy(seeded: Seed) -> None:
    staff = await seeded.token("staff")
    manager = await seeded.token("manager")
    body = {"name": "Facial", "duration_minutes": 30}
    assert (await seeded.client.post("/v1/admin/services", json=body, headers=staff)).status_code == 403
    assert (await seeded.client.post("/v1/admin/services", json=body, headers=manager)).status_code == 201
    assert (await seeded.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=manager)).status_code == 403
    r = await seeded.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=seeded.admin)
    assert r.status_code == 201
    # the new key works
    assert (await seeded.client.get("/v1/public/services", headers={"X-Site-Key": r.json()["key"]})).status_code == 200


async def test_site_origin_allowlist(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "O", "allowed_origins": ["https://good.example"]})
    pk = {"X-Site-Key": r.json()["publishable_key"]}
    assert (await client.get("/v1/public/services", headers={**pk, "Origin": "https://good.example"})).status_code == 200
    assert (await client.get("/v1/public/services", headers={**pk, "Origin": "https://evil.example"})).status_code == 403


async def test_staff_token_pinned_to_resource_only_sees_and_edits_own_bookings(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    b_mei = (await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)).json()["id"]
    b_lin = (await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), lin), headers=seeded.pk)).json()["id"]
    tok = await seeded.token("staff", resource_id=mei)
    ids = [b["id"] for b in (await seeded.client.get("/v1/admin/bookings", headers=tok)).json()]
    assert ids == [b_mei]
    assert (await seeded.client.patch(f"/v1/admin/bookings/{b_lin}/status", json={"status": "completed"}, headers=tok)).status_code == 403


# ------------------------------------------------------------------ state machine
async def test_pending_flow_and_invalid_transitions(client: httpx.AsyncClient) -> None:
    seed = await make_seed(client, default_status="pending")
    mei = await seed.add_staff("Mei")
    bid = (await client.post("/v1/public/bookings", json=seed.booking_body(at(seed.day, 10), mei), headers=seed.pk)).json()["id"]

    def patch(status: str) -> object:
        return client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": status}, headers=seed.admin)

    assert (await patch("completed")).status_code == 409  # pending -> completed is not allowed
    r = await patch("confirmed")
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["hold_expires_at"] is None
    assert (await patch("confirmed")).status_code == 409
    assert (await patch("in-progress")).status_code == 409  # spa does not use the arrival step
    assert (await patch("completed")).status_code == 200
    assert (await patch("cancelled")).status_code == 409  # terminal
    # completed releases the resource
    assert (await client.post("/v1/public/bookings", json=seed.booking_body(at(seed.day, 10), mei), headers=seed.pk)).status_code == 201


async def test_policy_snapshot_is_frozen_on_the_booking(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = (await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)).json()["id"]
    await seeded.client.post("/v1/admin/policies", headers=seeded.admin, json={"name": "Strict", "min_notice_cancel": 7 * 86400, "make_default": True})
    detail = (await seeded.client.get(f"/v1/admin/bookings/{bid}", headers=seeded.admin)).json()
    assert detail["policy_snapshot"]["name"] == "Default" and detail["policy_snapshot"]["min_notice_cancel_s"] == 86400


def test_every_business_type_has_an_allocator() -> None:
    """All five verticals are implemented: each business type resolves to its own allocator."""
    from app.models.tenant import BUSINESS_TYPES
    from app.services.allocators import get_allocator
    from app.services.allocators.base import BaseAllocator

    names = {bt: type(get_allocator(bt)).__name__ for bt in BUSINESS_TYPES}
    assert all(isinstance(get_allocator(bt), BaseAllocator) for bt in BUSINESS_TYPES)
    assert names == {
        "spa": "StaffAllocator", "salon": "StylistUpsellAllocator", "restaurant": "RestaurantAllocator",
        "automotive": "WorkshopAllocator", "professional": "FieldServiceAllocator",
    }


# ------------------------------------------------------------------ WordPress sync webhook
async def test_sync_webhook_hmac(seeded: Seed) -> None:
    body = json.dumps({"event": "site.updated", "data": {"allowed_origins": ["https://wp.example"]}}).encode()
    ts = str(int(time.time()))

    def headers(sig: str, stamp: str = ts) -> dict[str, str]:
        return {**seeded.pk, "X-Timestamp": stamp, "X-Signature": sig, "Content-Type": "application/json"}

    good = sign_payload(seeded.webhook_secret, ts, body)
    assert (await seeded.client.post("/v1/sync/webhook", content=body, headers=headers("0" * 64))).status_code == 401
    assert (await seeded.client.post("/v1/sync/webhook", content=body + b" ", headers=headers(good))).status_code == 401
    old = str(int(time.time()) - 3600)
    assert (await seeded.client.post("/v1/sync/webhook", content=body, headers=headers(sign_payload(seeded.webhook_secret, old, body), old))).status_code == 401
    assert (await seeded.client.post("/v1/sync/webhook", content=body, headers=headers(good))).status_code == 200
    # the update took effect: a foreign Origin is now refused, the new one allowed
    assert (await seeded.client.get("/v1/public/services", headers={**seeded.pk, "Origin": "https://other.example"})).status_code == 403
    assert (await seeded.client.get("/v1/public/services", headers={**seeded.pk, "Origin": "https://wp.example"})).status_code == 200
