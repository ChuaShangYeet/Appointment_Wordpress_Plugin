"""The bot check (Cloudflare Turnstile) in front of sign-up and guest booking: a fake Cloudflare answers, nothing real is called (app.services.captcha)."""
from __future__ import annotations

import logging
import urllib.parse
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.core import logs
from app.core.config import Settings, get_settings
from app.core.exceptions import CaptchaFailed, CaptchaRequired, CaptchaUnavailable
from app.services import captcha, delivery
from app.services.captcha import TurnstileVerifier
from tests.conftest import Recorder, Seed, at
from tests.test_ratelimit import redis  # noqa: F401  (fixture)
from tests.test_signup import ask, open_signup, request, token_of, verify  # noqa: F401  (fixtures and helpers)

SECRET = "0x4AAAAAAAsecret-key-for-tests-0123456789"
SITE_KEY = "0x4AAAAAAAsite-key-for-tests"


class Cloudflare:
    """A stand-in for Turnstile's ``siteverify``. A token is ``good-<action>`` (valid for that form), ``bad-...`` (refused) or anything else (unknown); each is good once."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []
        self.used: set[str] = set()
        self.down: Exception | None = None
        self.status = 200
        self.raw: str | None = None
        self.secret_ok = True

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down is not None:
            raise self.down
        form = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode(), keep_blank_values=True).items()}
        form["_url"] = str(request.url)
        form["_content_type"] = request.headers.get("content-type", "")
        self.calls.append(form)
        if self.raw is not None:
            return httpx.Response(self.status, text=self.raw)
        if not self.secret_ok:
            return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-secret"]})
        token = form.get("response", "")
        if token in self.used:
            return httpx.Response(200, json={"success": False, "error-codes": ["timeout-or-duplicate"]})
        if token.startswith("good-"):
            self.used.add(token)
            return httpx.Response(self.status, json={"success": True, "hostname": "shop.example.test", "action": token.split("-", 2)[1]})
        return httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response"]})

    def verifier(self) -> TurnstileVerifier:
        return TurnstileVerifier(SECRET, verify_url="https://cf.example.test/siteverify", timeout=3.0, transport=httpx.MockTransport(self))


@pytest.fixture
def cloudflare() -> Iterator[Cloudflare]:
    """Turnstile switched on against the fake (and off again afterwards)."""
    fake = Cloudflare()
    captcha.set_verifier(fake.verifier())
    try:
        yield fake
    finally:
        captcha.set_verifier(None)


def token(action: str) -> str:
    return f"good-{action}-{uuid.uuid4().hex}"


# --------------------------------------------------------------------- the verifier against a fake Cloudflare
async def test_the_token_goes_to_cloudflare_with_the_secret_and_the_address(cloudflare: Cloudflare) -> None:
    t = token("signup")
    await cloudflare.verifier().verify(t, action="signup", remote_ip="203.0.113.9")
    (call,) = cloudflare.calls
    assert call["secret"] == SECRET and call["response"] == t and call["remoteip"] == "203.0.113.9"
    assert call["_url"] == "https://cf.example.test/siteverify" and "x-www-form-urlencoded" in call["_content_type"]


async def test_the_default_address_is_cloudflares_and_the_timeout_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    assert TurnstileVerifier(SECRET).verify_url == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    seen: dict[str, Any] = {}
    real = httpx.AsyncClient

    def spy(**kwargs: Any) -> httpx.AsyncClient:
        seen.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(captcha.httpx, "AsyncClient", spy)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True, "action": "signup"}))
    await TurnstileVerifier(SECRET, timeout=2.5, transport=transport).verify("t", action="signup")
    assert seen["timeout"] == 2.5 and seen["trust_env"] is False


async def test_no_address_is_sent_when_there_is_none(cloudflare: Cloudflare) -> None:
    await cloudflare.verifier().verify(token("booking"), action="booking")
    assert "remoteip" not in cloudflare.calls[0]


@pytest.mark.parametrize("answer", [
    {"success": True, "action": "booking"}, {"success": True}, {"success": True, "action": ""}, {"success": True, "action": "Signup"},
])
async def test_a_token_made_for_another_form_is_refused(answer: dict[str, Any]) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=answer))
    with pytest.raises(CaptchaFailed) as caught:
        await TurnstileVerifier(SECRET, transport=transport).verify("t", action="signup")
    assert caught.value.extra == {"reason": "wrong-action"}


async def test_a_token_made_for_the_right_form_is_accepted() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True, "action": "signup", "hostname": "x"}))
    await TurnstileVerifier(SECRET, transport=transport).verify("t", action="signup")


@pytest.mark.parametrize("codes, reason", [
    (["invalid-input-response"], "invalid-input-response"), (["timeout-or-duplicate"], "timeout-or-duplicate"),
    (["missing-input-response", "other"], "missing-input-response"), ([], "failed"),
])
async def test_a_refused_token_says_to_try_again(codes: list[str], reason: str) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False, "error-codes": codes}))
    with pytest.raises(CaptchaFailed) as caught:
        await TurnstileVerifier(SECRET, transport=transport).verify("t", action="signup")
    assert caught.value.extra == {"reason": reason} and "again" in caught.value.message and caught.value.status_code == 403


@pytest.mark.parametrize("code", ["invalid-input-secret", "missing-input-secret", "bad-request", "internal-error"])
async def test_our_fault_or_cloudflares_is_not_the_visitors(code: str, caplog: pytest.LogCaptureFixture) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False, "error-codes": [code]}))
    with pytest.raises(CaptchaUnavailable) as caught:
        await TurnstileVerifier(SECRET, transport=transport).verify("t", action="signup")
    assert caught.value.status_code == 503
    assert any(code in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records) and SECRET not in caplog.text


async def test_a_visitor_error_next_to_ours_is_still_ours() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": False, "error-codes": ["invalid-input-response", "invalid-input-secret"]}))
    with pytest.raises(CaptchaUnavailable):
        await TurnstileVerifier(SECRET, transport=transport).verify("t", action="signup")


@pytest.mark.parametrize("error", [httpx.ConnectError("boom"), httpx.ReadTimeout("slow"), httpx.ConnectTimeout("slow")])
async def test_an_unreachable_cloudflare_is_unavailable_and_the_log_keeps_no_secret(error: Exception, caplog: pytest.LogCaptureFixture) -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise error

    with pytest.raises(CaptchaUnavailable):
        await TurnstileVerifier(SECRET, transport=httpx.MockTransport(down)).verify("secret-token-value", action="signup")
    assert type(error).__name__ in caplog.text and SECRET not in caplog.text and "secret-token-value" not in caplog.text


@pytest.mark.parametrize("status, body", [(500, "<html>oops</html>"), (200, "not json"), (200, "{}"), (200, "[]"), (200, '"x"'), (502, '{"success": true}')])
async def test_an_unreadable_answer_is_unavailable(status: int, body: str) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(status, text=body))
    if status == 502:  # (a gateway error page that happens to be valid JSON with success=true is still not an answer we trust)
        transport = httpx.MockTransport(lambda r: httpx.Response(status, json={"error": "bad gateway"}))
    with pytest.raises(CaptchaUnavailable):
        await TurnstileVerifier(SECRET, transport=transport).verify("t", action="signup")


@pytest.mark.parametrize("value", ["true", 1, "yes", None])
async def test_only_a_real_true_counts_as_success(value: Any) -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": value, "action": "signup", "error-codes": []}))
    with pytest.raises(CaptchaFailed):
        await TurnstileVerifier(SECRET, transport=transport).verify("t", action="signup")


# --------------------------------------------------------------------- check()
async def test_with_no_verifier_nothing_is_checked() -> None:
    assert captcha.get_verifier() is None
    await captcha.check(None, action="signup")
    await captcha.check("anything", action="booking")


@pytest.mark.parametrize("missing", [None, "", "   "])
async def test_a_missing_token_is_asked_for(cloudflare: Cloudflare, missing: str | None) -> None:
    with pytest.raises(CaptchaRequired) as caught:
        await captcha.check(missing, action="signup")
    assert caught.value.status_code == 403 and "X-Captcha-Token" in caught.value.message and cloudflare.calls == []


async def test_a_token_that_is_too_long_is_refused_without_asking_cloudflare(cloudflare: Cloudflare) -> None:
    with pytest.raises(CaptchaFailed):
        await captcha.check("good-signup-" + "x" * 2040, action="signup")  # 2052 characters
    assert cloudflare.calls == []
    with pytest.raises(CaptchaFailed):
        await captcha.check("good-signup-" + "x" * 2037, action="signup")  # 2049
    await captcha.check("good-signup-" + "x" * 2036, action="signup")  # exactly 2048
    assert len(cloudflare.calls) == 1


async def test_the_token_is_trimmed_before_it_is_sent(cloudflare: Cloudflare) -> None:
    await captcha.check("  good-signup-abc \n", action="signup")
    assert cloudflare.calls[0]["response"] == "good-signup-abc"


async def test_a_token_works_once(cloudflare: Cloudflare) -> None:
    t = token("booking")
    await captcha.check(t, action="booking")
    with pytest.raises(CaptchaFailed) as caught:
        await captcha.check(t, action="booking")
    assert caught.value.extra == {"reason": "timeout-or-duplicate"}


async def test_an_outage_refuses_unless_it_is_set_to_let_requests_through(cloudflare: Cloudflare, caplog: pytest.LogCaptureFixture) -> None:
    cloudflare.down = httpx.ConnectError("down")
    with pytest.raises(CaptchaUnavailable):
        await captcha.check(token("booking"), action="booking")
    captcha.set_verifier(cloudflare.verifier(), fail_open=True)
    caplog.clear()
    await captcha.check(token("booking"), action="booking")  # lets it through ...
    assert any("CAPTCHA_FAIL_OPEN" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)  # ... and says so
    with pytest.raises(CaptchaRequired):
        await captcha.check(None, action="booking")  # but a missing token is still refused
    cloudflare.down = None
    with pytest.raises(CaptchaFailed):
        await captcha.check("bad-token", action="booking")  # and so is a wrong one


# --------------------------------------------------------------------- settings
def test_the_check_is_off_by_default_and_a_provider_needs_both_keys() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.captcha_provider == "" and s.captcha_fail_open is False and s.captcha_timeout_seconds == 5.0
    assert s.turnstile_verify_url == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    for keys in ({}, {"turnstile_site_key": SITE_KEY}, {"turnstile_secret_key": SECRET}):
        with pytest.raises(ValidationError, match="TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY"):
            Settings(_env_file=None, captcha_provider="turnstile", **keys)  # type: ignore[call-arg]
    ok = Settings(_env_file=None, captcha_provider="turnstile", turnstile_site_key=SITE_KEY, turnstile_secret_key=SECRET)  # type: ignore[call-arg]
    assert SECRET not in repr(ok) and SECRET not in str(ok.model_dump())
    with pytest.raises(ValidationError):
        Settings(_env_file=None, captcha_provider="recaptcha")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, captcha_timeout_seconds=0)  # type: ignore[call-arg]


def test_configure_registers_what_the_settings_describe() -> None:
    try:
        captcha.configure(Settings(_env_file=None))  # type: ignore[call-arg]
        assert captcha.get_verifier() is None
        captcha.configure(Settings(  # type: ignore[call-arg]
            _env_file=None, captcha_provider="turnstile", turnstile_site_key=SITE_KEY, turnstile_secret_key=SECRET,
            turnstile_verify_url="https://cf.example.test/v", captcha_timeout_seconds=2.5, captcha_fail_open=True,
        ))
        verifier = captcha.get_verifier()
        assert isinstance(verifier, TurnstileVerifier) and verifier.verify_url == "https://cf.example.test/v" and verifier.timeout == 2.5
        assert verifier._secret == SECRET and captcha._fail_open is True
        captcha.configure(Settings(_env_file=None))  # type: ignore[call-arg]
        assert captcha.get_verifier() is None and captcha._fail_open is False
    finally:
        captcha.set_verifier(None)


def test_what_a_page_is_told() -> None:
    off = captcha.public_config(Settings(_env_file=None))  # type: ignore[call-arg]
    assert off == {"enabled": False, "provider": None, "site_key": None, "protects": []}
    on = captcha.public_config(Settings(_env_file=None, captcha_provider="turnstile", turnstile_site_key=SITE_KEY, turnstile_secret_key=SECRET))  # type: ignore[call-arg]
    assert on == {"enabled": True, "provider": "turnstile", "site_key": SITE_KEY, "protects": ["signup", "booking"]}


def test_the_start_up_report_asks_for_the_check_in_production() -> None:
    base: dict[str, Any] = {"environment": "production", "smtp_host": "mail.example.test"}
    delivery.clear_providers()
    quiet = delivery.configuration_problems(Settings(_env_file=None, **base))  # type: ignore[call-arg]
    assert any("CAPTCHA_PROVIDER is not set" in p for p in quiet)
    on = delivery.configuration_problems(Settings(_env_file=None, captcha_provider="turnstile", turnstile_site_key=SITE_KEY, turnstile_secret_key=SECRET, **base))  # type: ignore[call-arg]
    assert not any("CAPTCHA" in p for p in on)


# --------------------------------------------------------------------- the page's side
async def test_a_page_asks_whether_a_check_is_needed(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    r = await client.get("/v1/captcha")
    assert r.status_code == 200 and r.json() == {"enabled": False, "provider": None, "site_key": None, "protects": []}
    monkeypatch.setattr(get_settings(), "captcha_provider", "turnstile")
    monkeypatch.setattr(get_settings(), "turnstile_site_key", SITE_KEY)
    monkeypatch.setattr(get_settings(), "turnstile_secret_key", __import__("pydantic").SecretStr(SECRET))
    r = await client.get("/v1/captcha")
    assert r.json() == {"enabled": True, "provider": "turnstile", "site_key": SITE_KEY, "protects": ["signup", "booking"]}
    assert SECRET not in r.text


async def test_the_sign_up_page_is_told_too(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert (await client.get("/v1/signup/config")).json()["captcha"]["enabled"] is False
    monkeypatch.setattr(get_settings(), "captcha_provider", "turnstile")
    monkeypatch.setattr(get_settings(), "turnstile_site_key", SITE_KEY)
    body = (await client.get("/v1/signup/config")).json()
    assert body["captcha"] == {"enabled": True, "provider": "turnstile", "site_key": SITE_KEY, "protects": ["signup", "booking"]}


# --------------------------------------------------------------------- sign-up
async def test_sign_up_without_a_token_is_refused_and_nothing_is_sent(client: httpx.AsyncClient, open_signup: Recorder, cloudflare: Cloudflare, redis: Any) -> None:  # noqa: F811
    r = await ask(client)
    assert r.status_code == 403 and r.json()["error"] == "captcha_required"
    assert open_signup.sent == [] and cloudflare.calls == [] and await redis.keys("signup:*") == []  # no email, no pending request, no cooldown spent


async def test_sign_up_with_a_good_token_goes_ahead(client: httpx.AsyncClient, open_signup: Recorder, cloudflare: Cloudflare) -> None:
    r = await client.post("/v1/signup", json=request(), headers={"X-Captcha-Token": token("signup")})
    assert r.status_code == 202 and len(open_signup.sent) == 1
    (call,) = cloudflare.calls
    assert call["remoteip"] == "127.0.0.1" and call["secret"] == SECRET


async def test_a_client_with_no_plain_address_is_checked_without_one(open_signup: Recorder, cloudflare: Cloudflare) -> None:
    from app.main import app

    for peer in ("2001:db8::1", "::ffff:203.0.113.7"):
        cloudflare.calls.clear()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(peer, 4000)), base_url="http://test") as c:
            r = await c.post("/v1/signup", json=request(), headers={"X-Captcha-Token": token("signup")})
        assert r.status_code == 202, r.text
        # an IPv6 client is grouped by /64 for the rate limits ("2001:db8::/64" is no address); a mapped IPv4 one is its IPv4 address
        assert cloudflare.calls[0].get("remoteip") == ("203.0.113.7" if peer.startswith("::ffff") else None)


async def test_the_service_starts_the_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main

    started: list[str] = []

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(main.hardening, "enforce", nothing)
    monkeypatch.setattr(main, "dispose_engine", nothing)
    monkeypatch.setattr(main.delivery, "report_configuration", lambda *a, **k: [])
    monkeypatch.setattr(main.captcha, "configure", lambda settings: started.append("captcha"))
    async with main.lifespan(main.app):
        pass
    assert started == ["captcha"]


async def test_sign_up_with_a_wrong_token_is_refused_without_using_up_the_day(client: httpx.AsyncClient, open_signup: Recorder, cloudflare: Cloudflare, redis: Any) -> None:  # noqa: F811
    r = await client.post("/v1/signup", json=request(), headers={"X-Captcha-Token": "bad-token"})
    assert r.status_code == 403 and r.json()["error"] == "captcha_failed" and r.json()["reason"] == "invalid-input-response"
    assert "bad-token" not in r.text and open_signup.sent == [] and await redis.keys("signup:*") == []


async def test_a_booking_token_cannot_sign_anyone_up(client: httpx.AsyncClient, open_signup: Recorder, cloudflare: Cloudflare) -> None:
    r = await client.post("/v1/signup", json=request(), headers={"X-Captcha-Token": token("booking")})
    assert r.status_code == 403 and r.json()["reason"] == "wrong-action" and open_signup.sent == []


async def test_a_token_signs_up_once(client: httpx.AsyncClient, open_signup: Recorder, cloudflare: Cloudflare) -> None:
    headers = {"X-Captcha-Token": token("signup")}
    assert (await client.post("/v1/signup", json=request(), headers=headers)).status_code == 202
    r = await client.post("/v1/signup", json=request(), headers=headers)
    assert r.status_code == 403 and r.json()["reason"] == "timeout-or-duplicate" and len(open_signup.sent) == 1


async def test_a_closed_sign_up_says_so_before_asking_for_a_token(client: httpx.AsyncClient, cloudflare: Cloudflare, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "self_service_signup", False)
    r = await client.post("/v1/signup", json=request())
    assert r.status_code == 403 and r.json()["error"] == "signup_disabled" and cloudflare.calls == []


async def test_confirming_the_email_needs_no_token(client: httpx.AsyncClient, open_signup: Recorder, cloudflare: Cloudflare) -> None:
    assert (await client.post("/v1/signup", json=request(), headers={"X-Captcha-Token": token("signup")})).status_code == 202
    done = await verify(client, token_of(open_signup))  # (the link in the email is the proof here)
    assert done.status_code == 201 and len(cloudflare.calls) == 1


async def test_an_outage_stops_sign_up_unless_set_to_let_it_through(client: httpx.AsyncClient, open_signup: Recorder, cloudflare: Cloudflare) -> None:
    cloudflare.down = httpx.ConnectError("down")
    r = await client.post("/v1/signup", json=request(), headers={"X-Captcha-Token": token("signup")})
    assert r.status_code == 503 and r.json()["error"] == "captcha_unavailable" and open_signup.sent == []
    captcha.set_verifier(cloudflare.verifier(), fail_open=True)
    r = await client.post("/v1/signup", json=request(), headers={"X-Captcha-Token": token("signup")})
    assert r.status_code == 202 and len(open_signup.sent) == 1


async def test_without_a_provider_sign_up_asks_for_nothing(client: httpx.AsyncClient, open_signup: Recorder) -> None:
    assert (await ask(client)).status_code == 202


# --------------------------------------------------------------------- booking
async def book(s: Seed, rid: uuid.UUID, headers: dict[str, str], hour: int = 10) -> httpx.Response:
    return await s.client.post("/v1/public/bookings", json=s.booking_body(at(s.day, hour), rid), headers=headers)


async def test_booking_without_a_token_is_refused_and_nothing_is_booked(seeded: Seed, cloudflare: Cloudflare) -> None:
    rid = await seeded.add_staff("Mei")
    r = await book(seeded, rid, seeded.pk)
    assert r.status_code == 403 and r.json()["error"] == "captcha_required" and cloudflare.calls == []
    assert (await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)).json() == []


async def test_booking_with_a_good_token_goes_ahead(seeded: Seed, cloudflare: Cloudflare) -> None:
    rid = await seeded.add_staff("Mei")
    r = await book(seeded, rid, {**seeded.pk, "X-Captcha-Token": token("booking")})
    assert r.status_code == 201, r.text
    assert cloudflare.calls[0]["remoteip"] == "127.0.0.1"


async def test_each_booking_needs_its_own_token(seeded: Seed, cloudflare: Cloudflare) -> None:
    rid = await seeded.add_staff("Mei")
    headers = {**seeded.pk, "X-Captcha-Token": token("booking")}
    assert (await book(seeded, rid, headers, 10)).status_code == 201
    r = await book(seeded, rid, headers, 12)
    assert r.status_code == 403 and r.json()["reason"] == "timeout-or-duplicate"


async def test_a_sign_up_token_cannot_book(seeded: Seed, cloudflare: Cloudflare) -> None:
    rid = await seeded.add_staff("Mei")
    r = await book(seeded, rid, {**seeded.pk, "X-Captcha-Token": token("signup")})
    assert r.status_code == 403 and r.json()["reason"] == "wrong-action"


async def test_the_servers_own_secret_key_books_without_a_token(seeded: Seed, cloudflare: Cloudflare) -> None:
    rid = await seeded.add_staff("Mei")
    r = await book(seeded, rid, seeded.sk)  # the WordPress server booking for a visitor: no widget to show
    assert r.status_code == 201 and cloudflare.calls == []


async def test_an_unknown_site_key_is_refused_before_cloudflare_is_asked(seeded: Seed, cloudflare: Cloudflare) -> None:
    rid = await seeded.add_staff("Mei")
    r = await book(seeded, rid, {"X-Site-Key": "pk_nope", "X-Captcha-Token": token("booking")})
    assert r.status_code == 401 and cloudflare.calls == []


async def test_reading_needs_no_token(seeded: Seed, cloudflare: Cloudflare) -> None:
    await seeded.add_staff("Mei")
    r = await seeded.client.get("/v1/public/availability", params={"date": seeded.day.isoformat(), "service_ids": str(seeded.service_id)}, headers=seeded.pk)
    assert r.status_code == 200 and (await seeded.client.get("/v1/public/services", headers=seeded.pk)).status_code == 200 and cloudflare.calls == []


async def test_a_wrong_token_does_not_book(seeded: Seed, cloudflare: Cloudflare) -> None:
    rid = await seeded.add_staff("Mei")
    r = await book(seeded, rid, {**seeded.pk, "X-Captcha-Token": "bad-token"})
    assert r.status_code == 403 and r.json()["error"] == "captcha_failed"
    assert (await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)).json() == []


async def test_an_outage_stops_booking_unless_set_to_let_it_through(seeded: Seed, cloudflare: Cloudflare) -> None:
    rid = await seeded.add_staff("Mei")
    cloudflare.down = httpx.ConnectError("down")
    headers = {**seeded.pk, "X-Captcha-Token": token("booking")}
    r = await book(seeded, rid, headers)
    assert r.status_code == 503 and r.json()["error"] == "captcha_unavailable"
    captcha.set_verifier(cloudflare.verifier(), fail_open=True)
    assert (await book(seeded, rid, headers)).status_code == 201


async def test_without_a_provider_booking_asks_for_nothing(seeded: Seed) -> None:
    rid = await seeded.add_staff("Mei")
    assert (await book(seeded, rid, seeded.pk)).status_code == 201


# --------------------------------------------------------------------- the token stays out of the logs
def test_a_captcha_token_in_a_log_line_is_removed() -> None:
    text = logs.redact("headers: X-Captcha-Token: 0.AbCdEf_ghIJkl-1234567890 accept: */*")
    assert "0.AbCdEf" not in text and "X-Captcha-Token" in text


async def test_the_token_is_not_in_the_access_log_or_sentry(seeded: Seed, cloudflare: Cloudflare, caplog: pytest.LogCaptureFixture) -> None:
    from app.core import observability

    caplog.set_level(logging.DEBUG)
    rid = await seeded.add_staff("Mei")
    t = token("booking")
    await book(seeded, rid, {**seeded.pk, "X-Captcha-Token": t})
    assert t not in caplog.text
    scrubbed = observability.scrub_event({"extra": {"x-captcha-token": t, "X-Captcha-Token": t}})
    assert t not in str(scrubbed)
