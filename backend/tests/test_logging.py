"""Logging and error reporting: secrets are scrubbed from every line, every request has an id that reaches its log lines, an unexpected error is a clean 500,
and Sentry (when switched on) receives scrubbed errors only (app.core.logs, app.core.observability)."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import worker
from app.core import logs, observability
from app.core.config import Settings, get_settings
from app.core.security import issue_jwt
from app.services.sweep import SweepReport
from tests.conftest import Seed

GOOGLE_KEY = "AIzaSyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY"
SECRET_KEY = "sk_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk0"


# --------------------------------------------------------------------- redaction
@pytest.mark.parametrize("text, secret", [
    (f"calling https://maps.googleapis.com/maps/api/geocode/json?address=x&key={GOOGLE_KEY}&region=my", GOOGLE_KEY),
    (f"HTTP Request: GET https://maps.example.test/?api_key={GOOGLE_KEY[:20]}ZZ HTTP/1.1", GOOGLE_KEY[:20]),
    (f"Authorization: Bearer {JWT}", JWT),
    (f"token expired: {JWT}", JWT),
    ("bearer abc.def-ghi_jkl123", "abc.def-ghi_jkl123"),
    (f"X-API-Key: {SECRET_KEY}", SECRET_KEY),
    (f"key {SECRET_KEY} was revoked", SECRET_KEY),
    ("x-platform-token=tZ9pLm4QxV7rKc2WbN8d", "tZ9pLm4QxV7rKc2WbN8d"),
    ("password=hunter2hunter2&user=bob", "hunter2hunter2"),
    ("GET /lookup?address=x&key=plainvalue123&region=my", "plainvalue123"),
    ("callback ?token=Zm9vYmFy&x=1", "Zm9vYmFy"),
    ("secret=s3cr3t-value", "s3cr3t-value"),
    ("connect failed: postgresql+asyncpg://booking_app:s3cr3t-Pw@db.internal:5432/booking", "s3cr3t-Pw"),
    ("redis://:ignored@h:6379 and redis://user:p4ss@h:6379", "p4ss"),
    ("Signature=deadbeefcafe0123", "deadbeefcafe0123"),
])
def test_secrets_are_removed_from_text(text: str, secret: str) -> None:
    out = logs.redact(text)
    assert secret not in out and logs.REDACTED in out


def test_what_is_not_a_secret_is_left_alone() -> None:
    for text in (
        "booking 7f3a confirmed for 2026-10-08 10:00", "sweep: 3 tenants, 0 failed", "cache key clearing tenant", "monkey=banana",
        "GET /v1/public/bookings/7a0e5b7e-8a60-4c8e-9d3e-1d2f3a4b5c6d 200 12ms", "postgresql+asyncpg://booking_app@db.internal:5432/booking",
        "keyboard=azerty", "the token was refused", "sk_short", "AIza-short",
    ):
        assert logs.redact(text) == text


def test_redaction_keeps_the_name_of_what_was_hidden() -> None:
    assert logs.redact("api_key=abc123xyz&region=my") == f"api_key={logs.REDACTED}&region=my"
    assert logs.redact("Authorization: Bearer abc") == f"Authorization: {logs.REDACTED}"
    assert logs.redact("sent Bearer abc123 to the server") == f"sent Bearer {logs.REDACTED} to the server"
    assert logs.redact("postgresql://u:pw@h/db") == f"postgresql://u:{logs.REDACTED}@h/db"


# --------------------------------------------------------------------- formats
def record(message: str = "hello", *args: Any, level: int = logging.INFO, exc: BaseException | None = None, **extra: Any) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    made = logging.LogRecord("app.test", level, __file__, 1, message, args or None, exc_info)
    for name, value in extra.items():
        setattr(made, name, value)
    return made


def test_json_line_has_the_standard_fields() -> None:
    line = json.loads(logs.JsonFormatter().format(record("sweep %s done", "A", level=logging.WARNING)))
    assert line["level"] == "warning" and line["logger"] == "app.test" and line["message"] == "sweep A done"
    assert line["time"].endswith("+00:00") and "request_id" not in line and "tenant_id" not in line


def test_json_line_carries_the_request_and_the_business() -> None:
    rid = logs.request_id_var.set("req-12345678")
    tid = logs.tenant_id_var.set("t-1")
    try:
        line = json.loads(logs.JsonFormatter().format(record()))
    finally:
        logs.request_id_var.reset(rid)
        logs.tenant_id_var.reset(tid)
    assert line["request_id"] == "req-12345678" and line["tenant_id"] == "t-1"


def test_json_line_is_scrubbed_everywhere() -> None:
    try:
        raise RuntimeError(f"upstream said key={GOOGLE_KEY}")
    except RuntimeError as exc:
        text = logs.JsonFormatter().format(record(f"failed with {SECRET_KEY}", exc=exc, fields={"url": f"https://x.test/?token=abc12345", "n": 3}))
    assert GOOGLE_KEY not in text and SECRET_KEY not in text and "abc12345" not in text
    line = json.loads(text)
    assert "RuntimeError" in line["exception"] and line["n"] == 3 and "\n" not in text


def test_extra_fields_cannot_overwrite_the_standard_ones() -> None:
    line = json.loads(logs.JsonFormatter().format(record("real", fields={"message": "fake", "level": "debug", "extra": 1})))
    assert line["message"] == "real" and line["level"] == "info" and line["extra"] == 1


def test_json_line_survives_values_json_cannot_hold() -> None:
    line = json.loads(logs.JsonFormatter().format(record(fields={"id": uuid.UUID(int=1), "set": {1}})))
    assert line["id"] == "00000000-0000-0000-0000-000000000001"


def test_text_line_is_scrubbed_and_shows_the_request() -> None:
    token = logs.request_id_var.set("req-abcdefgh")
    try:
        out = logs.TextFormatter().format(record(f"Bearer {JWT}"))
    finally:
        logs.request_id_var.reset(token)
    assert JWT not in out and out.endswith("[req-abcdefgh]") and "INFO app.test:" in out


@pytest.mark.parametrize("fmt, env, expected", [
    ("auto", "development", "text"), ("auto", "test", "text"), ("auto", "production", "json"), ("auto", "staging", "json"),
    ("json", "development", "json"), ("text", "production", "text"),
])
def test_format_follows_the_setting_then_the_environment(fmt: str, env: str, expected: str) -> None:
    assert logs.resolve_format(Settings(_env_file=None, log_format=fmt, environment=env)) == expected  # type: ignore[call-arg,arg-type]


def test_defaults_are_the_safe_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENVIRONMENT")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.environment == "production" and s.log_format == "auto" and s.log_level == "INFO"
    assert s.sentry_dsn.get_secret_value() == ""
    assert "https://k@o.example" not in repr(Settings(_env_file=None, sentry_dsn="https://k@o.example/1"))  # type: ignore[call-arg]  # (the DSN carries a key)


@pytest.fixture
def configured() -> Iterator[io.StringIO]:
    """Our handler installed on the root logger, writing to a buffer; everything put back afterwards."""
    root = logging.getLogger()
    saved = {n: (logging.getLogger(n).level, logging.getLogger(n).propagate, list(logging.getLogger(n).handlers))
             for n in ("", "httpx", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access")}
    out = io.StringIO()
    logs.configure_logging("INFO", "json", stream=out)
    try:
        yield out
    finally:
        logs.unconfigure_logging()
        root.setLevel(saved[""][0])
        for name, (level, propagate, handlers) in saved.items():
            if name:
                lg = logging.getLogger(name)
                lg.setLevel(level)
                lg.propagate = propagate
                lg.handlers = handlers


def test_configure_writes_one_json_line_per_event(configured: io.StringIO) -> None:
    logging.getLogger("app.thing").info("started %d", 3)
    logging.getLogger("app.thing").debug("too chatty")
    (line,) = configured.getvalue().splitlines()
    assert json.loads(line)["message"] == "started 3"


def test_configure_twice_does_not_double_every_line(configured: io.StringIO) -> None:
    logs.configure_logging("INFO", "json", stream=configured)
    logging.getLogger("app.thing").info("once")
    assert len(configured.getvalue().splitlines()) == 1
    assert [h.get_name() for h in logging.getLogger().handlers].count(logs.HANDLER_NAME) == 1


def test_the_level_setting_is_honoured() -> None:
    out = io.StringIO()
    try:
        logs.configure_logging("WARNING", "json", stream=out)
        logging.getLogger("app.thing").info("quiet")
        logging.getLogger("app.thing").warning("loud")
        assert [json.loads(x)["message"] for x in out.getvalue().splitlines()] == ["loud"]
    finally:
        logs.unconfigure_logging()
        logging.getLogger().setLevel(logging.WARNING)


def test_libraries_that_print_urls_are_kept_quiet(configured: io.StringIO) -> None:
    # httpx logs every request at INFO, with the whole URL: for Google that includes the API key
    logging.getLogger("httpx").info('HTTP Request: GET https://maps.googleapis.com/x?key=%s "HTTP/1.1 200 OK"', GOOGLE_KEY)
    logging.getLogger("httpcore.connection").debug("connect %s", GOOGLE_KEY)
    assert configured.getvalue() == ""
    logging.getLogger("httpx").warning("trouble at https://maps.googleapis.com/x?key=%s", GOOGLE_KEY)
    assert GOOGLE_KEY not in configured.getvalue() and "trouble" in configured.getvalue()


def test_uvicorns_own_lines_go_through_ours(configured: io.StringIO) -> None:
    logging.getLogger("uvicorn.error").info("Application startup complete.")
    logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET /v1/x HTTP/1.1" 200')  # replaced by the request log
    (line,) = configured.getvalue().splitlines()
    assert json.loads(line)["logger"] == "uvicorn.error"


# --------------------------------------------------------------------- the middleware, on a small app
def small_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(observability.RequestContextMiddleware)

    @app.get("/ok")
    async def ok() -> dict[str, str | None]:
        logging.getLogger("app.inside").info("working")
        return {"rid": logs.request_id_var.get()}

    @app.get("/deny")
    async def deny() -> JSONResponse:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.get("/missing")
    async def missing() -> JSONResponse:
        return JSONResponse({"error": "not_found"}, status_code=404)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/status/{code}")
    async def status(code: int) -> JSONResponse:
        return JSONResponse({}, status_code=code)

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/slow")
    async def slow() -> dict[str, str]:
        await asyncio.sleep(0.06)
        return {}

    @app.get("/bind")
    async def bind() -> dict[str, str | None]:
        logs.bind_tenant("tenant-from-this-request")
        logging.getLogger("app.inside").info("bound")
        return {}

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(f"database said: password=hunter2hunter2 and {SECRET_KEY}")

    return app


@pytest.fixture
async def small() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=small_app()), base_url="http://test") as c:
        yield c


class Emitted(logging.Handler):
    """Formats each record as our JSON line the moment it is emitted (when the request's context is still current)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(logs.JsonFormatter())
        self.lines: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(json.loads(self.format(record)))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [x for x in self.lines if x["logger"] == name]


@pytest.fixture
def emitted() -> Iterator[Emitted]:
    handler, root = Emitted(), logging.getLogger()
    before = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(before)


async def test_every_response_has_a_request_id(small: httpx.AsyncClient) -> None:
    a, b = await small.get("/ok"), await small.get("/ok")
    assert len(a.headers["x-request-id"]) == 32 and a.headers["x-request-id"] != b.headers["x-request-id"]
    assert a.json()["rid"] == a.headers["x-request-id"]  # the handler sees the same id as the caller


async def test_a_sensible_id_from_the_caller_is_kept(small: httpx.AsyncClient) -> None:
    r = await small.get("/ok", headers={"X-Request-ID": "proxy-abc_123.XYZ"})
    assert r.headers["x-request-id"] == "proxy-abc_123.XYZ" and r.json()["rid"] == "proxy-abc_123.XYZ"


@pytest.mark.parametrize("sent", ["short", "x" * 65, "has space in it", "semi;colon-in-id", "new\tline-id", b"caf\xc3\xa9-in-the-id", "a" * 7, ""])
async def test_an_unsuitable_id_from_the_caller_is_replaced(small: httpx.AsyncClient, sent: str | bytes) -> None:
    r = await small.get("/ok", headers={"X-Request-ID": sent})
    assert r.headers["x-request-id"] != sent and len(r.headers["x-request-id"]) == 32


async def test_the_id_boundaries(small: httpx.AsyncClient) -> None:
    for ok in ("a" * 8, "b" * 64):
        assert (await small.get("/ok", headers={"X-Request-ID": ok})).headers["x-request-id"] == ok


async def test_log_lines_written_during_the_request_carry_its_id(small: httpx.AsyncClient, emitted: Emitted) -> None:
    r = await small.get("/ok", headers={"X-Request-ID": "trace-0000001"})
    (inside,) = emitted.named("app.inside")
    assert inside["request_id"] == "trace-0000001" and r.status_code == 200
    (access,) = emitted.named("app.access")
    assert access["request_id"] == "trace-0000001"


async def test_the_id_does_not_leak_into_later_work(small: httpx.AsyncClient) -> None:
    await small.get("/ok", headers={"X-Request-ID": "trace-0000002"})
    assert logs.request_id_var.get() is None and logs.tenant_id_var.get() is None


async def test_access_line_for_a_normal_request(small: httpx.AsyncClient, emitted: Emitted) -> None:
    await small.get("/ok?token=abc12345&phone=%2B60123456789")
    (line,) = emitted.named("app.access")
    assert line["level"] == "info" and line["method"] == "GET" and line["path"] == "/ok" and line["status"] == 200
    assert isinstance(line["duration_ms"], int) and line["duration_ms"] >= 0 and line["client"] == "127.0.0.1" and line["tenant"] is None
    assert "abc12345" not in json.dumps(line) and "60123456789" not in json.dumps(line)  # no query string, ever
    assert line["message"].startswith("GET /ok 200 ") and line["message"].endswith("ms")


@pytest.mark.parametrize("path, status, level", [
    ("/deny", 401, "warning"), ("/missing", 404, "info"), ("/ok", 200, "info"), ("/status/403", 403, "warning"), ("/status/429", 429, "warning"),
    ("/status/422", 422, "info"), ("/status/499", 499, "info"), ("/status/500", 500, "warning"), ("/status/503", 503, "warning"),
])
async def test_access_line_levels(small: httpx.AsyncClient, emitted: Emitted, path: str, status: int, level: str) -> None:
    await small.get(path)
    (line,) = emitted.named("app.access")
    assert line["level"] == level and line["status"] == status


@pytest.mark.parametrize("path", ["/healthz", "/readyz"])
async def test_health_checks_are_debug_lines(small: httpx.AsyncClient, emitted: Emitted, path: str) -> None:
    await small.get(path)
    (line,) = emitted.named("app.access")
    assert line["level"] == "debug"


async def test_the_access_line_times_the_request(small: httpx.AsyncClient, emitted: Emitted) -> None:
    await small.get("/slow")
    (line,) = emitted.named("app.access")
    assert 50 <= line["duration_ms"] < 5000 and f"{line['duration_ms']}ms" in line["message"]


async def test_a_business_named_during_one_request_is_not_named_in_the_next(small: httpx.AsyncClient, emitted: Emitted) -> None:
    await small.get("/bind")
    (inside,) = emitted.named("app.inside")
    assert inside["tenant_id"] == "tenant-from-this-request"
    assert logs.tenant_id_var.get() is None
    await small.get("/ok")
    assert "tenant_id" not in emitted.named("app.access")[-1] and "tenant_id" not in emitted.named("app.inside")[-1]


async def test_an_unexpected_error_is_a_clean_500_with_the_request_id(small: httpx.AsyncClient, emitted: Emitted) -> None:
    r = await small.get("/boom", headers={"X-Request-ID": "trace-0000003"})
    assert r.status_code == 500 and r.headers["x-request-id"] == "trace-0000003"
    body = r.json()
    assert body["error"] == "internal_error" and body["request_id"] == "trace-0000003"
    assert "hunter2" not in r.text and SECRET_KEY not in r.text and "RuntimeError" not in r.text  # nothing of the exception reaches the caller
    (err,) = [x for x in emitted.lines if x["level"] == "error"]
    assert "unhandled error serving GET /boom" in err["message"] and err["request_id"] == "trace-0000003"
    assert "hunter2" not in json.dumps(err) and SECRET_KEY not in json.dumps(err) and "RuntimeError" in err["exception"]  # the traceback is there, scrubbed
    (access,) = emitted.named("app.access")
    assert access["status"] == 500 and access["level"] == "warning"  # (the error line above is the one that reports it)


async def test_the_service_keeps_answering_after_an_error(small: httpx.AsyncClient) -> None:
    assert (await small.get("/boom")).status_code == 500
    assert (await small.get("/ok")).status_code == 200


async def test_an_error_after_the_response_started_is_not_hidden(caplog: pytest.LogCaptureFixture) -> None:
    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("died mid-answer")

    caplog.set_level(logging.DEBUG)
    wrapped = observability.RequestContextMiddleware(app)
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def receive() -> dict[str, Any]:
        return {"type": "http.request"}

    scope = {"type": "http", "method": "GET", "path": "/x", "headers": [], "query_string": b"", "client": ("1.2.3.4", 1)}
    with pytest.raises(RuntimeError, match="died mid-answer"):
        await wrapped(scope, receive, send)
    assert [m["type"] for m in sent] == ["http.response.start"]  # no second response was started
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_other_protocols_pass_straight_through() -> None:
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    await observability.RequestContextMiddleware(app)({"type": "lifespan"}, None, None)  # type: ignore[arg-type]
    assert seen == ["lifespan"]


async def test_a_long_path_is_cut_in_the_log(small: httpx.AsyncClient, emitted: Emitted) -> None:
    await small.get("/" + "a" * 500)
    (line,) = emitted.named("app.access")
    assert len(line["path"]) == 200


# --------------------------------------------------------------------- the real application
async def test_the_real_app_gives_every_answer_an_id(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz", headers={"X-Request-ID": "front-proxy-0001"})
    assert r.headers["x-request-id"] == "front-proxy-0001"
    assert len((await client.get("/v1/countries")).headers["x-request-id"]) == 32


async def test_refusals_carry_the_id_too(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/admin/business-settings")
    assert r.status_code == 401 and len(r.headers["x-request-id"]) == 32


async def test_the_browser_may_read_the_id_and_the_rate_limit_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import create_app

    monkeypatch.setattr(get_settings(), "cors_origins", ["http://shop.example.test"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as c:
        r = await c.get("/healthz", headers={"Origin": "http://shop.example.test"})
    assert r.headers["access-control-allow-origin"] == "http://shop.example.test"
    exposed = {h.strip().lower() for h in r.headers["access-control-expose-headers"].split(",")}
    assert {"x-request-id", "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset"} <= exposed
    assert r.headers["x-request-id"]


async def test_the_access_line_names_the_business(seeded: Seed, emitted: Emitted) -> None:
    r = await seeded.client.get("/v1/admin/business-settings", headers=seeded.admin)
    assert r.status_code == 200
    (line,) = [x for x in emitted.named("app.access") if x["path"] == "/v1/admin/business-settings"]
    assert line["tenant"] == str(seeded.tenant_id) and line["status"] == 200 and line["route"] == "/v1/admin/business-settings"


async def test_the_logged_route_names_the_ids_instead_of_showing_them(seeded: Seed, emitted: Emitted) -> None:
    some_id = uuid.uuid4()
    await seeded.client.patch(f"/v1/admin/resources/{some_id}", json={}, headers=seeded.admin)
    (line,) = [x for x in emitted.named("app.access") if x["path"].startswith("/v1/admin/resources/")]
    assert line["path"].endswith(str(some_id)) and line["route"] == "/v1/admin/resources/{resource_id}"
    await seeded.client.get("/v1/no/such/thing", headers=seeded.admin)
    assert emitted.named("app.access")[-1]["route"] is None  # (an unknown path is not a route: nothing to group by)


async def test_lines_written_by_the_endpoint_name_the_business(seeded: Seed, emitted: Emitted, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import admin

    original = admin.countries.get

    def noisy(code: str) -> Any:  # (stands in for any work an endpoint does)
        logging.getLogger("app.inside").info("in the endpoint")
        return original(code)

    monkeypatch.setattr(admin.countries, "get", noisy)
    await seeded.client.get("/v1/admin/business-settings", headers=seeded.admin)
    inside = emitted.named("app.inside")[0]
    assert inside["tenant_id"] == str(seeded.tenant_id) and inside["request_id"]


async def test_an_unexpected_error_in_the_real_app(seeded: Seed, emitted: Emitted, monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(_: uuid.UUID) -> None:
        raise RuntimeError(f"connection to postgresql://booking_app:s3cr3t-Pw@db/booking lost; key={GOOGLE_KEY}")

    monkeypatch.setattr("app.api.deps._load_tenant", broken)
    r = await seeded.client.get("/v1/admin/business-settings", headers=seeded.admin)
    assert r.status_code == 500 and r.json()["error"] == "internal_error" and r.json()["request_id"] == r.headers["x-request-id"]
    assert "s3cr3t" not in r.text and GOOGLE_KEY not in r.text
    (err,) = [x for x in emitted.lines if x["level"] == "error"]
    assert "s3cr3t" not in json.dumps(err) and GOOGLE_KEY not in json.dumps(err) and err["request_id"] == r.headers["x-request-id"]


# --------------------------------------------------------------------- Sentry
def test_scrubbing_an_event() -> None:
    event = {
        "message": f"failed with {SECRET_KEY}", "request": {"headers": {"Authorization": "Bearer abc"}, "data": {"phone": "+60123456789"}},
        "user": {"ip_address": "1.2.3.4", "email": "a@example.com"},
        "exception": {"values": [{"type": "RuntimeError", "value": f"key={GOOGLE_KEY} rejected", "stacktrace": {"frames": [{"function": "f", "vars": {"password": "hunter2hunter2"}}]}}]},
        "extra": {"x-api-key": SECRET_KEY, "count": 3, "urls": [f"https://x.test/?token=abc12345", 7]},
        "breadcrumbs": {"values": [{"message": f"Bearer {JWT}"}]},
    }
    out = observability.scrub_event(event)
    text = json.dumps(out)
    for secret in (SECRET_KEY, GOOGLE_KEY, "hunter2", "abc12345", JWT, "+60123456789", "a@example.com", "1.2.3.4"):
        assert secret not in text, secret
    assert "request" not in out and "user" not in out
    assert out["exception"]["values"][0]["type"] == "RuntimeError" and out["extra"]["count"] == 3 and out["extra"]["urls"][1] == 7


def test_scrubbed_events_are_tagged_with_the_request_and_business() -> None:
    rid, tid = logs.request_id_var.set("req-tag-0001"), logs.tenant_id_var.set("tenant-9")
    try:
        assert observability.scrub_event({})["tags"] == {"request_id": "req-tag-0001", "tenant_id": "tenant-9"}
    finally:
        logs.request_id_var.reset(rid)
        logs.tenant_id_var.reset(tid)
    assert observability.scrub_event({"tags": {"keep": "me"}})["tags"] == {"keep": "me"}


def test_sentry_is_off_without_a_dsn() -> None:
    assert observability.init_sentry(Settings(_env_file=None)) is False  # type: ignore[call-arg]


def test_a_missing_library_is_reported_not_fatal(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    assert observability.init_sentry(Settings(_env_file=None, sentry_dsn="https://k@o.example.test/1")) is False  # type: ignore[call-arg]
    assert any("sentry-sdk package is not installed" in r.getMessage() for r in caplog.records)


@pytest.fixture
def sentry_events() -> Iterator[list[dict[str, Any]]]:
    import sentry_sdk
    from sentry_sdk.transport import Transport

    events: list[dict[str, Any]] = []

    class Collect(Transport):  # what would have been sent, as it would have been sent
        def capture_envelope(self, envelope: Any) -> None:
            if (event := envelope.get_event()) is not None:
                events.append(event)

    assert observability.init_sentry(Settings(_env_file=None, sentry_dsn="https://key@o0.ingest.example.test/1"), transport=Collect())  # type: ignore[call-arg]
    try:
        yield events
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.init()  # (no DSN: switched off again)


def test_an_error_line_reaches_sentry_scrubbed(sentry_events: list[dict[str, Any]]) -> None:
    rid = logs.request_id_var.set("req-sentry-01")
    try:
        try:
            raise RuntimeError(f"upstream rejected key={GOOGLE_KEY}")
        except RuntimeError:
            logging.getLogger("app.thing").exception("could not look up %s", SECRET_KEY)
    finally:
        logs.request_id_var.reset(rid)
    (event,) = sentry_events
    text = json.dumps(event)
    assert GOOGLE_KEY not in text and SECRET_KEY not in text
    assert event["tags"]["request_id"] == "req-sentry-01" and event["level"] == "error"
    assert "request" not in event and "user" not in event
    assert all("vars" not in frame for v in event["exception"]["values"] for frame in v["stacktrace"]["frames"])  # no local variables


def test_lower_levels_do_not_reach_sentry(sentry_events: list[dict[str, Any]]) -> None:
    logging.getLogger("app.thing").warning("just a warning")
    logging.getLogger("app.thing").info("just info")
    assert sentry_events == []


async def test_an_unexpected_request_error_reaches_sentry_once(small: httpx.AsyncClient, sentry_events: list[dict[str, Any]]) -> None:
    r = await small.get("/boom", headers={"X-Request-ID": "trace-sentry-9"}, params={"token": "abc12345"})
    assert r.status_code == 500
    (event,) = sentry_events
    text = json.dumps(event)
    assert "hunter2" not in text and SECRET_KEY not in text and "abc12345" not in text and "request" not in event
    assert event["tags"]["request_id"] == "trace-sentry-9"


# --------------------------------------------------------------------- the worker
async def test_each_sweep_has_its_own_id(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    seen: list[str | None] = []

    async def fake_sweep() -> SweepReport:
        seen.append(logs.request_id_var.get())
        return SweepReport(ran=False)

    monkeypatch.setattr(worker, "sweep_all", fake_sweep)
    import asyncio

    stop = asyncio.Event()
    caplog.set_level(logging.INFO)
    await worker.run(0, stop, once=True)
    await worker.run(0, stop, once=True)
    assert len(seen) == 2 and seen[0] != seen[1] and all(s and s.startswith("sweep-") for s in seen)


async def test_the_worker_sets_up_logging_and_error_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(worker.logs, "configure_logging", lambda *a, **k: calls.append("logging"))
    monkeypatch.setattr(worker.observability, "init_sentry", lambda *a, **k: calls.append("sentry") or False)

    async def enforce(*a: Any, **k: Any) -> None:
        calls.append("enforce")

    async def done(*a: Any, **k: Any) -> None:
        calls.append("run")

    monkeypatch.setattr(worker.hardening, "enforce", enforce)
    monkeypatch.setattr(worker.outbox, "set_event_publisher", lambda *a, **k: None)
    monkeypatch.setattr(worker, "run", done)
    monkeypatch.setattr(worker, "dispose_engine", done)
    await worker.amain(["--once"])
    assert calls[:3] == ["logging", "sentry", "enforce"]  # logging first: the start-up checks write to it


# --------------------------------------------------------------------- gaps found by mutation testing
def test_a_google_key_alone_is_removed_too() -> None:
    assert GOOGLE_KEY not in logs.redact(f"the lookup used {GOOGLE_KEY} today")


def test_the_stack_of_a_log_line_is_kept_and_scrubbed() -> None:
    line = json.loads(logs.JsonFormatter().format(record("where", stack_info=f"Stack (most recent call last):\n  File x.py token={SECRET_KEY}")))
    assert line["stack"].startswith("Stack") and SECRET_KEY not in line["stack"]
    assert "stack" not in json.loads(logs.JsonFormatter().format(record("plain")))


def test_lines_go_to_standard_output_unless_told_otherwise(capsys: pytest.CaptureFixture[str]) -> None:
    try:
        logs.configure_logging("INFO", "json")
        logging.getLogger("app.thing").info("to stdout")
    finally:
        logs.unconfigure_logging()
        logging.getLogger().setLevel(logging.WARNING)
    out = capsys.readouterr()
    assert json.loads(out.out)["message"] == "to stdout" and "to stdout" not in out.err


def test_breadcrumbs_are_scrubbed_before_they_are_kept(sentry_events: list[dict[str, Any]]) -> None:
    import sentry_sdk

    hook = sentry_sdk.get_client().options["before_breadcrumb"]
    crumb = hook({"category": "httplib", "message": f"Bearer {JWT}", "data": {"url": f"https://maps.example.test/?key={GOOGLE_KEY}"}}, {})
    assert JWT not in json.dumps(crumb) and GOOGLE_KEY not in json.dumps(crumb) and crumb["category"] == "httplib"


async def test_starting_the_service_sets_up_logging_and_error_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main

    calls: list[Any] = []

    async def enforce(*a: Any, **k: Any) -> None:
        calls.append("enforce")

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(main.hardening, "enforce", enforce)
    monkeypatch.setattr(main, "dispose_engine", nothing)
    monkeypatch.setattr(main.delivery, "report_configuration", lambda *a, **k: [])
    monkeypatch.setattr(main.logs, "configure_logging", lambda level, fmt, **k: calls.append(("logging", level, fmt)))
    monkeypatch.setattr(main.observability, "init_sentry", lambda settings, **k: calls.append("sentry") or False)
    monkeypatch.setattr(get_settings(), "log_level", "WARNING")
    monkeypatch.setattr(get_settings(), "log_format", "json")

    monkeypatch.setattr(get_settings(), "environment", "staging")
    async with main.lifespan(main.app):
        pass
    assert calls[:3] == [("logging", "WARNING", "json"), "sentry", "enforce"]  # logging first: the start-up checks write to it

    calls.clear()
    monkeypatch.setattr(get_settings(), "environment", "test")
    async with main.lifespan(main.app):
        pass
    assert calls == ["sentry", "enforce"]  # (tests keep pytest's own log capture)


def test_configuring_leaves_other_handlers_alone_and_unconfiguring_too() -> None:
    other = logging.NullHandler()
    root = logging.getLogger()
    root.addHandler(other)
    try:
        logs.configure_logging("INFO", "json", stream=io.StringIO())
        assert other in root.handlers
        logs.unconfigure_logging()
        assert other in root.handlers and logs.HANDLER_NAME not in [h.get_name() for h in root.handlers]
    finally:
        root.removeHandler(other)
        logs.unconfigure_logging()
        root.setLevel(logging.WARNING)


def test_uvicorns_own_handlers_are_taken_off(configured: io.StringIO) -> None:
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(logging.NullHandler())
    logs.configure_logging("INFO", "json", stream=configured)
    assert all(logging.getLogger(n).handlers == [] for n in ("uvicorn", "uvicorn.error", "uvicorn.access"))


def test_httpcore_is_kept_quiet_too(configured: io.StringIO) -> None:
    logging.getLogger("httpcore").info("connect to https://x.test/?token=abc12345xyz")
    logging.getLogger("httpcore.http11").info("send request headers")
    assert configured.getvalue() == ""


def test_sentry_is_started_without_personal_data(sentry_events: list[dict[str, Any]]) -> None:
    import sentry_sdk

    options = sentry_sdk.get_client().options
    assert options["send_default_pii"] is False and options["include_local_variables"] is False and options["max_request_body_size"] == "never"
    assert options["auto_enabling_integrations"] is False


def test_secret_looking_names_are_hidden_whatever_their_case() -> None:
    out = observability.scrub_event({"extra": {"Authorization": "plainsecretvalue", "X-Api-Key": "anothersecret", "Cookie": "session=1", "note": "fine"}})
    text = json.dumps(out)
    assert "plainsecretvalue" not in text and "anothersecret" not in text and "session=1" not in text and out["extra"]["note"] == "fine"


def test_the_new_settings_have_sensible_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.sign_in_requests_per_day == 10 and s.signup_requests_per_address_per_day == 5 and s.docs_enabled is None
    for bad in ({"sign_in_requests_per_day": -1}, {"signup_requests_per_address_per_day": -1}, {"log_level": "CHATTY"}, {"log_format": "xml"}):
        with pytest.raises(Exception):
            Settings(_env_file=None, **bad)  # type: ignore[call-arg]
