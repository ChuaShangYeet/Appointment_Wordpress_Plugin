"""The owner signs in by email, and can get new keys if the old ones are lost; the operator can give a business an owner address (app.services.owner_login)."""
from __future__ import annotations

import re
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.database import tenant_session
from app.core.ratelimit import classify
from app.core.security import decode_jwt
from app.services import delivery
from tests.conftest import PLATFORM, Recorder, Seed, make_seed
from tests.test_ratelimit import clock, limited, redis  # noqa: F401  (fixtures)
from tests.test_signup import open_signup, sign_up  # noqa: F401  (fixtures and helper)

LINK = "https://site.example/owner?t={token}"


@pytest.fixture
def owner(open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Sign-up on (to make businesses with a confirmed owner address), a recording email provider, an in-memory Redis, the pause between requests off."""
    settings = get_settings()
    monkeypatch.setattr(settings, "owner_login_url", LINK)
    monkeypatch.setattr(settings, "owner_login_cooldown_seconds", 0)
    return open_signup


async def new_business(client: httpx.AsyncClient, mail: Recorder) -> tuple[str, Seed]:
    """A business that signed itself up: its owner's address, and a Seed with its first keys (the admin token is not set)."""
    body, answer = await sign_up(client, mail)
    mail.sent.clear()
    return body["owner_email"], Seed(client, answer)


async def ask(client: httpx.AsyncClient, email: str, **extra: Any) -> httpx.Response:
    return await client.post("/v1/owner/login", json={"email": email, **extra})


async def use(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/v1/owner/login/verify", json={"token": token})


def token_of(mail: Recorder) -> str:
    sent = [m for m in mail.sent if (m.subject or "").startswith("Your sign-in link")]
    assert sent, "no sign-in email was sent"
    found = re.search(r"\?t=([A-Za-z0-9_\-]+)", sent[-1].body)
    assert found, sent[-1].body
    return found.group(1)


def sign_in_mails(mail: Recorder) -> list[Any]:
    return [m for m in mail.sent if (m.subject or "").startswith("Your sign-in link")]


async def signed_in(client: httpx.AsyncClient, mail: Recorder, email: str) -> dict[str, str]:
    assert (await ask(client, email)).status_code == 202
    r = await use(client, token_of(mail))
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# --------------------------------------------------------------------- asking for the link
async def test_the_owners_address_is_sent_a_link(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    r = await ask(client, email)
    assert r.status_code == 202 and r.json() == {"status": "sent"}
    (mail,) = sign_in_mails(owner)
    assert mail.channel == "email" and mail.recipient == email and mail.subject == "Your sign-in link for JustSimple"
    assert "https://site.example/owner?t=" in mail.body and "works once" in mail.body and "15 minutes" in mail.body and "if you did not ask" in mail.body.lower()
    assert s.tenant_id  # (the business exists)


async def test_the_business_is_named_in_the_email(client: httpx.AsyncClient, owner: Recorder) -> None:
    body, _ = await sign_up(client, owner, name="Glow Studio\r\nBcc: evil@example.com")
    owner.sent.clear()
    await ask(client, body["owner_email"])
    (mail,) = sign_in_mails(owner)
    assert "Glow Studio" in mail.body and "\r" not in mail.body.split("\n\n")[1]  # (a name cannot add lines)


async def test_an_unknown_address_gets_the_same_answer_and_no_email(client: httpx.AsyncClient, owner: Recorder) -> None:
    known, _ = await new_business(client, owner)
    a, b = await ask(client, known), await ask(client, "nobody@example.com")
    assert a.status_code == b.status_code == 202 and a.json() == b.json()
    assert [m.recipient for m in sign_in_mails(owner)] == [known]


@pytest.mark.parametrize("spelling", [lambda e: e.upper(), lambda e: f"  {e}  ", lambda e: e.title()])
async def test_the_address_is_matched_however_it_is_written(client: httpx.AsyncClient, owner: Recorder, spelling: Any) -> None:
    email, _ = await new_business(client, owner)
    assert (await ask(client, spelling(email).strip())).status_code == 202
    assert len(sign_in_mails(owner)) == 1


async def test_only_a_hash_of_the_token_is_kept_for_fifteen_minutes(client: httpx.AsyncClient, owner: Recorder, redis: Any) -> None:  # noqa: F811
    email, s = await new_business(client, owner)
    await ask(client, email)
    token = token_of(owner)
    (key,) = await redis.keys("owner:token:*")
    assert token not in key and await redis.get(key) == str(s.tenant_id)
    assert 800 < await redis.ttl(key) <= 900


async def test_a_newer_link_cancels_the_older(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, _ = await new_business(client, owner)
    await ask(client, email)
    first = token_of(owner)
    await ask(client, email)
    second = token_of(owner)
    assert first != second
    assert (await use(client, first)).status_code == 401
    assert (await use(client, second)).status_code == 200


async def test_asking_again_too_soon_is_refused_for_every_address_alike(client: httpx.AsyncClient, owner: Recorder, monkeypatch: pytest.MonkeyPatch, redis: Any) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "owner_login_cooldown_seconds", 60)
    email, _ = await new_business(client, owner)
    assert (await ask(client, email)).status_code == 202
    again = await ask(client, email)
    assert again.status_code == 429 and again.json()["error"] == "too_many_requests"
    assert (await ask(client, "nobody@example.com")).status_code == 202 and (await ask(client, "nobody@example.com")).status_code == 429  # (unknown ones too)
    assert len(sign_in_mails(owner)) == 1
    (cooldown,) = [k for k in await redis.keys("owner:email:*:cooldown") if await redis.ttl(k) > 0][:1]
    assert 0 < await redis.ttl(cooldown) <= 60


async def test_an_address_gets_only_so_many_links_a_day(client: httpx.AsyncClient, owner: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "sign_in_requests_per_day", 2)
    email, _ = await new_business(client, owner)
    assert [(await ask(client, email)).status_code for _ in range(2)] == [202, 202]
    r = await ask(client, email)
    assert r.status_code == 429 and "today" in r.json()["detail"]
    assert [(await ask(client, "nobody@example.com")).status_code for _ in range(3)] == [202, 202, 429]  # an unknown address reaches the cap the same way
    assert len(sign_in_mails(owner)) == 2


async def test_a_link_that_could_not_be_sent_leaves_nothing_behind_and_looks_the_same(client: httpx.AsyncClient, owner: Recorder, redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "sign_in_requests_per_day", 1)
    monkeypatch.setattr(get_settings(), "owner_login_cooldown_seconds", 60)  # (so a pause that was not given back would show)
    email, _ = await new_business(client, owner)
    owner.failing["email"] = RuntimeError("smtp down")
    r = await ask(client, email)
    assert r.status_code == 202 and r.json() == {"status": "sent"}  # (the answer does not say whether it went)
    assert await redis.keys("owner:*") == []  # no token, no cool-down, and the day's count was given back
    owner.failing.clear()
    assert (await ask(client, email)).status_code == 202 and len(sign_in_mails(owner)) == 1


async def test_a_suspended_business_is_sent_nothing(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    async with tenant_session(None) as db:
        await db.execute(text("UPDATE tenants SET status = 'suspended' WHERE id = :t"), {"t": s.tenant_id})
    assert (await ask(client, email)).status_code == 202 and sign_in_mails(owner) == []


@pytest.mark.parametrize("body", [{}, {"email": "not-an-address"}, {"email": ""}, {"email": "a@b.com", "extra": 1}, {"email": 5}])
async def test_a_bad_request_is_refused(client: httpx.AsyncClient, owner: Recorder, body: dict[str, Any]) -> None:
    assert (await client.post("/v1/owner/login", json=body)).status_code == 422 and owner.sent == []


async def test_without_a_page_the_email_carries_the_bare_token(client: httpx.AsyncClient, owner: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    email, _ = await new_business(client, owner)
    monkeypatch.setattr(get_settings(), "owner_login_url", "")
    await ask(client, email)
    (mail,) = sign_in_mails(owner)
    assert "Your sign-in token: " in mail.body and "http" not in mail.body


# --------------------------------------------------------------------- using the link
async def test_the_link_gives_an_owner_token_for_the_business(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    await ask(client, email)
    r = await use(client, token_of(owner))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"access_token", "token_type", "expires_in", "tenant_id"} and body["token_type"] == "bearer" and body["expires_in"] == get_settings().jwt_ttl_seconds
    assert body["tenant_id"] == str(s.tenant_id)
    claims = decode_jwt(body["access_token"])
    assert claims["role"] == "owner" and claims["tid"] == str(s.tenant_id) and claims["sub"] == f"owner:{email}" and claims["exp"] - claims["iat"] == get_settings().jwt_ttl_seconds


async def test_the_token_works_for_everything_an_owner_does(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    assert (await client.get("/v1/admin/business-settings", headers=headers)).status_code == 200
    assert (await client.get("/v1/auth/api-keys", headers=headers)).status_code == 200  # owner-only
    assert (await client.get("/v1/admin/audit-log", headers=headers)).status_code == 200
    assert (await client.get("/v1/admin/plan", headers=headers)).json()["plan"]["code"] == "free"  # (and it is this business's, not another's)
    assert s.tenant_id


async def test_the_link_works_once(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, _ = await new_business(client, owner)
    await ask(client, email)
    token = token_of(owner)
    assert (await use(client, token)).status_code == 200
    again = await use(client, token)
    assert again.status_code == 401 and again.json()["error"] == "unauthorized"


async def test_an_expired_or_made_up_link_is_refused(client: httpx.AsyncClient, owner: Recorder, redis: Any) -> None:  # noqa: F811
    email, _ = await new_business(client, owner)
    await ask(client, email)
    token = token_of(owner)
    (key,) = await redis.keys("owner:token:*")
    await redis.delete(key)  # (as if its ten minutes had run out)
    assert (await use(client, token)).status_code == 401
    assert (await use(client, "x" * 43)).status_code == 401


@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": "short"}, {"token": "x" * 201}, {"token": "x" * 30, "extra": 1}])
async def test_a_bad_token_request_is_refused(client: httpx.AsyncClient, owner: Recorder, body: dict[str, Any]) -> None:
    assert (await client.post("/v1/owner/login/verify", json=body)).status_code == 422


async def test_the_link_is_for_posting_not_for_opening(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, _ = await new_business(client, owner)
    await ask(client, email)
    token = token_of(owner)
    assert (await client.get("/v1/owner/login/verify", params={"token": token})).status_code == 405
    assert (await use(client, token)).status_code == 200  # (so opening it, as a mail scanner does, did not use it up)


async def test_a_business_suspended_after_the_link_was_sent_cannot_be_entered(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    await ask(client, email)
    async with tenant_session(None) as db:
        await db.execute(text("UPDATE tenants SET status = 'suspended' WHERE id = :t"), {"t": s.tenant_id})
    assert (await use(client, token_of(owner))).status_code == 401


async def test_a_link_for_an_address_that_was_removed_is_refused(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    await ask(client, email)
    r = await client.put(f"/v1/platform/tenants/{s.tenant_id}/owner-email", json={"email": None}, headers=PLATFORM)
    assert r.status_code == 200
    assert (await use(client, token_of(owner))).status_code == 401


async def test_the_owner_appears_among_the_users_and_in_the_audit_log(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, _ = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    users = (await client.get("/v1/admin/users", headers=headers)).json()
    (me,) = [u for u in users if u["external_id"] == f"owner:{email}"]
    assert me["status"] == "active" and [r["role"] for r in me["roles"]] == ["owner"]
    assert (await client.put("/v1/admin/business-profile", json={"business_name": "Glow"}, headers=headers)).status_code == 200
    entry = (await client.get("/v1/admin/audit-log", headers=headers)).json()[0]
    assert entry["action"] == "PUT /v1/admin/business-profile" and entry["actor_id"] == f"owner:{email}" and entry["role"] == "owner"


async def test_an_owner_the_business_has_disabled_cannot_sign_in_and_loses_the_token_they_hold(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    (me,) = [u for u in (await client.get("/v1/admin/users", headers=headers)).json() if u["external_id"] == f"owner:{email}"]
    assert (await client.patch(f"/v1/admin/users/{me['id']}", json={"status": "disabled"}, headers=headers)).status_code == 422  # (not one's own account)
    other_owner = await s.token("owner")  # (another owner, through the WordPress server's secret key)
    assert (await client.patch(f"/v1/admin/users/{me['id']}", json={"status": "disabled"}, headers=other_owner)).status_code == 200
    assert (await client.get("/v1/admin/business-settings", headers=headers)).status_code == 403
    await ask(client, email)
    assert (await use(client, token_of(owner))).status_code == 403


async def test_one_business_cannot_sign_in_as_another(client: httpx.AsyncClient, owner: Recorder) -> None:
    email_a, a = await new_business(client, owner)
    email_b, b = await new_business(client, owner)
    headers = await signed_in(client, owner, email_a)
    assert decode_jwt(headers["Authorization"][7:])["tid"] == str(a.tenant_id) != str(b.tenant_id)
    assert email_b != email_a


# --------------------------------------------------------------------- lost keys
async def test_an_owner_who_lost_the_keys_gets_a_new_pair_for_the_same_site(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    before = (await client.get("/v1/auth/api-keys", headers=headers)).json()
    r = await client.post("/v1/auth/api-keys/recover", json={}, headers=headers)
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"site_id", "publishable_key", "secret_key", "revoked"} and body["revoked"] == 0 and body["site_id"] == before[0]["site_id"]
    assert body["publishable_key"].startswith("pk_") and body["secret_key"].startswith("sk_")
    assert (await client.get("/v1/public/services", headers={"X-Site-Key": body["publishable_key"]})).status_code == 200
    token = await client.post("/v1/auth/token", json={"user_id": "wp-1", "role": "owner"}, headers={"X-API-Key": body["secret_key"]})
    assert token.status_code == 200 and decode_jwt(token.json()["access_token"])["tid"] == str(s.tenant_id)
    assert (await client.get("/v1/public/services", headers=s.pk)).status_code == 200  # the old keys still work
    after = (await client.get("/v1/auth/api-keys", headers=headers)).json()
    assert len(after) == len(before) + 2 and {k["site_id"] for k in after} == {body["site_id"]}


async def test_a_key_that_may_have_leaked_can_be_replaced_in_one_step(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    r = await client.post("/v1/auth/api-keys/recover", json={"revoke_existing": True}, headers=headers)
    assert r.status_code == 201 and r.json()["revoked"] == 2
    new = r.json()
    assert (await client.get("/v1/public/services", headers=s.pk)).status_code == 401  # the old publishable key
    assert (await client.post("/v1/auth/token", json={"user_id": "x", "role": "owner"}, headers=s.sk)).status_code == 401  # and the old secret key
    assert (await client.get("/v1/public/services", headers={"X-Site-Key": new["publishable_key"]})).status_code == 200
    assert (await client.post("/v1/auth/token", json={"user_id": "x", "role": "owner"}, headers={"X-API-Key": new["secret_key"]})).status_code == 200
    states = {k["prefix"]: k["revoked_at"] is not None for k in (await client.get("/v1/auth/api-keys", headers=headers)).json()}
    assert sorted(states.values()) == [False, False, True, True]


async def test_recovery_works_when_the_only_keys_are_gone(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    for key in (await client.get("/v1/auth/api-keys", headers=headers)).json():
        await client.delete(f"/v1/auth/api-keys/{key['id']}", headers=headers)  # (the last secret key cannot be revoked by hand: whatever is left is left)
    r = await client.post("/v1/auth/api-keys/recover", json={"revoke_existing": True}, headers=headers)
    assert r.status_code == 201
    assert (await client.post("/v1/auth/token", json={"user_id": "x", "role": "owner"}, headers={"X-API-Key": r.json()["secret_key"]})).status_code == 200


async def test_recovery_leaves_other_businesses_alone(client: httpx.AsyncClient, owner: Recorder) -> None:
    email_a, a = await new_business(client, owner)
    _, b = await new_business(client, owner)
    headers = await signed_in(client, owner, email_a)
    await client.post("/v1/auth/api-keys/recover", json={"revoke_existing": True}, headers=headers)
    assert (await client.get("/v1/public/services", headers=b.pk)).status_code == 200
    assert (await client.post("/v1/auth/token", json={"user_id": "x", "role": "owner"}, headers=b.sk)).status_code == 200
    assert (await client.get("/v1/public/services", headers=a.pk)).status_code == 401


async def test_only_an_owner_recovers_keys(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    assert (await client.post("/v1/auth/api-keys/recover", json={})).status_code == 401
    token = (await client.post("/v1/auth/token", json={"user_id": "wp-2", "role": "manager"}, headers=s.sk)).json()["access_token"]
    assert (await client.post("/v1/auth/api-keys/recover", json={}, headers={"Authorization": f"Bearer {token}"})).status_code == 403
    assert (await client.post("/v1/auth/api-keys/recover", json={}, headers=s.pk)).status_code == 401


async def test_recovery_is_audited_without_the_keys(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, _ = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    new = (await client.post("/v1/auth/api-keys/recover", json={"revoke_existing": True}, headers=headers)).json()
    log = await client.get("/v1/admin/audit-log", headers=headers)
    assert log.json()[0]["action"] == "POST /v1/auth/api-keys/recover" and log.json()[0]["role"] == "owner"
    assert new["secret_key"] not in log.text and new["publishable_key"] not in log.text


async def test_recovery_refuses_unknown_fields_and_a_business_with_no_site(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    assert (await client.post("/v1/auth/api-keys/recover", json={"colour": 1}, headers=headers)).status_code == 422
    assert (await client.post("/v1/auth/api-keys/recover", json={"revoke_existing": "perhaps"}, headers=headers)).status_code == 422
    async with tenant_session(None) as db:
        await db.execute(text("DELETE FROM sites WHERE tenant_id = :t"), {"t": s.tenant_id})
    r = await client.post("/v1/auth/api-keys/recover", json={}, headers=headers)
    assert r.status_code == 409 and "no site" in r.json()["detail"]


# --------------------------------------------------------------------- the operator gives a business an owner address
async def test_the_operator_sets_the_owner_address_of_a_business_it_set_up(client: httpx.AsyncClient, owner: Recorder) -> None:
    s = await make_seed(client)  # (set up by the operator: no owner address)
    assert (await ask(client, "boss@example.com")).status_code == 202 and sign_in_mails(owner) == []
    r = await client.put(f"/v1/platform/tenants/{s.tenant_id}/owner-email", json={"email": "  Boss@Example.com "}, headers=PLATFORM)
    assert r.status_code == 200 and r.json() == {"tenant_id": str(s.tenant_id), "owner_email": "boss@example.com"}
    assert (await ask(client, "BOSS@example.com")).status_code == 202
    assert [m.recipient for m in sign_in_mails(owner)] == ["boss@example.com"]
    headers = {"Authorization": f"Bearer {(await use(client, token_of(owner))).json()['access_token']}"}
    assert (await client.get("/v1/auth/api-keys", headers=headers)).status_code == 200


async def test_the_owner_address_can_be_taken_away(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    r = await client.put(f"/v1/platform/tenants/{s.tenant_id}/owner-email", json={"email": None}, headers=PLATFORM)
    assert r.status_code == 200 and r.json()["owner_email"] is None
    assert (await ask(client, email)).status_code == 202 and sign_in_mails(owner) == []


async def test_two_businesses_cannot_share_an_owner_address(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, _ = await new_business(client, owner)
    other = await make_seed(client)
    r = await client.put(f"/v1/platform/tenants/{other.tenant_id}/owner-email", json={"email": email}, headers=PLATFORM)
    assert r.status_code == 409 and "already" in r.json()["detail"]


async def test_only_the_operator_sets_it(client: httpx.AsyncClient, owner: Recorder) -> None:
    s = await make_seed(client)
    for headers in ({}, {"X-Platform-Token": "wrong"}, s.sk, s.admin):
        assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/owner-email", json={"email": "a@b.com"}, headers=headers)).status_code == 401, headers
    assert (await client.put(f"/v1/platform/tenants/{uuid.uuid4()}/owner-email", json={"email": "a@b.com"}, headers=PLATFORM)).status_code == 404
    for bad in ({"email": "nope"}, {}, {"email": "a@b.com", "x": 1}):
        assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/owner-email", json=bad, headers=PLATFORM)).status_code == 422, bad


async def test_the_operator_can_give_an_owner_address_at_set_up(client: httpx.AsyncClient, owner: Recorder) -> None:
    body = {"name": f"Set up {uuid.uuid4().hex[:6]}", "business_type": "salon", "owner_email": "Owner@Setup.example"}
    assert (await client.post("/v1/auth/tenants", json=body, headers=PLATFORM)).status_code == 201
    assert (await ask(client, "owner@setup.example")).status_code == 202 and [m.recipient for m in sign_in_mails(owner)] == ["owner@setup.example"]
    dup = await client.post("/v1/auth/tenants", json={**body, "name": "Another one"}, headers=PLATFORM)
    assert dup.status_code == 409 and dup.json()["error"] == "conflict"
    assert (await client.post("/v1/auth/tenants", json={**body, "owner_email": "not-an-address"}, headers=PLATFORM)).status_code == 422


# --------------------------------------------------------------------- limits and settings
async def test_asking_for_links_is_limited_per_client_address(client: httpx.AsyncClient, owner: Recorder, limited: Any) -> None:
    codes = [(await ask(client, f"nobody{i}@example.com")).status_code for i in range(11)]
    assert codes == [202] * 10 + [429]
    assert (await client.post("/v1/owner/login", json={"email": "x@example.com"})).json()["error"] == "rate_limited"


async def test_using_links_is_limited_per_client_address(client: httpx.AsyncClient, owner: Recorder, limited: Any) -> None:
    codes = [(await use(client, "x" * 43)).status_code for _ in range(31)]
    assert codes == [401] * 30 + [429]


def test_the_owner_routes_are_limited_by_their_own_buckets() -> None:
    assert classify("POST", "/v1/owner/login") == "owner_login" and classify("POST", "/v1/owner/login/") == "owner_login"
    assert classify("POST", "/v1/owner/login/verify") == "owner_login_verify"
    assert classify("GET", "/v1/owner/other") is None


def test_the_settings() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.owner_login_url, s.owner_login_ttl_seconds, s.owner_login_cooldown_seconds) == ("", 900, 60)
    for bad in ({"owner_login_ttl_seconds": 59}, {"owner_login_cooldown_seconds": -1}):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **bad)  # type: ignore[call-arg]


def test_start_up_asks_for_the_owner_login_page_when_sign_up_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "_providers", {})
    open_ = {"self_service_signup": True, "signup_verify_url": "https://x/{token}", "terms_url": "https://x/t"}
    said = [p for p in delivery.configuration_problems(Settings(_env_file=None, **open_)) if "OWNER_LOGIN_URL" in p]  # type: ignore[call-arg]
    assert len(said) == 1 and "bare token" in said[0]
    assert not [p for p in delivery.configuration_problems(Settings(_env_file=None, owner_login_url="https://x/{token}", **open_)) if "OWNER_LOGIN_URL" in p]  # type: ignore[call-arg]
    assert not [p for p in delivery.configuration_problems(Settings(_env_file=None)) if "OWNER_LOGIN_URL" in p]  # type: ignore[call-arg]


async def test_recovery_revokes_only_the_keys_still_working(client: httpx.AsyncClient, owner: Recorder) -> None:
    email, s = await new_business(client, owner)
    headers = await signed_in(client, owner, email)
    keys = (await client.get("/v1/auth/api-keys", headers=headers)).json()
    publishable = next(k for k in keys if k["kind"] == "publishable")
    assert (await client.delete(f"/v1/auth/api-keys/{publishable['id']}", headers=headers)).status_code == 200
    when = next(k for k in (await client.get("/v1/auth/api-keys", headers=headers)).json() if k["id"] == publishable["id"])["revoked_at"]
    r = await client.post("/v1/auth/api-keys/recover", json={"revoke_existing": True}, headers=headers)
    assert r.json()["revoked"] == 1  # (only the secret key was still working)
    again = next(k for k in (await client.get("/v1/auth/api-keys", headers=headers)).json() if k["id"] == publishable["id"])["revoked_at"]
    assert again == when  # and the one revoked earlier keeps its time
