"""Findings of the security review, each with the test that proves it is closed: keys belong to their own business and can be listed and revoked, token
claims are required, list limits cannot break a query, answers carry safety headers, the API documentation is off in production, and a stranger's phone
or mailbox cannot be filled with sign-in messages."""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import Unauthorized
from app.core.security import decode_jwt, issue_jwt
from app.models import ApiKey, Site
from tests.conftest import Seed, make_seed
from tests.test_customer_cancel import PHONE, login, otp, request_code  # noqa: F401  (fixtures)
from tests.test_ratelimit import redis  # noqa: F401  (fixture)


async def site_of(seed: Seed) -> uuid.UUID:
    async with tenant_session(None) as s:
        return (await s.scalars(select(Site.id).where(Site.tenant_id == seed.tenant_id))).one()


async def new_key(seed: Seed, kind: str = "publishable", **extra: Any) -> httpx.Response:
    return await seed.client.post("/v1/auth/api-keys", json={"kind": kind, **extra}, headers=seed.admin)


async def keys(seed: Seed) -> list[dict[str, Any]]:
    r = await seed.client.get("/v1/auth/api-keys", headers=seed.admin)
    assert r.status_code == 200, r.text
    return list(r.json())


async def works(seed: Seed, key: str, header: str = "X-Site-Key") -> bool:
    r = await seed.client.get("/v1/public/services", headers={header: key})
    assert r.status_code in (200, 401), r.text
    return r.status_code == 200


# --------------------------------------------------------------------- a key belongs to its own business's sites
async def test_a_key_cannot_be_tied_to_another_businesss_site(client: httpx.AsyncClient) -> None:
    mine, theirs = await make_seed(client), await make_seed(client)
    before = await keys(mine)
    r = await new_key(mine, site_id=str(await site_of(theirs)))
    assert r.status_code == 404 and r.json()["error"] == "not_found"
    assert await keys(mine) == before  # nothing was created
    assert (await new_key(mine, site_id=str(uuid.uuid4()))).status_code == 404  # a site that does not exist is answered the same


async def test_a_key_can_be_tied_to_the_businesss_own_site(seeded: Seed) -> None:
    site = await site_of(seeded)
    r = await new_key(seeded, site_id=str(site))
    assert r.status_code == 201, r.text
    assert await works(seeded, r.json()["key"])
    assert (await new_key(seeded)).status_code == 201  # and a key without a site is still fine


# --------------------------------------------------------------------- listing keys
async def test_the_owner_sees_the_businesss_keys_but_never_a_key(seeded: Seed) -> None:
    created = (await new_key(seeded)).json()
    listed = await keys(seeded)
    assert len(listed) == 3 and {k["kind"] for k in listed} == {"publishable", "secret"}  # the pair made at set-up, and the new one
    mine = next(k for k in listed if k["id"] == created["id"])
    assert mine["prefix"] == created["key"][:8] and mine["revoked_at"] is None and mine["site_id"] == next(k["site_id"] for k in listed if k["id"] != created["id"]) and mine["created_at"]  # (a key made without naming a site belongs to the business's own)
    text = json.dumps(listed)
    assert created["key"] not in text and "key_hash" not in text and seeded.pk["X-Site-Key"] not in text and seeded.sk["X-API-Key"] not in text


async def test_each_business_sees_only_its_own_keys(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    assert not {k["id"] for k in await keys(a)} & {k["id"] for k in await keys(b)}


async def test_only_the_owner_manages_keys(seeded: Seed) -> None:
    created = (await new_key(seeded)).json()
    manager = await seeded.token("manager")
    c = seeded.client
    assert (await c.get("/v1/auth/api-keys", headers=manager)).status_code == 403
    assert (await c.delete(f"/v1/auth/api-keys/{created['id']}", headers=manager)).status_code == 403
    assert (await c.get("/v1/auth/api-keys")).status_code == 401
    assert (await c.delete(f"/v1/auth/api-keys/{created['id']}")).status_code == 401
    assert await works(seeded, created["key"])  # untouched


# --------------------------------------------------------------------- revoking keys
async def test_a_revoked_key_stops_working_at_once(seeded: Seed) -> None:
    created = (await new_key(seeded)).json()
    assert await works(seeded, created["key"])
    r = await seeded.client.delete(f"/v1/auth/api-keys/{created['id']}", headers=seeded.admin)
    assert r.status_code == 200 and r.json()["id"] == created["id"] and r.json()["revoked_at"] and "key" not in r.json()
    assert not await works(seeded, created["key"])
    assert next(k for k in await keys(seeded) if k["id"] == created["id"])["revoked_at"] == r.json()["revoked_at"]
    assert await works(seeded, seeded.pk["X-Site-Key"])  # the others are not affected


async def test_revoking_twice_changes_nothing_more(seeded: Seed) -> None:
    created = (await new_key(seeded)).json()
    first = (await seeded.client.delete(f"/v1/auth/api-keys/{created['id']}", headers=seeded.admin)).json()
    again = await seeded.client.delete(f"/v1/auth/api-keys/{created['id']}", headers=seeded.admin)
    assert again.status_code == 200 and again.json()["revoked_at"] == first["revoked_at"]


async def test_one_business_cannot_revoke_anothers_key(client: httpx.AsyncClient) -> None:
    mine, theirs = await make_seed(client), await make_seed(client)
    their_key = (await keys(theirs))[0]
    r = await client.delete(f"/v1/auth/api-keys/{their_key['id']}", headers=mine.admin)
    assert r.status_code == 404
    assert (await client.delete(f"/v1/auth/api-keys/{uuid.uuid4()}", headers=mine.admin)).status_code == 404  # no different from a key that does not exist
    assert all(k["revoked_at"] is None for k in await keys(theirs)) and await works(theirs, theirs.pk["X-Site-Key"])


async def test_a_revoked_secret_key_cannot_sign_in_an_administrator(seeded: Seed) -> None:
    second = (await new_key(seeded, "secret")).json()
    assert (await seeded.client.post("/v1/auth/token", json={"user_id": "x", "role": "owner"}, headers={"X-API-Key": second["key"]})).status_code == 200
    assert (await seeded.client.delete(f"/v1/auth/api-keys/{second['id']}", headers=seeded.admin)).status_code == 200
    r = await seeded.client.post("/v1/auth/token", json={"user_id": "x", "role": "owner"}, headers={"X-API-Key": second["key"]})
    assert r.status_code == 401


async def test_the_last_secret_key_cannot_be_revoked(seeded: Seed) -> None:
    (only,) = [k for k in await keys(seeded) if k["kind"] == "secret"]
    r = await seeded.client.delete(f"/v1/auth/api-keys/{only['id']}", headers=seeded.admin)
    assert r.status_code == 409 and "only secret key" in r.json()["detail"]
    assert await works(seeded, seeded.sk["X-API-Key"], "X-API-Key")
    second = (await new_key(seeded, "secret")).json()  # rotate: the new one first ...
    assert (await seeded.client.delete(f"/v1/auth/api-keys/{only['id']}", headers=seeded.admin)).status_code == 200  # ... then the old one goes
    assert (await seeded.client.delete(f"/v1/auth/api-keys/{second['id']}", headers=seeded.admin)).status_code == 409  # and the new one is now the last


async def test_a_publishable_key_may_be_revoked_even_if_it_is_the_last(seeded: Seed) -> None:
    for k in [k for k in await keys(seeded) if k["kind"] == "publishable"]:
        assert (await seeded.client.delete(f"/v1/auth/api-keys/{k['id']}", headers=seeded.admin)).status_code == 200
    assert not await works(seeded, seeded.pk["X-Site-Key"])


async def test_two_owners_cannot_revoke_the_last_two_secret_keys_at_once(seeded: Seed) -> None:
    second = (await new_key(seeded, "secret")).json()
    (first,) = [k for k in await keys(seeded) if k["kind"] == "secret" and k["id"] != second["id"]]
    codes = sorted(r.status_code for r in await asyncio.gather(
        seeded.client.delete(f"/v1/auth/api-keys/{first['id']}", headers=seeded.admin),
        seeded.client.delete(f"/v1/auth/api-keys/{second['id']}", headers=seeded.admin),
    ))
    assert codes == [200, 409]
    assert len([k for k in await keys(seeded) if k["kind"] == "secret" and k["revoked_at"] is None]) == 1


async def test_revoking_a_key_is_audited_without_the_key(seeded: Seed) -> None:
    created = (await new_key(seeded)).json()
    await seeded.client.delete(f"/v1/auth/api-keys/{created['id']}", headers=seeded.admin)
    r = await seeded.client.get("/v1/admin/audit-log", headers=seeded.admin)
    entry = r.json()[0]
    assert entry["action"] == "DELETE /v1/auth/api-keys/{key_id}" and entry["role"] == "owner" and entry["target_id"] == created["id"]
    assert created["key"] not in r.text


async def test_a_refused_revocation_is_not_audited(seeded: Seed) -> None:
    (only,) = [k for k in await keys(seeded) if k["kind"] == "secret"]
    await seeded.client.delete(f"/v1/auth/api-keys/{only['id']}", headers=seeded.admin)
    r = await seeded.client.get("/v1/admin/audit-log", headers=seeded.admin)
    assert not any(e["action"].startswith("DELETE /v1/auth/api-keys") for e in r.json())


# --------------------------------------------------------------------- tokens must say when they expire (and who they are)
def forged(**claims: Any) -> str:
    now = int(time.time())
    body: dict[str, Any] = {"sub": "u1", "tid": str(uuid.uuid4()), "role": "owner", "iat": now, "exp": now + 600}
    body.update(claims)
    return jwt.encode({k: v for k, v in body.items() if v is not None}, get_settings().jwt_secret, algorithm=get_settings().jwt_algorithm)


def test_a_complete_token_is_accepted() -> None:
    assert decode_jwt(forged())["role"] == "owner"
    assert decode_jwt(issue_jwt(tenant_id=uuid.uuid4(), subject="s", role="customer"))["role"] == "customer"


@pytest.mark.parametrize("missing", ["exp", "iat", "sub", "tid", "role"])
def test_a_signed_token_missing_a_claim_is_refused(missing: str) -> None:
    with pytest.raises(Unauthorized):
        decode_jwt(forged(**{missing: None}))


def test_expired_and_foreign_tokens_are_still_refused() -> None:
    with pytest.raises(Unauthorized):
        decode_jwt(forged(exp=int(time.time()) - 5))
    with pytest.raises(Unauthorized):
        decode_jwt(jwt.encode({"sub": "u", "tid": "t", "role": "owner", "iat": 1, "exp": int(time.time()) + 600}, "another-secret-another-secret-123456", algorithm="HS256"))


async def test_a_token_that_never_expires_does_not_open_the_admin_api(seeded: Seed) -> None:
    token = forged(tid=str(seeded.tenant_id), exp=None)
    r = await seeded.client.get("/v1/admin/business-settings", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    ok = forged(tid=str(seeded.tenant_id))
    assert (await seeded.client.get("/v1/admin/business-settings", headers={"Authorization": f"Bearer {ok}"})).status_code == 200


# --------------------------------------------------------------------- list limits cannot break a query
@pytest.mark.parametrize("query", ["limit=-1", "limit=0", "offset=-5", "limit=-3&offset=-3", "limit=100000000", "limit=1&offset=100000000"])
async def test_the_admin_booking_list_survives_any_limit_and_offset(seeded: Seed, query: str) -> None:
    r = await seeded.client.get(f"/v1/admin/bookings?{query}", headers=seeded.admin)
    assert r.status_code == 200 and isinstance(r.json(), list)


@pytest.mark.parametrize("query", ["limit=-1", "limit=0", "limit=100000000"])
async def test_a_customers_booking_list_survives_any_limit(seeded: Seed, otp: Any, query: str) -> None:
    headers = {**seeded.pk, **await login(seeded)}
    r = await seeded.client.get(f"/v1/public/me/bookings?{query}", headers=headers)
    assert r.status_code == 200 and isinstance(r.json(), list)


async def test_the_limit_still_limits(seeded: Seed, otp: Any) -> None:
    rid = await seeded.add_staff("Mei")
    for i in range(3):
        start = seeded.booking_body(__import__("tests.conftest", fromlist=["at"]).at(seeded.day, 9 + i), rid)
        assert (await seeded.client.post("/v1/public/bookings", json=start, headers=seeded.pk)).status_code == 201
    assert len((await seeded.client.get("/v1/admin/bookings?limit=2", headers=seeded.admin)).json()) == 2
    assert len((await seeded.client.get("/v1/admin/bookings?limit=1", headers=seeded.admin)).json()) == 1
    assert len((await seeded.client.get("/v1/admin/bookings?limit=5&offset=1", headers=seeded.admin)).json()) == 2


# --------------------------------------------------------------------- safety headers on every answer
SAFETY = {
    "x-content-type-options": "nosniff", "cache-control": "no-store", "referrer-policy": "no-referrer",
    "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
}


@pytest.mark.parametrize("path", ["/healthz", "/readyz", "/v1/countries", "/v1/admin/business-settings", "/v1/no/such/route"])
async def test_every_api_answer_carries_the_safety_headers(client: httpx.AsyncClient, path: str) -> None:
    r = await client.get(path)
    assert {k: r.headers.get(k) for k in SAFETY} == SAFETY


async def test_errors_carry_them_too(seeded: Seed) -> None:
    r = await seeded.client.post("/v1/public/auth/otp", json={"phone": "x"}, headers={"X-Site-Key": "pk_nope"})
    assert r.status_code == 401 and r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


async def test_the_documentation_page_keeps_its_own_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/docs")  # (the test environment serves the documentation)
    assert r.status_code == 200 and "content-security-policy" not in r.headers and "cache-control" not in r.headers


async def test_a_header_an_endpoint_sets_itself_is_kept() -> None:
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from app.core.observability import RequestContextMiddleware

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/x")
    async def x() -> JSONResponse:
        return JSONResponse({}, headers={"Cache-Control": "max-age=60"})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/x")
    assert r.headers["cache-control"] == "max-age=60" and r.headers["x-content-type-options"] == "nosniff"


async def test_strict_transport_security_only_where_tls_was_used(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "strict-transport-security" not in (await client.get("/healthz")).headers  # plain http: browsers would ignore it, and it would mislead
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as secure:
        r = await secure.get("/healthz")
    assert r.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


async def test_a_proxys_word_about_tls_is_believed_only_where_proxies_are_declared(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    said = {"X-Forwarded-Proto": "https"}
    monkeypatch.setattr(get_settings(), "rate_limit_trusted_proxies", 0)
    assert "strict-transport-security" not in (await client.get("/healthz", headers=said)).headers  # anyone can send that header
    monkeypatch.setattr(get_settings(), "rate_limit_trusted_proxies", 1)
    assert "strict-transport-security" in (await client.get("/healthz", headers=said)).headers
    assert "strict-transport-security" not in (await client.get("/healthz", headers={"X-Forwarded-Proto": "http"})).headers
    assert "strict-transport-security" in (await client.get("/healthz", headers={"X-Forwarded-Proto": "http, https"})).headers  # (the proxy nearest us spoke last)
    assert "strict-transport-security" in (await client.get("/healthz", headers={"X-Forwarded-Proto": "HTTPS"})).headers


# --------------------------------------------------------------------- the API documentation only where it is wanted
@pytest.mark.parametrize("environment, docs_enabled, served", [
    ("production", None, False), ("staging", None, False), ("development", None, True), ("test", None, True),
    ("production", True, True), ("development", False, False),
])
async def test_the_documentation_follows_the_environment_unless_told_otherwise(
    monkeypatch: pytest.MonkeyPatch, environment: str, docs_enabled: bool | None, served: bool,
) -> None:
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "environment", environment)
    monkeypatch.setattr(get_settings(), "docs_enabled", docs_enabled)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as c:
        statuses = {p: (await c.get(p)).status_code for p in ("/docs", "/redoc", "/openapi.json")}
        assert (await c.get("/healthz")).status_code == 200
    assert statuses == {p: (200 if served else 404) for p in statuses}


# --------------------------------------------------------------------- no filling a stranger's phone or mailbox
def cap(monkeypatch: pytest.MonkeyPatch, n: int) -> None:
    monkeypatch.setattr(get_settings(), "sign_in_requests_per_day", n)
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 0)


async def test_a_phone_gets_only_so_many_codes_a_day(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cap(monkeypatch, 3)
    assert [(await request_code(seeded)).status_code for _ in range(3)] == [202, 202, 202]
    r = await request_code(seeded)
    assert r.status_code == 429 and "today" in r.json()["detail"]
    assert (await request_code(seeded, "+60123456790")).status_code == 202  # another phone is unaffected
    (day_key,) = [k for k in await otp.keys("otp:*:day")][:1] or [None]
    assert day_key is not None and 0 < await otp.ttl(day_key) <= 86400


async def test_the_daily_count_is_per_business(client: httpx.AsyncClient, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cap(monkeypatch, 1)
    a, b = await make_seed(client), await make_seed(client)
    assert (await request_code(a)).status_code == 202 and (await request_code(a)).status_code == 429
    assert (await request_code(b)).status_code == 202  # (the same phone, asking another business)


async def test_zero_means_no_daily_cap(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cap(monkeypatch, 0)
    assert all([(await request_code(seeded)).status_code == 202 for _ in range(15)])
    assert await otp.keys("otp:*:day") == []


async def test_a_refused_request_sends_nothing(seeded: Seed, otp: Any, mail: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cap(monkeypatch, 1)
    await request_code(seeded)
    sent = len(mail.sent)
    assert (await request_code(seeded)).status_code == 429
    assert len(mail.sent) == sent


async def test_an_address_gets_only_so_many_sign_in_links_a_day(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cap(monkeypatch, 2)
    ask = lambda email: seeded.client.post("/v1/public/auth/magic-link", json={"email": email}, headers=seeded.pk)  # noqa: E731
    assert [(await ask("someone@example.com")).status_code for _ in range(2)] == [202, 202]
    r = await ask("someone@example.com")
    assert r.status_code == 429 and "today" in r.json()["detail"]
    assert (await ask("another@example.com")).status_code == 202
    assert (await ask("SomeOne@Example.com")).status_code == 429  # the same address, however it is written


async def test_a_code_that_could_not_be_sent_does_not_use_up_the_day(seeded: Seed, otp: Any, mail: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cap(monkeypatch, 2)
    mail.failing["whatsapp"] = RuntimeError("gateway down")
    assert [(await request_code(seeded)).status_code for _ in range(5)] == [503] * 5  # (more tries than the cap: none of them was sent)
    assert await otp.keys("otp:*:day") == []
    mail.failing.clear()
    assert [(await request_code(seeded)).status_code for _ in range(3)] == [202, 202, 429]


async def test_the_daily_cap_helper(redis: Any) -> None:
    from app.core import ratelimit

    assert [await ratelimit.daily_cap("k", 2) for _ in range(4)] == [True, True, False, False]
    assert await ratelimit.daily_cap("other", 2) is True
    assert all([await ratelimit.daily_cap("free", 0) for _ in range(5)]) and await redis.get("free") is None


async def test_a_refund_gives_back_one_use_and_leaves_nothing_behind(redis: Any) -> None:
    from app.core import ratelimit

    await ratelimit.refund_daily("never-used")  # nothing to give back
    assert await redis.keys("*") == []
    for _ in range(3):
        await ratelimit.daily_cap("k", 3)
    await ratelimit.refund_daily("k")
    assert await redis.get("k") == "2" and await ratelimit.daily_cap("k", 3) is True and await ratelimit.daily_cap("k", 3) is False
    for _ in range(3):  # (the count is 4 now: the refused request was counted too)
        await ratelimit.refund_daily("k")
    assert await redis.get("k") == "1"
    await ratelimit.refund_daily("k")
    assert await redis.keys("k") == []


# --------------------------------------------------------------------- deployment: the compose file does not leave the environment to chance
def test_compose_states_the_environment_for_every_service_that_runs_the_application() -> None:
    text = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    blocks = re.split(r"\n  (?=[a-z]+:\n)", text)
    for service in ("migrate", "api", "worker"):
        (block,) = [b for b in blocks if b.startswith(f"{service}:")]
        assert re.search(r"^\s+ENVIRONMENT: production\b", block, re.M), service
    assert "ENVIRONMENT: development" not in text


# --------------------------------------------------------------------- every route is guarded (a guard against a route added without its dependency)
#: What anyone may call without credentials, and what it may answer (sign-up refuses with 403 while it is switched off, or 422 for the empty body sent here).
OPEN = {("GET", "/v1/countries"): (200,), ("GET", "/v1/signup/config"): (200,), ("GET", "/v1/captcha"): (200,), ("GET", "/v1/calendar/oauth/callback"): (400,), ("POST", "/v1/owner/login"): (422,), ("POST", "/v1/owner/login/verify"): (422,), ("POST", "/v1/signup"): (403, 422), ("POST", "/v1/signup/verify"): (403, 422)}


def operations() -> list[tuple[str, str]]:
    from app.main import app

    found = []
    for path, item in app.openapi()["paths"].items():
        for method in item:
            if method in ("get", "post", "put", "patch", "delete"):
                found.append((method.upper(), re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)))
    return sorted(found)


def test_the_route_list_is_found_and_covers_every_area() -> None:
    paths = [p for _, p in operations()]
    assert len(paths) > 100
    for prefix in ("/v1/admin/", "/v1/public/", "/v1/platform/", "/v1/auth/", "/v1/sync/", "/v1/signup"):
        assert any(p.startswith(prefix) for p in paths), prefix


async def test_nothing_answers_a_caller_with_no_credentials_except_what_is_meant_to_be_open(client: httpx.AsyncClient) -> None:
    wrong: list[str] = []
    for method, path in operations():
        status = (await client.request(method, path)).status_code
        expected = OPEN.get((method, path.split("?")[0]), (401,))
        if status not in expected:
            wrong.append(f"{method} {path} -> {status} (expected {expected})")
    assert wrong == []


async def test_a_publishable_key_alone_opens_nothing_administrative(seeded: Seed) -> None:
    wrong: list[str] = []
    for method, path in operations():
        if path.startswith(("/v1/admin/", "/v1/platform/", "/v1/auth/")) and path != "/v1/auth/token":
            status = (await seeded.client.request(method, path, headers=seeded.pk)).status_code
            if status != 401:
                wrong.append(f"{method} {path} -> {status}")
    assert wrong == []


async def test_a_customers_token_opens_nothing_administrative(seeded: Seed, otp: Any) -> None:
    headers = {**seeded.pk, **await login(seeded)}
    wrong: list[str] = []
    for method, path in operations():
        if path.startswith(("/v1/admin/", "/v1/platform/")) or path in ("/v1/auth/api-keys", f"/v1/auth/api-keys/{path.rsplit('/', 1)[-1]}"):
            status = (await seeded.client.request(method, path, headers=headers)).status_code
            if status not in (401, 403):
                wrong.append(f"{method} {path} -> {status}")
    assert wrong == []


async def test_an_administrators_token_does_not_open_the_operators_routes(seeded: Seed) -> None:
    wrong: list[str] = []
    for method, path in operations():
        if path.startswith("/v1/platform/") or (method, path) == ("POST", "/v1/auth/tenants"):
            for headers in (seeded.admin, seeded.sk, seeded.pk):
                status = (await seeded.client.request(method, path, headers=headers)).status_code
                if status != 401:
                    wrong.append(f"{method} {path} {sorted(headers)} -> {status}")
    assert wrong == []


async def test_the_operators_token_alone_opens_nothing_of_a_business(seeded: Seed) -> None:
    wrong: list[str] = []
    for method, path in operations():
        if path.startswith(("/v1/admin/", "/v1/public/", "/v1/sync/")):
            status = (await seeded.client.request(method, path, headers={"X-Platform-Token": get_settings().platform_admin_token})).status_code
            if status != 401:
                wrong.append(f"{method} {path} -> {status}")
    assert wrong == []


# --------------------------------------------------------------------- gaps found by mutation testing
async def test_every_kind_of_request_names_the_business_for_the_lines_it_writes(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import logs

    bound: list[str] = []
    monkeypatch.setattr("app.api.deps.bind_tenant", lambda tid: bound.append(str(tid)))
    assert (await seeded.client.get("/v1/public/services", headers=seeded.pk)).status_code == 200  # a site key
    assert (await seeded.client.get("/v1/admin/business-settings", headers=seeded.admin)).status_code == 200  # an administrator's token
    assert bound == [str(seeded.tenant_id)] * 3  # (an administrator's request passes two role checks, the router's and the endpoint's)
    assert logs.tenant_id_var.get() is None


async def test_two_keys_for_one_site_are_listed_with_their_site(seeded: Seed) -> None:
    site = await site_of(seeded)
    made = (await new_key(seeded, site_id=str(site))).json()
    assert next(k for k in await keys(seeded) if k["id"] == made["id"])["site_id"] == str(site)
    assert next(k for k in await keys(seeded) if k["id"] != made["id"])["site_id"] == str(site)  # (the pair made at set-up are the site's too)


async def test_the_day_count_survives_for_a_day_and_not_longer(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    cap(monkeypatch, 5)
    await request_code(seeded)
    (day,) = await otp.keys("otp:*:day")
    assert 86000 < await otp.ttl(day) <= 86400


class Statements:
    """The SQL the application sends while a block runs, with its parameters."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, Any]] = []

    def __enter__(self) -> "Statements":
        from sqlalchemy import event

        from app.core.database import get_engine

        self.engine = get_engine().sync_engine
        self.hook = lambda conn, cursor, statement, parameters, context, many: self.seen.append((statement, parameters))
        event.listen(self.engine, "before_cursor_execute", self.hook)
        return self

    def __exit__(self, *exc: object) -> None:
        from sqlalchemy import event

        event.remove(self.engine, "before_cursor_execute", self.hook)

    def limits(self, table: str) -> list[int]:
        return [int(p) for s, params in self.seen if f"FROM {table}" in s and " LIMIT " in s for p in params if isinstance(p, int) and not isinstance(p, bool)]


async def test_no_list_returns_more_rows_than_its_ceiling(seeded: Seed, otp: Any) -> None:
    headers = {**seeded.pk, **await login(seeded)}
    with Statements() as sql:
        assert (await seeded.client.get("/v1/admin/bookings?limit=100000000", headers=seeded.admin)).status_code == 200
        assert (await seeded.client.get("/v1/public/me/bookings?limit=100000000", headers=headers)).status_code == 200
    asked = sql.limits("bookings")
    assert 500 in asked and 200 in asked and max(asked) == 500 and 100000000 not in asked


async def test_a_link_that_could_not_be_sent_does_not_use_up_the_day(seeded: Seed, otp: Any, mail: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_customer_identity import booked_with_email

    await booked_with_email(seeded)
    cap(monkeypatch, 2)
    mail.failing["email"] = RuntimeError("smtp down")
    from tests.test_customer_identity import EMAIL

    ask = lambda: seeded.client.post("/v1/public/auth/magic-link", json={"email": EMAIL}, headers=seeded.pk)  # noqa: E731
    assert [(await ask()).status_code for _ in range(4)] == [202] * 4  # (the answer never says whether a send worked)
    assert await otp.keys("magic:*:day") == []
    mail.failing.clear()
    assert [(await ask()).status_code for _ in range(3)] == [202, 202, 429]
