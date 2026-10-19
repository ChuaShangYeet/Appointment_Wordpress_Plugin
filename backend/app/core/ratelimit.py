"""Rate limiting (blueprint §16: "gateway rate limiting on public endpoints"), enforced in the application as defence in depth.

Two tiers, both counted in Redis with a **sliding-window counter** (the current fixed window's count plus the previous window's,
weighted by how much of it still overlaps - smooth at window edges, two counters per key, no scripting needed):

* **per client address**, in a middleware that runs *before* any database work - so a flood of requests with made-up API keys
  costs a Redis round trip, not a query. Tight on what can be abused or costs money (sending a sign-in code, creating tenants, booking), generous on reads.
* **per business**, once the API key has identified it - fair use, and a ceiling on runaway scripts or a leaked publishable key (OTP messages cost money).

A request is classified into a *bucket* from its method and path (:func:`classify`); each bucket has a limit per tier (:data:`DEFAULTS`,
overridable with ``RATE_LIMITS``, e.g. ``{"auth_send": {"ip": [5, 60]}}`` = 5 per 60 s; ``[0, 60]`` switches that limit off). A refused request
gets ``429`` with ``Retry-After`` and ``X-RateLimit-*`` headers; an accepted one on a limited route gets the per-address headers. Refused requests do
not use up quota, so a client that backs off recovers on schedule.

**The client address** is the socket peer unless ``RATE_LIMIT_TRUSTED_PROXIES`` says how many proxies sit in front, in which case it is the
``X-Forwarded-For`` entry that many hops from the right (the left side is client-controlled and ignored); IPv6 clients are grouped by /64, since one
subscriber normally holds a whole /64.

**Redis trouble never takes the API down**: errors let the request through (fail open), and Redis is not retried on every request for a few seconds.
What this does *not* do: limit by user or token, daily quotas, or protect against a distributed flood (that is the gateway's job).
"""
from __future__ import annotations

import ipaddress
import logging
import math
import time
import uuid
from dataclasses import dataclass

from redis.exceptions import RedisError
from starlette.requests import Request

from app.core.config import get_settings
from app.core.exceptions import TooManyRequests
from app.core.redis import get_redis

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Limit:
    requests: int
    window_seconds: int


#: bucket -> tier -> limit. ``ip`` is per client address, ``tenant`` per business.
DEFAULTS: dict[str, dict[str, Limit]] = {
    "platform": {"ip": Limit(10, 60)},  # creating tenants and changing their plans
    "signup": {"ip": Limit(5, 3600)},  # asking to set a business up: sends an email, and creates a business once confirmed
    "signup_verify": {"ip": Limit(20, 600)},
    "owner_login": {"ip": Limit(10, 3600)},  # asking for a sign-in link: it sends an email
    "owner_login_verify": {"ip": Limit(30, 600)},
    "oauth_callback": {"ip": Limit(30, 600)},  # where Google sends the owner's browser back; each use costs a token exchange
    "token_exchange": {"ip": Limit(300, 60), "tenant": Limit(120, 60)},  # WordPress -> admin token
    "auth_send": {"ip": Limit(10, 60), "tenant": Limit(120, 60)},  # sends a code / link: costs money, can be used to pester people
    "auth_verify": {"ip": Limit(30, 60), "tenant": Limit(600, 60)},
    "geocode": {"ip": Limit(10, 60), "tenant": Limit(120, 60)},  # every call is a paid lookup at the geocoding provider
    "public_write": {"ip": Limit(30, 60), "tenant": Limit(600, 60)},  # booking, cancelling, rescheduling, reviews, saved addresses...
    "public_read": {"ip": Limit(300, 60), "tenant": Limit(6000, 60)},
    "admin": {"ip": Limit(600, 60), "tenant": Limit(1200, 60)},
    "sync": {"ip": Limit(300, 60), "tenant": Limit(600, 60)},
}
BREAKER_SECONDS = 10.0

_AUTH_SEND = {"/v1/public/auth/otp", "/v1/public/auth/magic-link"}
_AUTH_VERIFY = {"/v1/public/auth/otp/verify", "/v1/public/auth/magic-link/verify"}


def classify(method: str, path: str) -> str | None:
    """The bucket a request counts against; ``None`` = not limited (health check, docs, CORS preflight, unknown paths)."""
    if method == "OPTIONS":
        return None
    path = path.rstrip("/") or "/"
    if path == "/v1/auth/tenants" or path.startswith("/v1/platform/"):
        return "platform"
    if path == "/v1/auth/token":
        return "token_exchange"
    if path == "/v1/signup":
        return "signup"
    if path == "/v1/signup/verify":
        return "signup_verify"
    if path == "/v1/owner/login":
        return "owner_login"
    if path == "/v1/owner/login/verify":
        return "owner_login_verify"
    if path == "/v1/calendar/oauth/callback":
        return "oauth_callback"
    if path in _AUTH_SEND:
        return "auth_send"
    if path in _AUTH_VERIFY:
        return "auth_verify"
    if path == "/v1/public/geocode":
        return "geocode"
    if path.startswith("/v1/public/") or path == "/v1/public":
        return "public_read" if method in ("GET", "HEAD") else "public_write"
    if path.startswith("/v1/admin/") or path == "/v1/admin" or path == "/v1/auth/api-keys" or path.startswith("/v1/auth/api-keys/"):
        return "admin"
    if path.startswith("/v1/sync/"):
        return "sync"
    return None


def limit_for(bucket: str, tier: str) -> Limit | None:
    """The limit in force (defaults, then ``RATE_LIMITS``); ``None`` when there is none or it is switched off with 0 requests."""
    override = get_settings().rate_limits.get(bucket, {}).get(tier)
    limit = Limit(override[0], override[1]) if override is not None else DEFAULTS.get(bucket, {}).get(tier)
    return limit if limit and limit.requests > 0 and limit.window_seconds > 0 else None


# --------------------------------------------------------------------------- who is asking
def client_ip(peer: str | None, forwarded_for: str | None, trusted_proxies: int) -> str:
    """The client's address for limiting (see the module notes). Never raises: anything unusable falls back to the socket peer."""
    candidate = peer
    if trusted_proxies > 0 and forwarded_for:
        hops = [h.strip() for h in forwarded_for.split(",") if h.strip()]
        if len(hops) >= trusted_proxies:
            candidate = hops[-trusted_proxies]
    try:
        ip = ipaddress.ip_address(candidate or "")
    except ValueError:
        try:
            ip = ipaddress.ip_address(peer or "")
        except ValueError:
            return "unknown"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False).network_address) + "/64"
    return str(ip)


def request_ip(request: Request) -> str:
    peer = request.client.host if request.client else None
    return client_ip(peer, request.headers.get("x-forwarded-for"), get_settings().rate_limit_trusted_proxies)


# --------------------------------------------------------------------------- counting
@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    #: Seconds until the current window ends (an upper bound on the wait after a refusal).
    reset: int

    @property
    def retry_after(self) -> int:
        return max(1, self.reset)


_down_until = 0.0


def reset_breaker() -> None:
    global _down_until
    _down_until = 0.0


def _clock() -> float:
    return time.time()


async def hit(bucket: str, tier: str, subject: str, limit: Limit) -> Decision | None:
    """Count one request of ``subject`` against ``limit``; ``None`` if Redis could not be used (the request is then let through)."""
    global _down_until
    now = _clock()
    if now < _down_until:
        return None
    window = limit.window_seconds
    index, elapsed = divmod(now, window)
    base = f"rl:{bucket}:{tier}:{subject}:"
    current_key, previous_key = f"{base}{int(index)}", f"{base}{int(index) - 1}"
    fraction = elapsed / window
    try:
        redis = get_redis()
        previous = int(await redis.get(previous_key) or 0)
        current = int(await redis.incr(current_key))
        if current == 1:
            await redis.expire(current_key, window * 2)  # long enough to serve as "previous" for the whole next window
        weighted = previous * (1 - fraction) + current
        if weighted > limit.requests:
            await redis.decr(current_key)  # refused requests do not use up quota
            return Decision(False, limit.requests, 0, math.ceil(window - elapsed))
        return Decision(True, limit.requests, max(0, limit.requests - math.ceil(weighted)), math.ceil(window - elapsed))
    except (RedisError, OSError, ValueError) as exc:
        _down_until = now + BREAKER_SECONDS
        log.error("rate limiter unavailable, letting requests through for %.0fs: %s", BREAKER_SECONDS, type(exc).__name__)
        return None


async def daily_cap(key: str, cap: int) -> bool:
    """Count one more use of ``key`` in its day (24 hours from the first); ``False`` once ``cap`` is passed. ``cap`` 0 means no cap.

    The per-minute limits and cooldowns say how fast; this says how many in all, so that one person's phone or mailbox cannot be filled over a day.
    """
    if cap <= 0:
        return True
    redis = get_redis()
    await redis.set(key, 0, ex=86400, nx=True)
    return int(await redis.incr(key)) <= cap


async def refund_daily(key: str) -> None:
    """Take back the use :func:`daily_cap` counted, because the message it was for was never sent (a failed send must not use up the person's day)."""
    redis = get_redis()
    if await redis.decr(key) <= 0:
        await redis.delete(key)  # nothing counted today: no key left behind


class RateLimited(TooManyRequests):
    code = "rate_limited"

    def __init__(self, decision: Decision) -> None:
        super().__init__(
            f"Too many requests; try again in {decision.retry_after} seconds",
            extra={"retry_after": decision.retry_after}, headers=headers_for(decision, refused=True),
        )


def headers_for(decision: Decision, *, refused: bool = False) -> dict[str, str]:
    headers = {
        "X-RateLimit-Limit": str(decision.limit), "X-RateLimit-Remaining": str(decision.remaining), "X-RateLimit-Reset": str(decision.reset),
    }
    if refused:
        headers["Retry-After"] = str(decision.retry_after)
    return headers


async def check_ip(request: Request, bucket: str) -> Decision | None:
    limit = limit_for(bucket, "ip")
    if limit is None:
        return None
    return await hit(bucket, "ip", request_ip(request), limit)


async def enforce_tenant(request: Request, tenant_id: uuid.UUID) -> None:
    """Per-business limit for the request's bucket; raises :class:`RateLimited`. Called once the API key has identified the business."""
    settings = get_settings()
    if not settings.rate_limit_enabled:
        return
    bucket = classify(request.method, request.url.path)
    if bucket is None:
        return
    limit = limit_for(bucket, "tenant")
    if limit is None:
        return
    if getattr(request.state, "rate_tenant_checked", False):  # the same request can pass two auth dependencies
        return
    request.state.rate_tenant_checked = True
    decision = await hit(bucket, "tenant", str(tenant_id), limit)
    if decision is not None and not decision.allowed:
        raise RateLimited(decision)

