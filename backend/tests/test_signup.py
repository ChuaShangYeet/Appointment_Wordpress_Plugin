"""Self-service sign-up (migration 0024): confirm an email address, then a business exists on the free plan."""
from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError

from app.core import ratelimit
from app.core.config import get_settings
from app.core.database import tenant_session
from app.models import BusinessProfile, Tenant
from app.services import delivery
from tests.conftest import PLATFORM, Recorder
from tests.test_ratelimit import clock, limited, redis  # noqa: F401  (fixtures)

TERMS = "2026-10"


@pytest.fixture
def open_signup(monkeypatch: pytest.MonkeyPatch, redis: Any, mail: Recorder) -> Recorder:  # noqa: F811
    """Sign-up switched on, an in-memory Redis, a recording email provider; the resend cooldown is off (tests that need it turn it on)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "self_service_signup", True)
    monkeypatch.setattr(settings, "signup_verify_url", "https://site.example/confirm?t={token}")
    monkeypatch.setattr(settings, "terms_version", TERMS)
    monkeypatch.setattr(settings, "terms_url", "https://site.example/terms")
    monkeypatch.setattr(settings, "signup_resend_cooldown_seconds", 0)
    return mail


def request(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": f"Glow {uuid.uuid4().hex[:6]}", "business_type": "salon", "owner_email": f"owner{uuid.uuid4().hex[:8]}@example.com",
        "accepted_terms_version": TERMS, "address_line1": "12 Jalan Bukit", "city": "Kuala Lumpur", "state": "KUL", "postcode": "50450",
        "domain": "glow.example",
    }
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


async def ask(client: httpx.AsyncClient, **over: Any) -> httpx.Response:
    return await client.post("/v1/signup", json=request(**over))


async def verify(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post("/v1/signup/verify", json={"token": token})


def token_of(mail: Recorder) -> str:
    sent = [m for m in mail.sent if m.channel == "email" and m.subject.startswith("Confirm your email")]
    assert sent, "no confirmation email was sent"
    found = re.search(r"\?t=([A-Za-z0-9_\-]+)", sent[-1].body)
    assert found, sent[-1].body
    return found.group(1)


async def sign_up(client: httpx.AsyncClient, mail: Recorder, **over: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ask, confirm and return (request body, the answer with the keys)."""
    body = request(**over)
    assert (await client.post("/v1/signup", json=body)).status_code == 202
    done = await verify(client, token_of(mail))
    assert done.status_code == 201, done.text
    return body, done.json()


async def tenant_by_email(email: str) -> Tenant | None:
    async with tenant_session(None) as db:
        return await db.scalar(select(Tenant).where(Tenant.owner_email == email))


# --------------------------------------------------------------------- closed by default
async def test_sign_up_is_closed_unless_switched_on(client: httpx.AsyncClient, redis: Any, mail: Recorder) -> None:  # noqa: F811
    assert get_settings().self_service_signup is False
    config = await client.get("/v1/signup/config")
    assert config.status_code == 200 and config.json()["enabled"] is False
    r = await ask(client)
    assert r.status_code == 403 and r.json()["error"] == "signup_disabled"
    assert (await verify(client, "x" * 30)).status_code == 403
    assert mail.sent == [] and await redis.keys("signup:*") == []


async def test_the_page_can_ask_whether_sign_up_is_open_and_which_terms_to_show(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    config = await client.get("/v1/signup/config")
    assert config.status_code == 200 and config.json() == {
        "enabled": True, "terms_version": TERMS, "terms_url": "https://site.example/terms",
        "captcha": {"enabled": False, "provider": None, "site_key": None, "protects": []},  # (see tests/test_captcha.py for when it is on)
    }


# --------------------------------------------------------------------- the whole path
async def test_a_business_signs_itself_up_by_confirming_its_email(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    body = request(owner_email="  Owner@Example.COM ", name="Glow Salon")
    r = await client.post("/v1/signup", json=body)
    assert r.status_code == 202 and r.json() == {"status": "sent"}
    (mail,) = open_signup.sent
    assert (mail.channel, mail.recipient, mail.subject) == ("email", "owner@example.com", "Confirm your email for JustSimple")
    assert "Glow Salon" in mail.body and "https://site.example/confirm?t=" in mail.body and "expires in 24 hours" in mail.body
    assert await tenant_by_email("owner@example.com") is None  # nothing exists yet

    done = await verify(client, token_of(open_signup))
    assert done.status_code == 201, done.text
    keys = done.json()
    assert set(keys) == {"tenant_id", "site_id", "publishable_key", "secret_key", "webhook_secret"}
    tenant = await tenant_by_email("owner@example.com")
    assert tenant is not None and str(tenant.id) == keys["tenant_id"]
    assert (tenant.plan, tenant.country, tenant.business_type, tenant.name, tenant.timezone, tenant.phone_country_code) == ("free", "MY", "salon", "Glow Salon", "Asia/Kuala_Lumpur", "60")
    assert tenant.terms_version == TERMS and tenant.terms_accepted_at is not None
    async with tenant_session(tenant.id) as db:
        profile = await db.scalar(select(BusinessProfile))
    assert profile is not None and profile.business_name == "Glow Salon" and profile.address == "12 Jalan Bukit, 50450 Kuala Lumpur, Kuala Lumpur"


async def test_the_keys_that_come_back_work_and_the_business_is_on_the_free_plan(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    _, keys = await sign_up(client, open_signup, country="SG", address_line1="1 Raffles Place", city=None, state=None, postcode="048616", business_type="spa")
    page = await client.get("/v1/public/business", headers={"X-Site-Key": keys["publishable_key"]})
    assert page.status_code == 200 and page.json()["country"] == "SG" and page.json()["timezone"] == "Asia/Singapore" and page.json()["business_type"] == "spa"
    token = await client.post("/v1/auth/token", json={"user_id": "wp-1", "role": "owner"}, headers={"X-API-Key": keys["secret_key"]})
    assert token.status_code == 200
    admin = {"Authorization": f"Bearer {token.json()['access_token']}"}
    plan = (await client.get("/v1/admin/plan", headers=admin)).json()
    assert plan["plan"]["code"] == "free" and plan["plan"]["limits"]["resources"] == 3
    for n in range(3):
        assert (await client.post("/v1/admin/resources", json={"type": "staff", "name": f"Mei{n}"}, headers=admin)).status_code == 201
    assert (await client.post("/v1/admin/resources", json={"type": "staff", "name": "Four"}, headers=admin)).status_code == 402  # the free plan's limits apply from the start


async def test_the_business_type_country_and_time_zone_chosen_at_sign_up_are_kept(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    body, keys = await sign_up(client, open_signup, country="AU", timezone="Australia/Perth", business_type="automotive",
                               address_line1="1 George St", city="Perth", state="Western Australia", postcode="6000")
    tenant = await tenant_by_email(body["owner_email"])
    assert tenant is not None
    assert (tenant.business_type, tenant.country, tenant.timezone, tenant.phone_country_code) == ("automotive", "AU", "Australia/Perth", "61")


async def test_a_confirmation_works_once(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    assert (await ask(client)).status_code == 202
    token = token_of(open_signup)
    first, second = await verify(client, token), await verify(client, token)
    assert (first.status_code, second.status_code) == (201, 401) and second.json()["error"] == "unauthorized"


async def test_two_people_confirming_the_same_link_at_once_make_one_business(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    assert (await ask(client)).status_code == 202
    token = token_of(open_signup)
    results = await asyncio.gather(verify(client, token), verify(client, token))
    assert sorted(r.status_code for r in results) == [201, 401]


async def test_a_made_up_or_malformed_token_gets_nothing(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    assert (await verify(client, "n" * 43)).status_code == 401
    for bad in ("short", "x" * 201, ""):
        assert (await verify(client, bad)).status_code == 422, bad
    assert (await client.post("/v1/signup/verify", json={"token": "n" * 43, "plan": "paid"})).status_code == 422
    assert (await client.post("/v1/signup/verify", json={})).status_code == 422


async def test_the_confirmation_expires(client: httpx.AsyncClient, open_signup: Recorder, redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "signup_token_ttl_seconds", 7200)
    assert (await ask(client)).status_code == 202
    (key,) = [k for k in await redis.keys("signup:token:*")]
    assert 7000 < await redis.ttl(key) <= 7200
    assert "expires in 2 hours" in open_signup.sent[-1].body
    await redis.delete(key)  # the time is up
    assert (await verify(client, token_of(open_signup))).status_code == 401


async def test_the_token_is_never_kept_as_it_was_sent(client: httpx.AsyncClient, open_signup: Recorder, redis: Any) -> None:  # noqa: F811
    assert (await ask(client)).status_code == 202
    token = token_of(open_signup)
    assert not any(token in key for key in await redis.keys("*"))  # only its hash is a key


# --------------------------------------------------------------------- one business per address, and no way to learn who has one
async def test_an_address_that_has_a_business_gets_the_same_answer_and_no_mail(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    body, _ = await sign_up(client, open_signup)
    sent = len(open_signup.sent)
    again = await client.post("/v1/signup", json=request(owner_email=body["owner_email"].upper()))
    assert again.status_code == 202 and again.json() == {"status": "sent"}  # identical to a new address
    assert len(open_signup.sent) == sent
    async with tenant_session(None) as db:
        assert len((await db.scalars(select(Tenant.id).where(Tenant.owner_email == body["owner_email"]))).all()) == 1


async def test_an_address_taken_between_asking_and_confirming_is_refused(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    body = request()
    assert (await client.post("/v1/signup", json=body)).status_code == 202
    async with tenant_session(None) as db:  # somebody (the operator, say) sets up a business for that address in the meantime
        await db.execute(insert(Tenant).values(name="Other", owner_email=body["owner_email"]))
    done = await verify(client, token_of(open_signup))
    assert done.status_code == 409 and "already exists" in done.json()["detail"]


async def test_the_database_allows_one_business_per_owner_address_and_any_number_with_none(client: httpx.AsyncClient) -> None:
    email = f"x{uuid.uuid4().hex[:8]}@example.com"
    async with tenant_session(None) as db:
        await db.execute(insert(Tenant).values(name="A", owner_email=email))
        await db.execute(insert(Tenant).values(name="B"))
        await db.execute(insert(Tenant).values(name="C"))  # (operator-made: no owner address, as many as you like)
    with pytest.raises(IntegrityError):
        async with tenant_session(None) as db:
            await db.execute(insert(Tenant).values(name="D", owner_email=email))


async def test_a_newer_request_replaces_the_older_for_the_same_address(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    email = "same@example.com"
    assert (await ask(client, owner_email=email)).status_code == 202
    first = token_of(open_signup)
    assert (await ask(client, owner_email=email)).status_code == 202
    second = token_of(open_signup)
    assert first != second
    assert (await verify(client, first)).status_code == 401  # the older link no longer works
    assert (await verify(client, second)).status_code == 201


async def test_asking_again_too_soon_is_refused_for_every_address_alike(client: httpx.AsyncClient, open_signup: Recorder, redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "signup_resend_cooldown_seconds", 60)
    body, _ = await sign_up(client, open_signup)  # (that asked once)
    r = await client.post("/v1/signup", json=request(owner_email=body["owner_email"]))
    assert r.status_code == 429 and r.json()["error"] == "too_many_requests"  # a known address is told the same as an unknown one
    (cooldown,) = await redis.keys("signup:email:*:cooldown")
    assert 0 < await redis.ttl(cooldown) <= 60  # and it ends
    assert (await ask(client, owner_email="someone-else@example.com")).status_code == 202  # another address is not held up


# --------------------------------------------------------------------- the request itself
@pytest.mark.parametrize("version", ["1999", "", "2026-10 "])
async def test_the_terms_must_be_the_current_version(client: httpx.AsyncClient, open_signup: Recorder, version: str) -> None:
    r = await ask(client, accepted_terms_version=version)
    assert r.status_code == 422, r.text
    assert open_signup.sent == []


async def test_the_terms_must_be_accepted_at_all(client: httpx.AsyncClient, open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    r = await client.post("/v1/signup", json={k: v for k, v in request().items() if k != "accepted_terms_version"})
    assert r.status_code == 422 and open_signup.sent == []
    monkeypatch.setattr(get_settings(), "terms_version", "1")  # (the default version: leaving the field out must not count as accepting it)
    r = await client.post("/v1/signup", json={k: v for k, v in request().items() if k != "accepted_terms_version"})
    assert r.status_code == 422 and "accepted_terms_version" in r.text and open_signup.sent == []


async def test_terms_that_changed_while_the_person_was_confirming_must_be_accepted_again(client: httpx.AsyncClient, open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    body = request()
    assert (await client.post("/v1/signup", json=body)).status_code == 202
    monkeypatch.setattr(get_settings(), "terms_version", "2026-11")
    done = await verify(client, token_of(open_signup))
    assert done.status_code == 422 and "terms changed" in done.json()["detail"]
    assert await tenant_by_email(body["owner_email"]) is None


@pytest.mark.parametrize("extra", [{"plan": "paid"}, {"plan": "free"}, {"owner_name": "x"}, {"status": "active"}, {"tenant_id": str(uuid.uuid4())}])
async def test_nothing_can_be_chosen_that_is_not_asked_for(client: httpx.AsyncClient, open_signup: Recorder, extra: dict[str, Any]) -> None:
    r = await ask(client, **extra)
    assert r.status_code == 422 and open_signup.sent == []


@pytest.mark.parametrize(("over", "words"), [
    ({"address_line1": None, "city": None, "state": None, "postcode": None}, "address is needed"),
    ({"postcode": None}, "postcode is needed"),
    ({"postcode": "5045"}, "5 digits"),
    ({"state": None}, "state is needed"),
    ({"state": "NSW"}, "not a state"),
    ({"country": "AU"}, "not a state"),  # (Malaysian address parts under Australia)
    ({"country": "XX"}, "not a country code"),
    ({"timezone": "Australia/Sydney"}, "time zones"),
    ({"country": "MY", "phone_country_code": "65"}, "not Malaysia's"),
    ({"owner_email": "not-an-email"}, "email"),
    ({"owner_email": None}, "owner_email"),
    ({"name": ""}, "name"),
    ({"business_type": "bakery"}, "business_type"),
])
async def test_a_request_that_does_not_fit_is_refused_before_any_mail_is_sent(client: httpx.AsyncClient, open_signup: Recorder, redis: Any, over: dict[str, Any], words: str) -> None:  # noqa: F811
    r = await client.post("/v1/signup", json=request(**over))  # (a ``None`` leaves that field out)
    assert r.status_code == 422 and words in r.text, r.text
    assert open_signup.sent == [] and await redis.keys("signup:*") == []


async def test_the_address_is_required_but_singapore_needs_no_state_or_city(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    r = await ask(client, country="SG", address_line1="1 Raffles Place", city=None, state=None, postcode="048616")
    assert r.status_code == 202, r.text


# --------------------------------------------------------------------- the email
async def test_what_the_person_typed_cannot_break_out_of_the_email(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    assert (await ask(client, name="Glow\r\nBcc: attacker@example.com")).status_code == 202
    (mail,) = open_signup.sent
    assert "\nBcc:" not in mail.body and "\r" not in mail.body and "Bcc" not in (mail.subject or "")
    assert mail.recipient != "attacker@example.com"


async def test_an_address_gets_only_so_many_confirmations_a_day(client: httpx.AsyncClient, open_signup: Recorder, redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "signup_requests_per_address_per_day", 2)
    assert [(await ask(client, owner_email="capped@example.com")).status_code for _ in range(2)] == [202, 202]
    r = await ask(client, owner_email="capped@example.com")
    assert r.status_code == 429 and "today" in r.json()["detail"]
    assert len(open_signup.sent) == 2  # nothing more was sent
    assert (await ask(client, owner_email="other@example.com")).status_code == 202  # another address is unaffected
    assert (await ask(client, owner_email="Capped@Example.com")).status_code == 429  # the same address, however it is written
    day = sorted(await redis.keys("signup:email:*:day"))[0]
    assert 0 < await redis.ttl(day) <= 86400


async def test_a_known_address_reaches_the_daily_cap_like_any_other(client: httpx.AsyncClient, open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "signup_requests_per_address_per_day", 3)
    body, _ = await sign_up(client, open_signup, owner_email="taken@example.com")  # (one request used)
    assert [(await ask(client, owner_email=body["owner_email"])).status_code for _ in range(2)] == [202, 202]  # nothing is sent, but it counts ...
    assert (await ask(client, owner_email=body["owner_email"])).status_code == 429  # ... so the cap cannot be used to tell known addresses from new ones


async def test_no_daily_cap_when_it_is_zero(client: httpx.AsyncClient, open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "signup_requests_per_address_per_day", 0)
    assert all([(await ask(client, owner_email="many@example.com")).status_code == 202 for _ in range(8)])


async def test_the_email_names_the_service_the_operator_configured(client: httpx.AsyncClient, open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "platform_name", "Acme Bookings")
    assert (await ask(client, name="Glow")).status_code == 202
    (mail,) = open_signup.sent
    assert mail.subject == "Confirm your email for Acme Bookings" and "set up Glow on Acme Bookings" in mail.body


async def test_without_a_confirmation_page_the_email_carries_the_bare_token(client: httpx.AsyncClient, open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "signup_verify_url", "")
    assert (await ask(client)).status_code == 202
    assert "Your confirmation token: " in open_signup.sent[-1].body


async def test_an_email_that_cannot_be_sent_says_so_and_leaves_nothing_behind(client: httpx.AsyncClient, open_signup: Recorder, redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setattr(get_settings(), "signup_resend_cooldown_seconds", 60)
    open_signup.failing["email"] = RuntimeError("smtp down")
    body = request()
    r = await client.post("/v1/signup", json=body)
    assert r.status_code == 503 and r.json()["error"] == "delivery_failed"
    assert await redis.keys("signup:*") == []  # no pending request, and no cool-down held against the person
    open_signup.failing.clear()
    assert (await client.post("/v1/signup", json=body)).status_code == 202


async def test_with_no_email_provider_nobody_can_sign_up(client: httpx.AsyncClient, open_signup: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "_providers", {})
    assert (await ask(client)).status_code == 503


# --------------------------------------------------------------------- limits on asking
def test_sign_up_calls_have_their_own_limits() -> None:
    assert ratelimit.classify("POST", "/v1/signup") == "signup" and ratelimit.classify("POST", "/v1/signup/") == "signup"
    assert ratelimit.classify("POST", "/v1/signup/verify") == "signup_verify"
    assert ratelimit.classify("GET", "/v1/signup/config") is None
    assert ratelimit.DEFAULTS["signup"]["ip"] == ratelimit.Limit(5, 3600) and ratelimit.DEFAULTS["signup_verify"]["ip"].requests <= 20


async def test_one_address_cannot_ask_for_many_businesses(client: httpx.AsyncClient, open_signup: Recorder, limited: Any) -> None:
    codes = [(await ask(client)).status_code for _ in range(6)]
    assert codes == [202] * 5 + [429]  # five an hour from one address
    assert len(open_signup.sent) == 5
    assert (await client.get("/v1/signup/config")).status_code == 200  # reading the form's settings is not held up


async def test_trying_many_tokens_is_slowed_down(client: httpx.AsyncClient, open_signup: Recorder, limited: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "rate_limits", {"signup_verify": {"ip": (3, 60)}})
    codes = [(await verify(client, f"{n:0>40}")).status_code for n in range(5)]
    assert codes == [401, 401, 401, 429, 429]


# --------------------------------------------------------------------- the operator's way is unchanged
async def test_a_business_the_operator_sets_up_has_no_owner_address_and_is_paid(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Op {uuid.uuid4().hex[:6]}"})
    assert r.status_code == 201
    async with tenant_session(None) as db:
        row = await db.get(Tenant, uuid.UUID(r.json()["tenant_id"]))
    assert row is not None and (row.plan, row.owner_email, row.terms_version, row.terms_accepted_at) == ("paid", None, None, None)


async def test_the_operators_endpoint_still_needs_its_token_and_can_still_choose_a_plan(client: httpx.AsyncClient) -> None:
    assert (await client.post("/v1/auth/tenants", json={"name": "x"})).status_code == 401
    assert (await client.post("/v1/auth/tenants", headers={"X-Platform-Token": "wrong"}, json={"name": "x"})).status_code == 401
    ok = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Op {uuid.uuid4().hex[:6]}", "plan": "free", "domain": "op.example"})
    assert ok.status_code == 201


# --------------------------------------------------------------------- start-up warnings
def test_start_up_says_what_a_sign_up_needs_that_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.config import Settings

    monkeypatch.setattr(delivery, "_providers", {})
    quiet = delivery.configuration_problems(Settings(_env_file=None))  # type: ignore[call-arg]
    assert not any("SELF_SERVICE_SIGNUP" in p for p in quiet)  # nothing to say while it is off
    loud = delivery.configuration_problems(Settings(_env_file=None, self_service_signup=True))  # type: ignore[call-arg]
    said = [p for p in loud if "SELF_SERVICE_SIGNUP" in p]
    assert len(said) == 4 and any("email provider" in p for p in said) and any("SIGNUP_VERIFY_URL" in p for p in said) and any("TERMS_URL" in p for p in said)
    assert any("OWNER_LOGIN_URL" in p for p in said)
    fine = delivery.configuration_problems(Settings(_env_file=None, self_service_signup=True, signup_verify_url="https://x/{token}", terms_url="https://x/t", owner_login_url="https://x/o/{token}"))  # type: ignore[call-arg]
    left = [p for p in fine if "SELF_SERVICE_SIGNUP" in p]
    assert len(left) == 1 and "email provider" in left[0]  # only the missing provider remains
