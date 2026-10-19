"""Seeing what the running service does: a request id on every request, one access-log line per request, a clean 500 for the unexpected, and Sentry.

* **Request id.** The id in the caller's ``X-Request-ID`` header is kept when it is a plain 8-64 character token (so a proxy's id flows through),
  otherwise a new one is made. It is on every log line written during the request (``app.core.logs``) and on the response (``X-Request-ID``), so
  "request abc123 failed" in a support email finds the lines.
* **Access log** (logger ``app.access``): method, path, route, status, milliseconds, client address, business. Never the query string, headers or body
  (those carry tokens and personal data). 401, 403 and 429 are warnings; the health checks are debug lines.
* **Unexpected errors.** Anything that is not a deliberate domain error ends as a logged traceback (level ERROR) and a 500 whose body names the request id and
  nothing else - no exception text, which could hold SQL or a secret.
* **Sentry** (:func:`init_sentry`): only when ``SENTRY_DSN`` is set. ERROR-level log lines (this includes every unexpected error and each "giving up" in
  the code) become Sentry events. No request data, headers, cookies, local variables or personal data are sent, and :func:`scrub_event` redacts the text
  that is. Not wired in: tracing/performance monitoring, metrics, OpenTelemetry.
"""
from __future__ import annotations

import logging
import re
import time
import uuid
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core import ratelimit
from app.core.config import Settings, get_settings
from app.core.logs import REDACTED, redact, request_id_var, tenant_id_var

log = logging.getLogger(__name__)
access_log = logging.getLogger("app.access")

REQUEST_ID_HEADER = "X-Request-ID"
_ACCEPTABLE_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
_QUIET_PATHS = frozenset({"/healthz", "/readyz"})
_WARNED = frozenset({401, 403, 429})
_MAX_PATH = 200
#: The answers are JSON for programs, never pages and never to be stored by a shared cache (they hold one customer's data, or a token).
_SAFETY_HEADERS = {
    "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
_DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})  # (the documentation page needs its own scripts and styles)


class RequestContextMiddleware:
    """Outermost middleware: request id, access log, and the 500 for anything unexpected."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        request_id = incoming if incoming and _ACCEPTABLE_ID.match(incoming) else uuid.uuid4().hex
        id_token, tenant_token = request_id_var.set(request_id), tenant_id_var.set(None)
        started = time.perf_counter()
        status = 500
        began = False

        async def send_with_id(message: Message) -> None:
            nonlocal status, began
            if message["type"] == "http.response.start":
                status, began = message["status"], True
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                if scope["path"] not in _DOCS_PATHS:
                    for name, value in _SAFETY_HEADERS.items():
                        headers.setdefault(name, value)
                    if _over_https(scope):
                        headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            await send(message)

        method, path = scope["method"], scope["path"]
        try:
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                log.exception("unhandled error serving %s %s", method, path[:_MAX_PATH])
                if began:  # too late for a clean answer: let the server drop the connection
                    raise
                await JSONResponse(
                    status_code=500,
                    content={"error": "internal_error", "detail": "Something went wrong on our side; quote the request id if you contact support", "request_id": request_id},
                )(scope, receive, send_with_id)
            self._log_access(scope, method, path, status, started)
        finally:
            request_id_var.reset(id_token)
            tenant_id_var.reset(tenant_token)

    @staticmethod
    def _log_access(scope: Scope, method: str, path: str, status: int, started: float) -> None:
        milliseconds = round((time.perf_counter() - started) * 1000)
        route = None  # the path with each id replaced by its name ("/v1/public/bookings/{booking_id}"): the same for every booking
        if scope.get("route") is not None:
            route = path
            for name, value in scope.get("path_params", {}).items():
                route = route.replace(str(value), "{" + name + "}", 1)
        tenant_id = scope.get("state", {}).get("tenant_id")
        level = logging.DEBUG if path in _QUIET_PATHS else logging.WARNING if status in _WARNED or status >= 500 else logging.INFO
        access_log.log(
            level, "%s %s %d %dms", method, path[:_MAX_PATH], status, milliseconds,
            extra={"fields": {
                "method": method, "path": path[:_MAX_PATH], "route": route, "status": status, "duration_ms": milliseconds,
                "client": ratelimit.request_ip(Request(scope)), "tenant": str(tenant_id) if tenant_id else None,
            }},
        )


def _over_https(scope: Scope) -> bool:
    """Did the client reach us over TLS? Behind a proxy that is its word (``X-Forwarded-Proto``), believed only where proxies are declared."""
    if scope.get("scheme") in ("https", "wss"):
        return True
    return get_settings().rate_limit_trusted_proxies > 0 and Headers(scope=scope).get("x-forwarded-proto", "").split(",")[-1].strip().lower() == "https"


# --------------------------------------------------------------------------- Sentry
def scrub_event(event: Any, hint: Any = None) -> Any:
    """Sentry's ``before_send``: redact every text in the event, drop any request data, and tag it with the request and business it came from."""
    event.pop("request", None)
    event.pop("user", None)
    event = _scrub(event)
    tags = event.setdefault("tags", {})
    if (request_id := request_id_var.get()) is not None:
        tags["request_id"] = request_id
    if (tenant_id := tenant_id_var.get()) is not None:
        tags["tenant_id"] = tenant_id
    return event


def _scrub(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: (REDACTED if isinstance(k, str) and k.lower() in _SECRET_NAMES else _scrub(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v) for v in value]
    return value


_SECRET_NAMES = frozenset({"authorization", "cookie", "x-api-key", "x-site-key", "x-platform-token", "x-captcha-token", "password", "token", "secret", "api_key"})


def init_sentry(settings: Settings, **options: Any) -> bool:
    """Start reporting errors to Sentry when ``SENTRY_DSN`` is set; ``False`` (and nothing happens) otherwise or when the library is missing."""
    dsn = settings.sentry_dsn.get_secret_value()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        log.error("SENTRY_DSN is set but the sentry-sdk package is not installed: errors are not being reported")
        return False
    sentry_sdk.init(
        dsn=dsn, environment=settings.environment, send_default_pii=False, include_local_variables=False, max_request_body_size="never",
        auto_enabling_integrations=False, integrations=[LoggingIntegration(level=None, event_level=logging.ERROR)],
        before_send=scrub_event, before_breadcrumb=lambda crumb, hint: _scrub(crumb), **options,
    )
    return True
