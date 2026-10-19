"""Administrative users: recorded at token exchange, listed, and disabled (immediately, including tokens already issued)."""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from sqlalchemy import text

from app.core.database import tenant_session
from app.services import users as users_service
from tests.conftest import Seed, make_seed
from tests.test_compliance import audit


async def exchange(s: Seed, user: str, role: str = "manager", **extra: Any) -> httpx.Response:
    return await s.client.post("/v1/auth/token", json={"user_id": user, "role": role, **extra}, headers=s.sk)


async def login_as(s: Seed, user: str, role: str = "manager", **extra: Any) -> dict[str, str]:
    r = await exchange(s, user, role, **extra)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def users(s: Seed, headers: dict[str, str] | None = None, **params: Any) -> list[dict[str, Any]]:
    r = await s.client.get("/v1/admin/users", params=params, headers=headers or s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def by_external(rows: list[dict[str, Any]], external_id: str) -> dict[str, Any]:
    return next(u for u in rows if u["external_id"] == external_id)


async def disable(s: Seed, target: str, headers: dict[str, str] | None = None, status: str = "disabled") -> httpx.Response:
    return await s.client.patch(f"/v1/admin/users/{target}", json={"status": status}, headers=headers or s.admin)


# --------------------------------------------------------------------- recording
async def test_a_token_exchange_records_the_user_and_each_role_they_have_been_given(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    await login_as(seeded, "wp-7", "manager", display_name="Mei Ling")
    first = by_external(await users(seeded), "wp-7")
    assert (first["display_name"], first["status"], [r["role"] for r in first["roles"]]) == ("Mei Ling", "active", ["manager"])
    assert first["last_seen_at"] is not None
    await login_as(seeded, "wp-7", "staff", resource_id=str(mei))  # the same person, now acting as staff (name not repeated)
    again = by_external(await users(seeded), "wp-7")
    assert again["id"] == first["id"] and again["display_name"] == "Mei Ling"  # the name is kept when a login leaves it out
    assert {(r["role"], r["resource_id"]) for r in again["roles"]} == {("manager", None), ("staff", str(mei))}
    assert again["last_seen_at"] >= first["last_seen_at"]
    await login_as(seeded, "wp-7", "staff", display_name="Mei L.")
    latest = by_external(await users(seeded), "wp-7")
    assert latest["display_name"] == "Mei L." and {(r["role"], r["resource_id"]) for r in latest["roles"]} == {("manager", None), ("staff", None)}


async def test_the_users_list_is_for_managers_and_can_be_filtered(seeded: Seed) -> None:
    await login_as(seeded, "wp-8", "staff")
    assert (await seeded.client.get("/v1/admin/users", headers=await login_as(seeded, "wp-9", "staff"))).status_code == 403
    assert (await seeded.client.get("/v1/admin/users", headers=await login_as(seeded, "wp-10", "manager"))).status_code == 200
    assert (await seeded.client.get("/v1/admin/users")).status_code == 401
    target = by_external(await users(seeded), "wp-8")["id"]
    assert (await disable(seeded, target)).status_code == 200
    assert [u["external_id"] for u in await users(seeded, status="disabled")] == ["wp-8"]
    assert "wp-8" not in [u["external_id"] for u in await users(seeded, status="active")]
    assert (await seeded.client.get("/v1/admin/users", params={"status": "banned"}, headers=seeded.admin)).status_code == 422


async def test_the_same_wordpress_user_id_at_two_businesses_is_two_users(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await login_as(a, "wp-42", "manager")
    await login_as(b, "wp-42", "manager")
    ua, ub = by_external(await users(a), "wp-42"), by_external(await users(b), "wp-42")
    assert ua["id"] != ub["id"]
    assert (await disable(a, ua["id"])).status_code == 200
    assert by_external(await users(b), "wp-42")["status"] == "active"  # disabling one does not touch the other
    assert (await exchange(b, "wp-42", "manager")).status_code == 200 and (await exchange(a, "wp-42", "manager")).status_code == 403
    assert (await disable(b, ua["id"])).status_code == 404  # and one business cannot reach the other's user


async def test_first_logins_arriving_together_create_one_user_and_no_errors(seeded: Seed) -> None:
    results = await asyncio.gather(*[exchange(seeded, "wp-new", "staff") for _ in range(8)])
    assert [r.status_code for r in results] == [200] * 8
    assert [u["external_id"] for u in await users(seeded)].count("wp-new") == 1


async def test_tokens_for_users_seen_before_this_feature_keep_working(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")  # the admin token's user (wp-1) has been recorded
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("DELETE FROM users"))  # as if it never had been
    assert (await seeded.client.get("/v1/admin/resources", headers=seeded.admin)).status_code == 200
    assert mei is not None and (await users(seeded)) == []


# --------------------------------------------------------------------- disabling
async def test_a_disabled_user_gets_no_new_token_and_loses_the_ones_they_hold_at_once(seeded: Seed) -> None:
    held = await login_as(seeded, "wp-7", "manager")
    assert (await seeded.client.get("/v1/admin/resources", headers=held)).status_code == 200
    uid = by_external(await users(seeded), "wp-7")["id"]
    r = await disable(seeded, uid)
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    refused = await exchange(seeded, "wp-7", "manager")
    assert refused.status_code == 403 and "disabled" in refused.json()["detail"]
    cut_off = await seeded.client.get("/v1/admin/resources", headers=held)  # the token is still within its lifetime
    assert cut_off.status_code == 403 and "disabled" in cut_off.json()["detail"]
    assert (await seeded.client.get("/v1/admin/resources", headers=seeded.admin)).status_code == 200  # everyone else is unaffected
    assert (await disable(seeded, uid, status="active")).json()["status"] == "active"
    assert (await seeded.client.get("/v1/admin/resources", headers=held)).status_code == 200  # the same token works again
    assert (await exchange(seeded, "wp-7", "manager")).status_code == 200


async def test_only_an_owner_can_change_a_users_status_and_the_change_is_audited(seeded: Seed) -> None:
    await login_as(seeded, "wp-7", "staff")
    uid = by_external(await users(seeded), "wp-7")["id"]
    for role in ("manager", "staff"):
        assert (await disable(seeded, uid, await login_as(seeded, f"wp-{role}", role))).status_code == 403
    assert (await disable(seeded, str(uuid.uuid4()))).status_code == 404
    assert (await seeded.client.patch(f"/v1/admin/users/{uid}", json={"status": "banned"}, headers=seeded.admin)).status_code == 422
    assert (await seeded.client.patch(f"/v1/admin/users/{uid}", json={"status": "disabled", "role": "owner"}, headers=seeded.admin)).status_code == 422
    assert by_external(await users(seeded), "wp-7")["status"] == "active"
    await disable(seeded, uid)
    entry = next(e for e in await audit(seeded) if e["action"] == "PATCH /v1/admin/users/{user_id}")
    assert (entry["target_type"], entry["target_id"], entry["actor_id"]) == ("user", uid, "wp-1")


async def test_an_owner_cannot_disable_themselves(seeded: Seed) -> None:
    me = by_external(await users(seeded), "wp-1")  # the admin token's user, recorded at exchange
    r = await disable(seeded, me["id"])
    assert r.status_code == 422 and "own account" in r.json()["detail"]
    assert by_external(await users(seeded), "wp-1")["status"] == "active"


async def test_the_last_active_owner_cannot_be_disabled(client: httpx.AsyncClient) -> None:
    s = await make_seed(client)
    boss = by_external(await users(s), "wp-1")["id"]
    other = await login_as(s, "wp-2", "owner")
    async with tenant_session(s.tenant_id) as db:  # wp-2's token says owner, but no owner role is recorded for them
        await db.execute(text("DELETE FROM user_roles WHERE user_id = (SELECT id FROM users WHERE external_id = 'wp-2')"))
    blocked = await disable(s, boss, other)
    assert blocked.status_code == 409 and "active owner" in blocked.json()["detail"]
    assert by_external(await users(s), "wp-1")["status"] == "active"
    await login_as(s, "wp-2", "owner")  # now wp-2 is recorded as an owner too, so wp-1 is not the last one
    assert (await disable(s, boss, other)).json()["status"] == "disabled"
    assert (await s.client.get("/v1/admin/resources", headers=s.admin)).status_code == 403  # and wp-1 is out
    assert (await s.client.get("/v1/admin/resources", headers=other)).status_code == 200  # wp-2 is still in charge


async def test_two_owners_disabling_each_other_at_once_cannot_leave_nobody_in_charge(client: httpx.AsyncClient, monkeypatch: Any) -> None:
    s = await make_seed(client)
    b_headers = await login_as(s, "wp-2", "owner")
    rows = await users(s)
    a, b = by_external(rows, "wp-1")["id"], by_external(rows, "wp-2")["id"]
    arrived, both = 0, asyncio.Event()
    real = users_service.set_status

    async def together(*args: Any, **kwargs: Any) -> Any:
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=10)  # let both requests start deciding at the same moment
        return await real(*args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(users_service, "set_status", together)
        first, second = await asyncio.gather(disable(s, b, s.admin), disable(s, a, b_headers))
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    survivor = s.admin if first.status_code == 200 else b_headers  # whichever request won left its own owner in place
    assert sum(1 for u in await users(s, survivor) if u["status"] == "active") == 1  # exactly one owner is left
    assert (await s.client.get("/v1/admin/resources", headers=survivor)).status_code == 200
