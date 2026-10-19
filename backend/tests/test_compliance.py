"""PDPA: the audit log, customers' access and erasure requests, and health-note retention."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.core.database import tenant_session
from tests.conftest import Seed, at, make_seed
from tests.test_customer_cancel import login, otp  # noqa: F401  (``otp`` is a fixture: an in-memory Redis and a fixed code)
from tests.test_flagging import book, get, put_hours
from tests.test_sweep import soon, sweep

PHONE_A = "+60125550001"
PHONE_B = "+60125550002"


async def audit(s: Seed, headers: dict[str, str] | None = None, **params: Any) -> list[dict[str, Any]]:
    r = await s.client.get("/v1/admin/audit-log", params=params, headers=headers or s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def post_booking(s: Seed, staff: uuid.UUID, hour: int, phone: str = PHONE_A, **extra: Any) -> str:
    body = s.booking_body(at(s.day, hour), staff, customer={"name": "Aisha Karim", "phone": phone, "email": "aisha@example.com"}, **extra)
    r = await s.client.post("/v1/public/bookings", json=body, headers=s.pk)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def with_notes(s: Seed, staff: uuid.UUID, hour: int = 10, phone: str = PHONE_A) -> str:
    return await post_booking(s, staff, hour, phone, health_notes="allergic to lavender oil", special_request="quiet room please")


# --------------------------------------------------------------------- the audit log
async def test_successful_admin_changes_are_audited_by_route_actor_and_record_but_never_by_content(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    await put_hours(seeded, "09:00:00", "18:00:00")
    r = await seeded.client.patch(f"/v1/admin/resources/{mei}", json={"name": "Top Secret Stylist"}, headers=seeded.admin)
    assert r.status_code == 200
    entries = await audit(seeded)
    latest = entries[0]
    assert (latest["action"], latest["actor_type"], latest["actor_id"], latest["role"]) == ("PATCH /v1/admin/resources/{resource_id}", "user", "wp-1", "owner")
    assert (latest["target_type"], latest["target_id"]) == ("resource", str(mei))
    assert "PUT /v1/admin/operating-hours" in [e["action"] for e in entries]
    assert "Top Secret" not in json.dumps(entries)  # what changed is not stored, only that it did
    assert all(e["detail"] == {} for e in entries if e["action"].split(" ")[0] in {"PUT", "PATCH", "POST", "DELETE"})  # nothing but the who/what/which


async def test_only_successful_writes_are_audited(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    before = len(await audit(seeded))
    await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)  # a read
    assert (await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "rejected"}, headers=seeded.admin)).status_code == 409  # refused by the state machine
    assert (await seeded.client.patch(f"/v1/admin/resources/{mei}", json={"capacity": 0}, headers=seeded.admin)).status_code == 422  # invalid
    assert (await seeded.client.patch(f"/v1/admin/resources/{mei}", json={"name": "x"}, headers=await seeded.token("staff"))).status_code == 403  # not allowed
    assert (await seeded.client.patch(f"/v1/admin/resources/{uuid.uuid4()}", json={"name": "x"}, headers=seeded.admin)).status_code == 404
    assert len(await audit(seeded)) == before  # none of those changed anything, so none is recorded
    assert (await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "completed"}, headers=seeded.admin)).status_code == 200
    latest = (await audit(seeded))[0]
    assert (latest["action"], latest["target_type"], latest["target_id"]) == ("PATCH /v1/admin/bookings/{booking_id}/status", "booking", bid)


async def test_reading_health_notes_is_audited_and_other_reads_are_not(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    with_n, without = await with_notes(seeded, mei, 10), await post_booking(seeded, mei, 12)
    before = len(await audit(seeded))
    assert (await get(seeded, without))["health_notes"] is None
    assert len(await audit(seeded)) == before  # nothing disclosed
    staff_view = await seeded.client.get(f"/v1/admin/bookings/{with_n}", headers=await seeded.token("staff"))
    assert staff_view.json()["health_notes"] is None and len(await audit(seeded)) == before  # staff never see them
    assert (await get(seeded, with_n))["health_notes"] == "allergic to lavender oil"
    entry = (await audit(seeded))[0]
    assert (entry["action"], entry["target_type"], entry["target_id"], entry["actor_id"]) == ("booking.health_notes.read", "booking", with_n, "wp-1")
    assert "lavender" not in json.dumps(await audit(seeded))


async def test_the_audit_log_is_owner_only_newest_first_and_filterable(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    await put_hours(seeded, "09:00:00", "18:00:00")
    with_n = await with_notes(seeded, mei)
    await get(seeded, with_n)
    assert (await seeded.client.get("/v1/admin/audit-log", headers=await seeded.token("manager"))).status_code == 403
    assert (await seeded.client.get("/v1/admin/audit-log", headers=await seeded.token("staff"))).status_code == 403
    assert (await seeded.client.get("/v1/admin/audit-log")).status_code == 401
    entries = await audit(seeded)
    assert [e["occurred_at"] for e in entries] == sorted((e["occurred_at"] for e in entries), reverse=True)
    assert [e["action"] for e in await audit(seeded, action="health_notes")] == ["booking.health_notes.read"]
    assert await audit(seeded, action="health%notes") == [] and await audit(seeded, action="health.notes") == []  # wildcards are literal
    assert {e["action"] for e in await audit(seeded, actor_id="wp-1")} >= {"PUT /v1/admin/operating-hours"}
    assert await audit(seeded, actor_id="someone-else") == []
    assert [e["target_id"] for e in await audit(seeded, target_id=with_n)] == [with_n]
    page1, page2 = await audit(seeded, limit=1), await audit(seeded, limit=1, offset=1)
    assert len(page1) == len(page2) == 1 and page1[0]["id"] != page2[0]["id"] and page1[0] == entries[0]
    now = dt.datetime.now(dt.UTC)
    assert await audit(seeded, since=(now + dt.timedelta(hours=1)).isoformat()) == []
    assert len(await audit(seeded, until=(now + dt.timedelta(hours=1)).isoformat())) == len(entries)


async def test_each_tenants_audit_log_is_private(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    mei = await a.add_staff("Mei")
    ours, theirs = await audit(a), await audit(b)
    assert str(mei) in {e["target_id"] for e in ours}  # tenant A's own change is in A's log ...
    assert not {e["id"] for e in ours} & {e["id"] for e in theirs} and str(mei) not in {e["target_id"] for e in theirs}  # ... and not B's


async def test_api_key_management_is_audited(seeded: Seed) -> None:
    r = await seeded.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=seeded.admin)
    assert r.status_code == 201, r.text
    entry = (await audit(seeded))[0]
    assert entry["action"] == "POST /v1/auth/api-keys" and entry["role"] == "owner"
    assert r.json()["key"] not in json.dumps(await audit(seeded))  # the secret is shown once, and only to the caller


async def test_the_audit_log_cannot_be_altered_even_by_the_application(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    async with tenant_session(seeded.tenant_id) as db:
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(text("UPDATE audit_log SET action = 'nothing happened'"))
    async with tenant_session(seeded.tenant_id) as db:
        with pytest.raises(DBAPIError, match="append-only"):
            await db.execute(text("DELETE FROM audit_log"))
    assert len(await audit(seeded)) >= 1


async def test_deleting_a_tenant_still_removes_its_audit_entries(client: httpx.AsyncClient) -> None:
    """The append-only trigger must not block the cascade when a whole tenant is deleted."""
    s = await make_seed(client)
    await s.add_staff("Mei")
    assert len(await audit(s)) >= 1
    async with tenant_session(None) as db:
        await db.execute(text("DELETE FROM tenants WHERE id = :t"), {"t": s.tenant_id})
        assert await db.scalar(text("SELECT count(*) FROM audit_log WHERE tenant_id = :t"), {"t": s.tenant_id}) == 0


# --------------------------------------------------------------------- customers' access and erasure requests
async def ask(s: Seed, headers: dict[str, str], kind: str) -> httpx.Response:
    return await s.client.post("/v1/public/me/data-requests", json={"kind": kind}, headers=headers)


async def requests(s: Seed, **params: Any) -> list[dict[str, Any]]:
    r = await s.client.get("/v1/admin/data-requests", params=params, headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_a_customer_can_open_one_request_of_each_kind_and_sees_only_their_own(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    await post_booking(seeded, mei, 10, PHONE_A)
    a, b = await login(seeded, PHONE_A), await login(seeded, PHONE_B)
    first = await ask(seeded, a, "access")
    assert first.status_code == 201 and first.json()["status"] == "pending" and first.json()["kind"] == "access"
    assert (await ask(seeded, a, "access")).status_code == 409  # already open
    assert (await ask(seeded, a, "erasure")).status_code == 201  # a different kind is fine
    assert (await ask(seeded, a, "deletion")).status_code == 422
    assert len((await seeded.client.get("/v1/public/me/data-requests", headers=a)).json()) == 2
    assert (await seeded.client.get("/v1/public/me/data-requests", headers=b)).json() == []
    assert (await seeded.client.post("/v1/public/me/data-requests", json={"kind": "access"}, headers=seeded.pk)).status_code == 401  # needs a customer token
    assert "customer_id" not in first.json() and "processed_by" not in first.json()


async def test_only_the_owner_handles_requests_and_sees_no_personal_data_in_the_list(seeded: Seed, otp: Any) -> None:
    await post_booking(seeded, await seeded.add_staff("Mei"), 10)
    await ask(seeded, await login(seeded, PHONE_A), "access")
    listing = await requests(seeded, status="pending")
    assert len(listing) == 1 and "Aisha" not in json.dumps(listing) and PHONE_A not in json.dumps(listing)
    rid = listing[0]["id"]
    for role in ("manager", "staff"):
        h = await seeded.token(role)
        assert (await seeded.client.get("/v1/admin/data-requests", headers=h)).status_code == 403
        assert (await seeded.client.post(f"/v1/admin/data-requests/{rid}/complete", headers=h)).status_code == 403
    assert await requests(seeded, status="completed") == []


async def test_an_access_request_returns_that_customers_data_only_and_is_audited(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    await with_notes(seeded, mei, 10, PHONE_A)
    await with_notes(seeded, mei, 12, PHONE_B)  # someone else's, with their own notes
    await ask(seeded, await login(seeded, PHONE_A), "access")
    rid = (await requests(seeded))[0]["id"]
    done = await seeded.client.post(f"/v1/admin/data-requests/{rid}/complete", headers=seeded.admin)
    assert done.status_code == 200, done.text
    export = done.json()["export"]
    assert done.json()["request"]["status"] == "completed" and done.json()["request"]["processed_by"] == "wp-1"
    assert export["customer"]["phone"] == PHONE_A and export["customer"]["email"] == "aisha@example.com"
    assert [(b["health_notes"], b["special_request"], b["services"]) for b in export["bookings"]] == [("allergic to lavender oil", "quiet room please", ["Massage"])]
    assert PHONE_B not in json.dumps(export)
    again = await seeded.client.get(f"/v1/admin/data-requests/{rid}/export", headers=seeded.admin)
    assert again.status_code == 200 and again.json()["customer"]["phone"] == PHONE_A
    assert (await seeded.client.post(f"/v1/admin/data-requests/{rid}/complete", headers=seeded.admin)).status_code == 409  # only once
    exports = [e for e in await audit(seeded) if e["action"] == "customer.data.export"]
    assert len(exports) == 2 and all(e["target_type"] == "customer" for e in exports)
    assert "lavender" not in json.dumps(await audit(seeded))


async def test_an_export_is_only_for_access_requests(seeded: Seed, otp: Any) -> None:
    await post_booking(seeded, await seeded.add_staff("Mei"), 10)
    await ask(seeded, await login(seeded, PHONE_A), "erasure")
    rid = (await requests(seeded))[0]["id"]
    assert (await seeded.client.get(f"/v1/admin/data-requests/{rid}/export", headers=seeded.admin)).status_code == 422
    assert (await seeded.client.get(f"/v1/admin/data-requests/{uuid.uuid4()}/export", headers=seeded.admin)).status_code == 404


async def test_erasure_waits_for_open_bookings_then_anonymises_the_customer_and_keeps_the_business_records(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await with_notes(seeded, mei, 10, PHONE_A)
    customer_id = uuid.UUID((await get(seeded, bid))["customer_id"])
    async with tenant_session(seeded.tenant_id) as db:  # saved details a customer accumulates over time
        await db.execute(text("INSERT INTO customer_addresses (tenant_id, customer_id, line1, latitude, longitude) VALUES (:t, :c, '12 Jalan Secret', 3.1, 101.6)"), {"t": seeded.tenant_id, "c": customer_id})
        await db.execute(text("INSERT INTO vehicles (tenant_id, customer_id, plate_no) VALUES (:t, :c, 'WXY1234')"), {"t": seeded.tenant_id, "c": customer_id})
    headers = await login(seeded, PHONE_A)
    await ask(seeded, headers, "erasure")
    rid = (await requests(seeded))[0]["id"]
    blocked = await seeded.client.post(f"/v1/admin/data-requests/{rid}/complete", headers=seeded.admin)
    assert blocked.status_code == 409 and blocked.json()["error"] == "erasure_blocked" and blocked.json()["open_bookings"] == 1
    assert (await requests(seeded))[0]["status"] == "pending" and (await get(seeded, bid))["has_health_notes"] is True  # nothing was touched

    assert (await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=seeded.admin)).status_code == 200
    done = await seeded.client.post(f"/v1/admin/data-requests/{rid}/complete", headers=seeded.admin)
    assert done.status_code == 200 and done.json()["export"] is None and done.json()["request"]["status"] == "completed"

    async with tenant_session(seeded.tenant_id) as db:
        row = (await db.execute(text("SELECT name, phone, email, erased_at IS NOT NULL FROM customers WHERE id = :c"), {"c": customer_id})).one()
        assert (row[0], row[1], row[2], row[3]) == ("Erased customer", f"erased-{customer_id.hex[:24]}", None, True)
        assert await db.scalar(text("SELECT count(*) FROM customer_addresses WHERE customer_id = :c"), {"c": customer_id}) == 0
        assert await db.scalar(text("SELECT count(*) FROM vehicles WHERE customer_id = :c"), {"c": customer_id}) == 0
    kept = await get(seeded, bid)  # the business's record of the appointment stays, with nothing personal left in it
    assert kept["status"] == "cancelled" and kept["has_health_notes"] is False and kept["special_request"] is None and kept["address_id"] is None
    assert [e["action"] for e in await audit(seeded) if e["action"] == "customer.data.erase"] == ["customer.data.erase"]
    fresh = await login(seeded, PHONE_A)  # the old number now belongs to a brand-new customer record
    assert (await seeded.client.get("/v1/public/me/bookings", params={"include_past": "true"}, headers=fresh)).json() == []


async def test_an_erased_customer_cannot_be_erased_again(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await post_booking(seeded, mei, 10)
    headers = await login(seeded, PHONE_A)
    await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=seeded.admin)
    await ask(seeded, headers, "erasure")
    first = (await requests(seeded))[0]["id"]
    assert (await seeded.client.post(f"/v1/admin/data-requests/{first}/complete", headers=seeded.admin)).status_code == 200
    async with tenant_session(seeded.tenant_id) as db:  # a second request for the same (already erased) customer
        cid = await db.scalar(text("SELECT customer_id FROM data_requests WHERE id = :r"), {"r": uuid.UUID(first)})
        await db.execute(text("INSERT INTO data_requests (tenant_id, customer_id, kind) VALUES (:t, :c, 'erasure')"), {"t": seeded.tenant_id, "c": cid})
    second = next(r["id"] for r in await requests(seeded, status="pending"))
    assert (await seeded.client.post(f"/v1/admin/data-requests/{second}/complete", headers=seeded.admin)).status_code == 409


async def test_a_request_can_be_rejected_with_a_reason_the_customer_sees(seeded: Seed, otp: Any) -> None:
    await post_booking(seeded, await seeded.add_staff("Mei"), 10)
    headers = await login(seeded, PHONE_A)
    await ask(seeded, headers, "erasure")
    rid = (await requests(seeded))[0]["id"]
    assert (await seeded.client.post(f"/v1/admin/data-requests/{rid}/reject", json={"note": ""}, headers=seeded.admin)).status_code == 422
    r = await seeded.client.post(f"/v1/admin/data-requests/{rid}/reject", json={"note": "We must keep this booking's records until tax filing"}, headers=seeded.admin)
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    mine = (await seeded.client.get("/v1/public/me/data-requests", headers=headers)).json()[0]
    assert mine["status"] == "rejected" and "tax filing" in mine["note"]
    assert (await seeded.client.post(f"/v1/admin/data-requests/{rid}/reject", json={"note": "again"}, headers=seeded.admin)).status_code == 409
    assert (await seeded.client.post(f"/v1/admin/data-requests/{rid}/complete", headers=seeded.admin)).status_code == 409
    assert (await ask(seeded, headers, "erasure")).status_code == 201  # a rejected request is no longer open: they may ask again


async def test_requests_are_private_to_their_tenant(client: httpx.AsyncClient, otp: Any) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await post_booking(a, await a.add_staff("Mei"), 10)
    await ask(a, await login(a, PHONE_A), "access")
    rid = (await requests(a))[0]["id"]
    assert await requests(b) == []
    assert (await b.client.post(f"/v1/admin/data-requests/{rid}/complete", headers=b.admin)).status_code == 404
    assert (await b.client.post(f"/v1/admin/data-requests/{rid}/reject", json={"note": "x"}, headers=b.admin)).status_code == 404
    assert (await b.client.get(f"/v1/admin/data-requests/{rid}/export", headers=b.admin)).status_code == 404


# --------------------------------------------------------------------- retention
async def age(s: Seed, bid: str, days: int, status: str) -> None:
    async with tenant_session(s.tenant_id) as db:
        await db.execute(
            text("UPDATE bookings SET start_at = now() - make_interval(days => :d) - interval '1 hour', end_at = now() - make_interval(days => :d), status = :st WHERE id = :b"),
            {"d": days, "st": status, "b": uuid.UUID(bid)},
        )


async def notes_present(s: Seed, bid: str) -> bool:
    return bool((await get(s, bid))["has_health_notes"])


async def test_health_notes_are_cleared_after_the_retention_period_and_only_then(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    old_done, recent_done, old_open, old_cancelled = [await with_notes(seeded, mei, h) for h in (9, 10, 11, 14)]
    await age(seeded, old_done, 40, "completed")
    await age(seeded, recent_done, 10, "completed")
    await age(seeded, old_open, 40, "confirmed")  # still open, however old: not a finished appointment
    await age(seeded, old_cancelled, 40, "cancelled")
    monkeypatch.setattr(get_settings(), "health_notes_retention_days", 0)
    assert (await sweep(seeded, now=soon())).tenants[0].notes_purged == 0  # retention off (the default): keep everything
    assert all([await notes_present(seeded, b) for b in (old_done, recent_done, old_open, old_cancelled)])

    monkeypatch.setattr(get_settings(), "health_notes_retention_days", 30)
    report = await sweep(seeded, now=soon())
    assert report.tenants[0].notes_purged == 2
    assert [await notes_present(seeded, b) for b in (old_done, recent_done, old_open, old_cancelled)] == [False, True, True, False]
    entry = next(e for e in await audit(seeded) if e["action"] == "retention.health_notes.purged")
    assert entry["actor_type"] == "system" and entry["actor_id"] is None and entry["detail"] == {"count": 2, "retention_days": 30}
    assert (await get(seeded, old_done))["status"] == "completed"  # the booking itself is untouched
    assert (await sweep(seeded, now=soon())).tenants[0].notes_purged == 0  # done once
