"""Rate limiting: classification, the sliding window, who the client is, both tiers, failure behaviour."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from app.core import ratelimit
from app.core.config import get_settings
from app.core.ratelimit import DEFAULTS, Limit, classify, client_ip, hit, limit_for
from tests.conftest import PLATFORM, Recorder, Seed, at, make_seed
from tests.test_customer_cancel import PHONE, otp, request_code  # noqa: F401
from tests.test_customer_identity import EMAIL, ask, booked_with_email  # noqa: F401

T0 = 1_700_000_040.0  # a multiple of 60: the start of a window


# --------------------------------------------------------------------- pure: which bucket
@pytest.mark.parametrize(("method", "path", "bucket"), [
    ("GET", "/v1/public/services", "public_read"), ("GET", "/v1/public/availability", "public_read"), ("HEAD", "/v1/public/business", "public_read"),
    ("GET", "/v1/public/me/bookings", "public_read"), ("GET", "/v1/public/availability/month/", "public_read"),
    ("POST", "/v1/public/bookings", "public_write"), ("POST", "/v1/public/bookings/abc/cancel", "public_write"),
    ("POST", "/v1/public/bookings/abc/reschedule", "public_write"), ("PATCH", "/v1/public/me/addresses/abc", "public_write"),
    ("DELETE", "/v1/public/me/vehicles/abc", "public_write"), ("POST", "/v1/public/me/data-requests", "public_write"),
    ("POST", "/v1/public/auth/otp", "auth_send"), ("POST", "/v1/public/auth/magic-link", "auth_send"), ("POST", "/v1/public/auth/otp/", "auth_send"),
    ("POST", "/v1/public/auth/otp/verify", "auth_verify"), ("POST", "/v1/public/auth/magic-link/verify", "auth_verify"),
    ("GET", "/v1/calendar/oauth/callback", "oauth_callback"), ("GET", "/v1/calendar/oauth/callback/", "oauth_callback"), ("POST", "/v1/admin/calendar/connect", "admin"),
    ("POST", "/v1/auth/tenants", "platform"), ("POST", "/v1/auth/token", "token_exchange"), ("POST", "/v1/auth/api-keys", "admin"),
    ("GET", "/v1/auth/api-keys", "admin"), ("DELETE", "/v1/auth/api-keys/abc", "admin"), ("DELETE", "/v1/auth/api-keys/abc/", "admin"),
    ("GET", "/v1/admin/bookings", "admin"), ("PUT", "/v1/admin/operating-hours", "admin"), ("DELETE", "/v1/admin/webhooks/x", "admin"),
    ("POST", "/v1/sync/webhook", "sync"),
])
def test_requests_fall_into_the_bucket_their_cost_and_risk_suggest(method: str, path: str, bucket: str) -> None:
    assert classify(method, path) == bucket


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/healthz"), ("GET", "/docs"), ("GET", "/openapi.json"), ("GET", "/"), ("GET", "/v2/public/services"), ("GET", "/v1/unknown"),
    ("OPTIONS", "/v1/public/bookings"), ("OPTIONS", "/v1/admin/bookings"), ("OPTIONS", "/v1/public/auth/otp"),
])
def test_health_docs_preflights_and_unknown_paths_are_not_limited(method: str, path: str) -> None:
    assert classify(method, path) is None


def test_every_bucket_has_an_address_limit_and_the_costly_ones_are_tight() -> None:
    for bucket, tiers in DEFAULTS.items():
        assert "ip" in tiers, bucket
        assert all(limit.requests > 0 and limit.window_seconds > 0 for limit in tiers.values())
    assert DEFAULTS["auth_send"]["ip"].requests <= 10 and DEFAULTS["platform"]["ip"].requests <= 10
    assert DEFAULTS["public_write"]["ip"].requests < DEFAULTS["public_read"]["ip"].requests
    assert DEFAULTS["auth_send"]["tenant"].requests < DEFAULTS["public_write"]["tenant"].requests


def test_limits_can_be_overridden_or_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    assert limit_for("auth_send", "ip") == Limit(10, 60) and limit_for("nonsense", "ip") is None and limit_for("platform", "tenant") is None
    monkeypatch.setattr(get_settings(), "rate_limits", {"auth_send": {"ip": (3, 30)}, "public_read": {"ip": (0, 60), "tenant": (5, 0)}})
    assert limit_for("auth_send", "ip") == Limit(3, 30)
    assert limit_for("auth_send", "tenant") == Limit(120, 60)  # what is not overridden stays
    assert limit_for("public_read", "ip") is None and limit_for("public_read", "tenant") is None  # 0 requests / 0 seconds = off


# --------------------------------------------------------------------- pure: who is asking
@pytest.mark.parametrize(("peer", "header", "trusted", "expected"), [
    ("203.0.113.9", None, 0, "203.0.113.9"),
    ("203.0.113.9", "1.1.1.1", 0, "203.0.113.9"),  # the header means nothing without a proxy we trust
    ("10.0.0.2", "198.51.100.7", 1, "198.51.100.7"),
    ("10.0.0.2", "6.6.6.6, 198.51.100.7", 1, "198.51.100.7"),  # a forged left side is ignored
    ("10.0.0.2", "6.6.6.6, 198.51.100.7, 10.0.0.1", 2, "198.51.100.7"),
    ("10.0.0.2", "198.51.100.7", 2, "10.0.0.2"),  # fewer hops than proxies: do not trust it
    ("10.0.0.2", "not-an-ip", 1, "10.0.0.2"), ("10.0.0.2", "", 1, "10.0.0.2"), ("10.0.0.2", " , ", 1, "10.0.0.2"),
    ("10.0.0.2", "999.1.1.1", 1, "10.0.0.2"),
    (None, None, 0, "unknown"), ("garbage", None, 0, "unknown"),
    ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", None, 0, "2001:db8:1:2::/64"),
    ("2001:db8:1:2:1:2:3:4", None, 0, "2001:db8:1:2::/64"),  # one /64 is one subscriber
    ("10.0.0.2", "2001:db8:1:2:aaaa::1", 1, "2001:db8:1:2::/64"),
    ("::ffff:198.51.100.7", None, 0, "198.51.100.7"),
])
def test_the_client_address(peer: str | None, header: str | None, trusted: int, expected: str) -> None:
    assert client_ip(peer, header, trusted) == expected


# --------------------------------------------------------------------- the sliding window
class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(ratelimit, "_clock", c)
    return c


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    import fakeredis.aioredis

    from app.core import redis as redis_module

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis", fake)
    ratelimit.reset_breaker()
    yield fake
    ratelimit.reset_breaker()


async def spend(n: int, subject: str = "a", limit: Limit = Limit(10, 60), bucket: str = "b", tier: str = "ip") -> list[bool]:
    out = []
    for _ in range(n):
        decision = await hit(bucket, tier, subject, limit)
        assert decision is not None
        out.append(decision.allowed)
    return out


async def test_the_limit_is_exact_and_the_numbers_add_up(redis: Any, clock: Clock) -> None:
    limit = Limit(10, 60)
    remaining = []
    for _ in range(10):
        decision = await hit("b", "ip", "a", limit)
        assert decision is not None and decision.allowed and decision.limit == 10
        remaining.append(decision.remaining)
    assert remaining == [9, 8, 7, 6, 5, 4, 3, 2, 1, 0]
    clock.t = T0 + 25
    assert (await hit("b", "ip", "other", limit)) == ratelimit.Decision(True, 10, 9, 35)  # the window ends in 35 s
    clock.t = T0
    refused = await hit("b", "ip", "a", limit)
    assert refused is not None and (refused.allowed, refused.remaining, refused.reset, refused.retry_after) == (False, 0, 60, 60)
    clock.t = T0 + 45
    later = await hit("b", "ip", "a", limit)
    assert later is not None and later.allowed is False and later.reset == 15 and later.retry_after == 15


async def test_the_window_slides_instead_of_resetting_all_at_once(redis: Any, clock: Clock) -> None:
    assert await spend(10) == [True] * 10
    clock.t = T0 + 60  # the next window starts, but the last one still counts in full
    assert await spend(1) == [False]
    clock.t = T0 + 60 + 5.9  # 10 * (1 - 5.9/60) + 1 = 10.02 > 10
    assert await spend(1) == [False]
    clock.t = T0 + 60 + 6.1  # 10 * (1 - 6.1/60) + 1 = 9.98
    assert await spend(1) == [True]
    clock.t = T0 + 120  # only that one request is left in the previous window: 1 * 1 + 1 = 2
    assert await spend(9) == [True] * 9 and await spend(1) == [False]  # 1 + 9 = 10 is the most that fits
    clock.t = T0 + 300 + 1
    assert await spend(10) == [True] * 10  # two windows of quiet: everything is forgotten


async def test_refused_requests_do_not_use_up_quota(redis: Any, clock: Clock) -> None:
    assert await spend(10) == [True] * 10
    assert await spend(50) == [False] * 50  # hammering while refused...
    clock.t = T0 + 60 + 6.1
    assert await spend(1) == [True]  # ...does not push the recovery back: it comes on the same schedule as for a polite client
    assert int(await redis.get(f"rl:b:ip:a:{int(T0 // 60)}")) == 10


async def test_subjects_buckets_and_tiers_are_counted_separately(redis: Any, clock: Clock) -> None:
    assert await spend(10, "a") == [True] * 10 and await spend(1, "a") == [False]
    assert await spend(1, "b") == [True]  # another address
    assert await spend(1, "a", bucket="c") == [True]  # another bucket
    assert await spend(1, "a", tier="tenant") == [True]  # another tier
    assert await spend(1, "a", limit=Limit(10, 30)) == [True]  # another window length is another key space... but not another subject:
    assert await redis.keys("rl:b:ip:a:*") != []


async def test_a_short_window_and_a_small_limit(redis: Any, clock: Clock) -> None:
    limit = Limit(2, 10)
    assert await spend(3, limit=limit) == [True, True, False]
    clock.t = T0 + 10 + 5.1
    assert await spend(1, limit=limit) == [True]  # 2 * (1 - .51) + 1 = 1.98


async def test_counters_expire_on_their_own(redis: Any, clock: Clock) -> None:
    await spend(3)
    (key,) = await redis.keys("rl:*")
    assert 60 < await redis.ttl(key) <= 120  # long enough to serve as "previous" for the whole next window, then gone


async def test_redis_trouble_lets_requests_through_and_is_not_retried_every_time(monkeypatch: pytest.MonkeyPatch, clock: Clock, redis: Any) -> None:
    calls = {"n": 0}

    class Broken:
        async def get(self, *a: Any) -> None:
            calls["n"] += 1
            from redis.exceptions import ConnectionError as RedisConnectionError

            raise RedisConnectionError("down")

    monkeypatch.setattr(ratelimit, "get_redis", lambda: Broken())
    assert await hit("b", "ip", "a", Limit(1, 60)) is None  # the caller lets the request through
    assert await hit("b", "ip", "a", Limit(1, 60)) is None and calls["n"] == 1  # and did not even try again
    clock.t = T0 + ratelimit.BREAKER_SECONDS + 1
    assert await hit("b", "ip", "a", Limit(1, 60)) is None and calls["n"] == 2  # until the pause is over
    ratelimit.reset_breaker()
    monkeypatch.undo()


# --------------------------------------------------------------------- through the API
@pytest.fixture
def limited(monkeypatch: pytest.MonkeyPatch, redis: Any, clock: Clock) -> Clock:
    """Rate limiting on, with a clock the test controls and an in-memory Redis."""
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    monkeypatch.setattr(get_settings(), "rate_limits", {})
    monkeypatch.setattr(get_settings(), "rate_limit_trusted_proxies", 0)
    return clock


def limits(monkeypatch: pytest.MonkeyPatch, **buckets: dict[str, tuple[int, int]]) -> None:
    monkeypatch.setattr(get_settings(), "rate_limits", buckets)


async def get_services(s: Seed, **headers: str) -> httpx.Response:
    return await s.client.get("/v1/public/services", headers={**s.pk, **headers})


async def test_the_address_limit_answers_429_with_retry_information(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, public_read={"ip": (3, 60)})
    seen = []
    for _ in range(3):
        r = await get_services(seeded)
        assert r.status_code == 200
        seen.append((r.headers["x-ratelimit-limit"], r.headers["x-ratelimit-remaining"], r.headers["x-ratelimit-reset"]))
    assert seen == [("3", "2", "60"), ("3", "1", "60"), ("3", "0", "60")]
    refused = await get_services(seeded)
    assert refused.status_code == 429 and refused.json() == {"error": "rate_limited", "detail": "Too many requests; try again in 60 seconds", "retry_after": 60}
    assert refused.headers["retry-after"] == "60" and refused.headers["x-ratelimit-remaining"] == "0" and refused.headers["x-ratelimit-limit"] == "3"
    limited.t = T0 + 100  # 40 s into the next window: 3 * (1 - 40/60) + 1 = 2
    again = await get_services(seeded)
    assert again.status_code == 200


async def test_a_flood_of_made_up_keys_is_stopped_before_the_database(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, public_read={"ip": (2, 60)})
    codes = [(await seeded.client.get("/v1/public/services", headers={"X-Site-Key": f"pk_made_up_{n}"})).status_code for n in range(5)]
    assert codes == [401, 401, 429, 429, 429]  # the third never got as far as looking the key up
    assert (await seeded.client.get("/v1/public/services")).status_code == 429  # nor does a missing one


async def test_health_checks_docs_and_preflights_are_never_limited(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, public_read={"ip": (1, 60)}, public_write={"ip": (1, 60)}, admin={"ip": (1, 60)})
    for _ in range(5):
        assert (await seeded.client.get("/healthz")).status_code == 200
        assert (await seeded.client.get("/docs")).status_code == 200
        assert (await seeded.client.options("/v1/public/bookings", headers={"Origin": "https://x.example", "Access-Control-Request-Method": "POST"})).status_code != 429
    r = await get_services(seeded)
    assert r.status_code == 200 and "x-ratelimit-limit" in r.headers
    assert "x-ratelimit-limit" not in (await seeded.client.get("/healthz")).headers


async def test_addresses_are_counted_separately_and_a_forged_header_does_not_help(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, public_read={"ip": (2, 60)})
    for n in range(5):  # without a trusted proxy the header is ignored: one peer, one limit
        r = await get_services(seeded, **{"X-Forwarded-For": f"9.9.9.{n}"})
        assert r.status_code == (200 if n < 2 else 429), n
    monkeypatch.setattr(get_settings(), "rate_limit_trusted_proxies", 1)
    limited.t = T0 + 600
    for who, expected in (("198.51.100.1", [200, 200, 429]), ("198.51.100.2", [200, 200, 429])):
        got = [(await get_services(seeded, **{"X-Forwarded-For": f"6.6.6.6, {who}"})).status_code for _ in range(3)]
        assert got == expected, who  # the proxy-appended entry decides, not the forged one on the left


async def test_the_business_limit_is_shared_by_all_its_addresses_and_not_by_other_businesses(
    client: httpx.AsyncClient, limited: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await make_seed(client), await make_seed(client)
    limits(monkeypatch, public_read={"ip": (100, 60), "tenant": (4, 60)})
    monkeypatch.setattr(get_settings(), "rate_limit_trusted_proxies", 1)
    codes = [(await get_services(a, **{"X-Forwarded-For": f"198.51.100.{n}"})).status_code for n in range(6)]
    assert codes == [200, 200, 200, 200, 429, 429]  # six different visitors, one business
    refused = await get_services(a, **{"X-Forwarded-For": "198.51.100.77"})
    assert refused.status_code == 429 and refused.headers["retry-after"] and refused.json()["error"] == "rate_limited"
    assert (await get_services(b, **{"X-Forwarded-For": "198.51.100.1"})).status_code == 200  # another business is untouched


async def test_asking_for_sign_in_codes_is_tightly_limited_and_nothing_is_sent_beyond_it(
    seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch, otp: Any, mail: Recorder
) -> None:
    limits(monkeypatch, auth_send={"ip": (2, 60)})
    codes = [(await request_code(seeded, f"+601200000{n:02d}")).status_code for n in range(4)]
    assert codes == [202, 202, 429, 429]
    assert len(mail.sent) == 2  # the refused requests cost nothing: no code was made and no message paid for
    assert len(await otp.keys("otp:*")) >= 2 and not [k for k in await otp.keys("otp:*") if k.endswith("00002") or "0000002" in k]


async def test_magic_links_share_the_sending_limit_but_verifying_has_its_own(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch, otp: Any, mail: Recorder) -> None:
    limits(monkeypatch, auth_send={"ip": (2, 60)}, auth_verify={"ip": (3, 60)})
    await booked_with_email(seeded)
    assert [(await ask(seeded, f"nobody{n}@example.com")).status_code for n in range(3)] == [202, 202, 429]
    codes = [(await seeded.client.post("/v1/public/auth/otp/verify", json={"phone": PHONE, "code": "000000"}, headers=seeded.pk)).status_code for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]  # wrong guesses are limited per address on top of the per-phone attempt cap


async def test_booking_and_the_other_writes_have_a_lower_limit_than_reading(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, public_write={"ip": (2, 60)})
    mei = await seeded.add_staff("Mei")
    codes = []
    for hour in (9, 10, 11):
        r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, hour), mei), headers=seeded.pk)
        codes.append(r.status_code)
    assert codes == [201, 201, 429]
    assert (await get_services(seeded)).status_code == 200  # reading is a different bucket
    listing = await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)
    assert listing.status_code == 200 and len(listing.json()) == 2  # and the refused booking was never made


async def test_the_admin_api_has_a_ceiling_per_business(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, admin={"ip": (100, 60), "tenant": (3, 60)})
    token_calls = [(await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)).status_code for _ in range(5)]
    assert token_calls == [200, 200, 200, 429, 429]
    refused = await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)
    assert refused.headers["retry-after"] and refused.json()["error"] == "rate_limited"


async def test_creating_businesses_is_limited_before_the_platform_token_is_even_checked(client: httpx.AsyncClient, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, platform={"ip": (2, 60)})
    wrong = {"X-Platform-Token": "not-the-token-0123456789"}
    codes = [(await client.post("/v1/auth/tenants", json={"name": "X"}, headers=wrong)).status_code for _ in range(4)]
    assert codes == [401, 401, 429, 429]
    limited.t = T0 + 600
    assert (await client.post("/v1/auth/tenants", json={"name": "X"}, headers=PLATFORM)).status_code == 201


async def test_token_exchange_has_its_own_bucket_per_business(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, token_exchange={"ip": (100, 60), "tenant": (2, 60)})
    codes = [(await seeded.client.post("/v1/auth/token", json={"user_id": "wp-1", "role": "owner"}, headers=seeded.sk)).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


async def test_switching_the_limiter_off_switches_it_off(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch, redis: Any) -> None:
    limits(monkeypatch, public_read={"ip": (1, 60), "tenant": (1, 60)})
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", False)
    for _ in range(5):
        r = await get_services(seeded)
        assert r.status_code == 200 and "x-ratelimit-limit" not in r.headers
    assert await redis.keys("rl:*") == []  # and Redis was not even touched


async def test_a_redis_outage_does_not_stop_the_api(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    limits(monkeypatch, public_read={"ip": (1, 60)})

    class Broken:
        async def get(self, *a: Any) -> None:
            raise OSError("redis is gone")

    monkeypatch.setattr(ratelimit, "get_redis", lambda: Broken())
    for _ in range(4):
        r = await get_services(seeded)
        assert r.status_code == 200 and "x-ratelimit-limit" not in r.headers


async def test_rate_limit_responses_pass_through_the_cors_layer() -> None:
    """The limiter sits inside the CORS middleware, so a browser can read the 429 instead of seeing an opaque network error (and both sit inside the request-id layer)."""
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.middleware.base import BaseHTTPMiddleware

    from app.core.observability import RequestContextMiddleware
    from app.main import app

    outermost_first = [m.cls for m in app.user_middleware]
    assert outermost_first == [RequestContextMiddleware, CORSMiddleware, BaseHTTPMiddleware]


async def test_other_errors_keep_their_shape_and_get_no_extra_headers(seeded: Seed, limited: Clock) -> None:
    r = await seeded.client.get("/v1/public/services", headers={"X-Site-Key": "pk_nope"})
    assert r.status_code == 401 and set(r.json()) == {"error", "detail"} and "retry-after" not in r.headers
    assert uuid.uuid4()


def test_a_refusal_never_says_to_retry_in_zero_seconds() -> None:
    assert ratelimit.Decision(False, 10, 0, 0).retry_after == 1
    assert ratelimit.Decision(False, 10, 0, 37).retry_after == 37


async def test_a_route_needing_two_role_checks_is_counted_once(seeded: Seed, limited: Clock, monkeypatch: pytest.MonkeyPatch) -> None:
    """Manager-level admin routes pass the router's staff check and their own manager check; the business is charged one request, not two."""
    limits(monkeypatch, admin={"ip": (100, 60), "tenant": (3, 60)})
    codes = [(await seeded.client.get("/v1/admin/notification-templates", headers=seeded.admin)).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
