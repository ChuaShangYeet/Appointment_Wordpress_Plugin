"""Google Calendar sync (migration 0025): connecting a calendar (OAuth), and bookings appearing in, moving within and leaving it. A fake Google answers; nothing real is called."""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select, text

from app import worker
from app.core.config import Settings, get_settings
from app.core.database import tenant_session
from app.core.security import decrypt_field
from app.models import Booking
from app.models.calendar import CalendarConnection, CalendarEvent
from app.models.events import OutboxEvent
from app.services import calendar, outbox
from app.services.calendar import CalendarPublisher, GoogleApi, build_event, content_hash, event_id_for
from app.services.notifications import NotificationPublisher
from app.services.sweep import SweepReport, TenantSweep, sweep_all
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_ratelimit import clock, limited, redis  # noqa: F401  (fixtures)

CLIENT_ID = "1234-abcd.apps.example.test"
CLIENT_SECRET = "client-secret-for-tests-0123456789"
REDIRECT = "https://api.example.test/v1/calendar/oauth/callback"
OWNER_EMAIL = "owner@lotus.example.com"


# --------------------------------------------------------------------- a fake Google
class Google:
    """Google's OAuth and Calendar endpoints, as far as the sync uses them. ``fail`` queues one-off answers: ``("POST /events", 500, None)`` fires once on the first request whose
    ``"METHOD path"`` contains the text."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.codes: dict[str, str | None] = {}  # authorisation code -> the refresh token it yields (None: Google sends none)
        self.live_refresh: set[str] = set()
        self.live_access: set[str] = set()
        self.calendars: dict[str, str] = {"primary": "Lotus Spa calendar", "work@example.test": "Work"}
        self.events: dict[str, dict[str, dict[str, Any]]] = {}
        self.ever_made: dict[str, set[str]] = {}
        self.revoked: list[str] = []
        self.fail: list[tuple[str, int | Exception, dict[str, Any] | None]] = []
        self.issued = 0
        self.expires_in = 3600

    # --- helpers for the tests
    def on(self, method: str, text: str = "") -> list[httpx.Request]:
        return [c for c in self.calls if c.method == method and text in c.url.path]

    def forms(self, text: str) -> list[dict[str, str]]:
        return [{k: v[0] for k, v in urllib.parse.parse_qs(c.content.decode()).items()} for c in self.on("POST", text)]

    def stored(self, calendar_id: str = "primary") -> dict[str, dict[str, Any]]:
        return self.events.get(calendar_id, {})

    def writes(self) -> list[httpx.Request]:
        return [c for c in self.calls if c.method in ("POST", "PUT", "DELETE") and "/events" in c.url.path]

    def new_access(self) -> str:
        self.issued += 1
        token = f"access-{self.issued}"
        self.live_access.add(token)
        return token

    # --- the endpoints
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        key = f"{request.method} {request.url.path}"
        for i, (needle, outcome, body) in enumerate(self.fail):
            if needle in key:
                self.fail.pop(i)
                if isinstance(outcome, Exception):
                    raise outcome
                return httpx.Response(outcome, json=body if body is not None else {})
        path = request.url.path
        if path == "/token":
            return self._token(request)
        if path == "/revoke":
            self.revoked.append(urllib.parse.parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200, json={})
        if request.headers.get("authorization", "")[7:] not in self.live_access:
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid Credentials"}})
        parts = path.removeprefix("/calendar/v3/calendars/").split("/")
        cid = urllib.parse.unquote(parts[0])
        if cid not in self.calendars:
            return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found", "errors": [{"reason": "notFound"}]}})
        if len(parts) == 1:
            return httpx.Response(200, json={"id": cid, "summary": self.calendars[cid]})
        store, made = self.events.setdefault(cid, {}), self.ever_made.setdefault(cid, set())
        if request.method == "POST":
            body = json.loads(request.content)
            if body["id"] in made:
                return httpx.Response(409, json={"error": {"code": 409, "message": "The requested identifier already exists.", "errors": [{"reason": "duplicate"}]}})
            store[body["id"]] = body
            made.add(body["id"])
            return httpx.Response(200, json=body)
        event_id = parts[2]
        if request.method == "PUT":
            if event_id not in store and event_id not in made:  # (a deleted event can be updated back to life; one Google has forgotten cannot)
                return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found", "errors": [{"reason": "notFound"}]}})
            store[event_id] = json.loads(request.content)
            return httpx.Response(200, json=store[event_id])
        if request.method == "DELETE":
            if event_id not in store:
                return httpx.Response(410, json={"error": {"code": 410, "message": "Resource has been deleted", "errors": [{"reason": "deleted"}]}})
            del store[event_id]
            return httpx.Response(204)
        return httpx.Response(405)

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in urllib.parse.parse_qs(request.content.decode()).items()}
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            if form.get("redirect_uri") != REDIRECT or form["code"] not in self.codes:
                return httpx.Response(400, json={"error": "invalid_grant"})
            refresh = self.codes.pop(form["code"])
            body: dict[str, Any] = {"access_token": self.new_access(), "expires_in": self.expires_in, "token_type": "Bearer"}
            if refresh is not None:
                self.live_refresh.add(refresh)
                body["refresh_token"] = refresh
            return httpx.Response(200, json=body)
        if form["refresh_token"] not in self.live_refresh:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        return httpx.Response(200, json={"access_token": self.new_access(), "expires_in": self.expires_in, "token_type": "Bearer"})


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch, redis: Any, mail: Recorder) -> Iterator[Google]:  # noqa: F811
    """A configured OAuth client, a fake Google behind it, and the calendar publisher on the outbox (next to the notification one, whose alert mail the ``mail`` recorder takes)."""
    settings = get_settings()
    monkeypatch.setattr(settings, "google_oauth_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_oauth_client_secret", SecretStr(CLIENT_SECRET))
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", REDIRECT)
    fake = Google()
    monkeypatch.setattr(calendar, "_transport", httpx.MockTransport(fake))
    monkeypatch.setattr(outbox, "_publisher", outbox.CompositePublisher(NotificationPublisher(), CalendarPublisher()))
    yield fake


# --------------------------------------------------------------------- helpers
async def connect(s: Seed, g: Google, *, refresh: str | None = None, **body: Any) -> dict[str, Any]:
    """The whole sign-in: ask for the address, 'sign in' at the fake Google, let it redirect back. Returns the listed connection."""
    r = await s.client.post("/v1/admin/calendar/connect", json=body, headers=s.admin)
    assert r.status_code == 200, r.text
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    code = f"code-{uuid.uuid4().hex[:8]}"
    g.codes[code] = refresh or f"refresh-{uuid.uuid4().hex[:8]}"
    done = await s.client.get("/v1/calendar/oauth/callback", params={"code": code, "state": state})
    assert done.status_code == 200 and done.json() == {"status": "connected"}, done.text
    listed = (await s.client.get("/v1/admin/calendar/connections", headers=s.admin)).json()
    wanted = body.get("resource_id")
    return next(c for c in listed if c["resource_id"] == wanted)  # type: ignore[no-any-return]


async def tick(s: Seed, now: dt.datetime | None = None) -> TenantSweep:
    report = await sweep_all(tenant_ids=[s.tenant_id], now=now)
    assert report.ran and not report.errors, report
    return report.tenants[0]


async def book(s: Seed, rid: uuid.UUID, hour: int = 10, **extra: Any) -> str:
    r = await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, hour), rid, **extra))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def set_status(s: Seed, booking_id: str, status: str) -> None:
    r = await s.client.patch(f"/v1/admin/bookings/{booking_id}/status", json={"status": status}, headers=s.admin)
    assert r.status_code == 200, r.text


async def rows(s: Seed, **where: Any) -> list[CalendarEvent]:
    async with tenant_session(s.tenant_id) as db:
        q = select(CalendarEvent).order_by(CalendarEvent.created_at, CalendarEvent.id)
        for column, value in where.items():
            q = q.where(getattr(CalendarEvent, column) == value)
        return list((await db.scalars(q)).all())


async def connection_row(s: Seed, connection_id: str) -> CalendarConnection:
    async with tenant_session(s.tenant_id) as db:
        row = await db.get(CalendarConnection, uuid.UUID(connection_id))
        assert row is not None
        return row


async def set_contact(s: Seed, email: str = OWNER_EMAIL) -> None:
    assert (await s.client.put("/v1/admin/business-profile", json={"business_name": "Lotus Spa", "contact_email": email}, headers=s.admin)).status_code == 200


def only_event(g: Google, calendar_id: str = "primary") -> dict[str, Any]:
    (event,) = g.stored(calendar_id).values()
    return event


# --------------------------------------------------------------------- pure: what an event says
BOOKING = uuid.UUID("12345678-aaaa-bbbb-cccc-1234567890ab")
START, END = dt.datetime(2030, 1, 7, 2, 0, tzinfo=dt.UTC), dt.datetime(2030, 1, 7, 3, 0, tzinfo=dt.UTC)


def event(**over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {"booking_id": BOOKING, "status": "confirmed", "start_at": START, "end_at": END, "timezone": "Asia/Kuala_Lumpur", "services": ["Massage"], "staff": ["Mei"], "share": False}
    args.update(over)
    return build_event(**args)


def test_an_event_without_customer_details_says_only_what_and_when() -> None:
    body = event(customer_name="Aisha", customer_phone="+60187654321", party_size=4, request_text="window seat", address="1 Jalan Bukit")
    assert body["summary"] == "Massage" and body["status"] == "confirmed"
    assert body["description"] == "Booking 12345678\nStatus: confirmed\nWith: Mei"
    assert body["start"] == {"dateTime": "2030-01-07T02:00:00Z", "timeZone": "Asia/Kuala_Lumpur"} and body["end"] == {"dateTime": "2030-01-07T03:00:00Z", "timeZone": "Asia/Kuala_Lumpur"}
    assert body["extendedProperties"] == {"private": {"bookingId": str(BOOKING)}} and body["reminders"] == {"useDefault": True}
    text_ = json.dumps(body)
    for private in ("Aisha", "87654321", "window seat", "Jalan Bukit", "Party"):
        assert private not in text_
    assert "location" not in body


def test_an_event_with_customer_details_adds_them() -> None:
    body = event(share=True, customer_name="Aisha", customer_phone="+60187654321", party_size=4, request_text="window seat", address="1 Jalan Bukit")
    assert body["summary"] == "Massage - Aisha" and body["location"] == "1 Jalan Bukit"
    assert body["description"].splitlines() == ["Booking 12345678", "Status: confirmed", "With: Mei", "Phone: +60187654321", "Party of 4", "Request: window seat"]


def test_shared_details_that_are_missing_are_left_out() -> None:
    body = event(share=True)
    assert body["summary"] == "Massage" and "location" not in body and body["description"] == "Booking 12345678\nStatus: confirmed\nWith: Mei"


def test_several_services_and_no_staff() -> None:
    body = event(services=["Oil change", "Brake service"], staff=[])
    assert body["summary"] == "Oil change, Brake service" and "With:" not in body["description"]
    assert event(services=[])["summary"] == "Booking"


def test_text_from_people_is_made_safe_and_short() -> None:
    body = event(share=True, services=["Cut\r\nColour"], customer_name="Aisha\nBinti" + "x" * 300, request_text="a\r\nb" + "y" * 900)
    assert "\n" not in body["summary"] and "\r" not in body["summary"] and body["summary"].startswith("Cut Colour - Aisha Binti")
    assert len(body["summary"]) <= 300 and len(body["description"]) <= 2000
    request_line = next(line for line in body["description"].splitlines() if line.startswith("Request:"))
    assert len(request_line) <= len("Request: ") + 500


def test_times_are_sent_as_utc_whatever_zone_they_arrive_in() -> None:
    local = dt.datetime(2030, 1, 7, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
    body = event(start_at=local, end_at=local + dt.timedelta(hours=1), timezone="Australia/Sydney")
    assert body["start"] == {"dateTime": "2030-01-07T02:00:00Z", "timeZone": "Australia/Sydney"}


def test_the_hash_follows_the_content_not_the_key_order() -> None:
    a = {"x": 1, "y": {"b": 2, "a": 3}}
    assert content_hash(a) == content_hash({"y": {"a": 3, "b": 2}, "x": 1}) and len(content_hash(a)) == 64
    assert content_hash(a) != content_hash({"x": 1, "y": {"b": 2, "a": 4}})
    assert content_hash(event()) != content_hash(event(status="completed")) != content_hash(event(services=["Facial"]))


def test_event_ids_are_stable_valid_and_unique() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert event_id_for(a) == event_id_for(a) and event_id_for(a) != event_id_for(b)
    assert len(event_id_for(a)) == 32 and set(event_id_for(a)) <= set("0123456789abcdefghijklmnopqrstuv")  # (what Google accepts in an event id)


def test_the_authorisation_address_asks_for_offline_access_to_events_only() -> None:
    settings = Settings(_env_file=None, google_oauth_client_id=CLIENT_ID, google_oauth_client_secret=CLIENT_SECRET, google_oauth_redirect_uri=REDIRECT)  # type: ignore[call-arg]
    url = GoogleApi(settings).authorization_url("the-state")
    base, _, query = url.partition("?")
    assert base == "https://accounts.google.com/o/oauth2/v2/auth"
    assert {k: v[0] for k, v in urllib.parse.parse_qs(query).items()} == {
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code", "scope": "https://www.googleapis.com/auth/calendar.events",
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false", "state": "the-state",
    }
    assert CLIENT_SECRET not in url


def test_the_google_settings_come_all_together_or_not_at_all() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert not calendar.is_configured(s) and s.calendar_done_url == "" and s.calendar_batch == 50 and s.calendar_max_attempts == 8 and s.calendar_pause_after_failures == 10
    assert s.google_token_url == "https://oauth2.googleapis.com/token" and s.google_calendar_url == "https://www.googleapis.com/calendar/v3"
    full = {"google_oauth_client_id": CLIENT_ID, "google_oauth_client_secret": CLIENT_SECRET, "google_oauth_redirect_uri": REDIRECT}
    assert calendar.is_configured(Settings(_env_file=None, **full))  # type: ignore[call-arg]
    for missing in full:
        with pytest.raises(ValueError, match="all three or none"):
            Settings(_env_file=None, **{k: v for k, v in full.items() if k != missing})  # type: ignore[call-arg]
    assert CLIENT_SECRET not in repr(Settings(_env_file=None, **full))  # type: ignore[call-arg]
    for bad in ({"calendar_timeout_seconds": 0}, {"calendar_batch": 0}, {"calendar_max_attempts": 0}, {"calendar_pause_after_failures": 0}, {"calendar_state_ttl_seconds": 30}):
        with pytest.raises(ValueError):
            Settings(_env_file=None, **bad)  # type: ignore[call-arg]


# --------------------------------------------------------------------- connecting
async def test_without_a_google_client_nothing_can_be_connected(seeded: Seed, redis: Any) -> None:  # noqa: F811
    r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
    assert r.status_code == 503 and r.json()["error"] == "calendar_not_configured" and await redis.keys("gcal:*") == []


async def test_only_the_owner_connects_and_a_manager_looks(seeded: Seed, google: Google) -> None:
    c = seeded.client
    assert (await c.post("/v1/admin/calendar/connect", json={})).status_code == 401
    manager, staff = await seeded.token("manager"), await seeded.token("staff")
    assert (await c.post("/v1/admin/calendar/connect", json={}, headers=manager)).status_code == 403
    assert (await c.get("/v1/admin/calendar/connections", headers=manager)).status_code == 200
    assert (await c.get("/v1/admin/calendar/connections", headers=staff)).status_code == 403
    assert (await c.get("/v1/admin/calendar/connections")).status_code == 401
    assert google.calls == []


async def test_asking_to_connect_gives_googles_address_and_keeps_the_state_for_ten_minutes(seeded: Seed, google: Google, redis: Any) -> None:  # noqa: F811
    r = await seeded.client.post("/v1/admin/calendar/connect", json={"share_customer_details": True}, headers=seeded.admin)
    assert r.status_code == 200
    url = r.json()["authorization_url"]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?") and query["client_id"] == [CLIENT_ID] and query["redirect_uri"] == [REDIRECT]
    assert CLIENT_SECRET not in url and google.calls == []  # (nothing is sent to Google yet)
    (key,) = await redis.keys("gcal:state:*")
    assert query["state"][0] not in key  # only a hash of the state is kept
    assert 0 < await redis.ttl(key) <= 600
    assert json.loads(await redis.get(key)) == {"tenant_id": str(seeded.tenant_id), "resource_id": None, "calendar_id": "primary", "share": True}


@pytest.mark.parametrize("body, status", [
    ({"calendar_id": "bad id with spaces"}, 422), ({"calendar_id": ""}, 422), ({"calendar_id": "x" * 255}, 422), ({"calendar_id": "a/b"}, 422),
    ({"surprise": 1}, 422), ({"resource_id": "not-a-uuid"}, 422), ({"share_customer_details": "maybe"}, 422),
])
async def test_a_bad_request_to_connect_is_refused(seeded: Seed, google: Google, redis: Any, body: dict[str, Any], status: int) -> None:  # noqa: F811
    r = await seeded.client.post("/v1/admin/calendar/connect", json=body, headers=seeded.admin)
    assert r.status_code == status and await redis.keys("gcal:*") == []


async def test_only_a_staff_member_of_the_business_can_have_a_calendar_of_their_own(client: httpx.AsyncClient, google: Google) -> None:
    mine, theirs = await make_seed(client), await make_seed(client)
    foreign = await theirs.add_staff("Lin")
    assert (await mine.client.post("/v1/admin/calendar/connect", json={"resource_id": str(foreign)}, headers=mine.admin)).status_code == 404
    assert (await mine.client.post("/v1/admin/calendar/connect", json={"resource_id": str(uuid.uuid4())}, headers=mine.admin)).status_code == 404
    table = (await mine.client.post("/v1/admin/resources", json={"type": "table", "name": "T1", "capacity": 4}, headers=mine.admin))
    assert table.status_code == 201
    r = await mine.client.post("/v1/admin/calendar/connect", json={"resource_id": table.json()["id"]}, headers=mine.admin)
    assert r.status_code == 422 and "staff" in r.json()["detail"]
    assert (await mine.client.post("/v1/admin/calendar/connect", json={"resource_id": str(await mine.add_staff("Mei"))}, headers=mine.admin)).status_code == 200


async def test_coming_back_from_google_stores_the_connection_encrypted(seeded: Seed, google: Google) -> None:
    listed = await connect(seeded, google, refresh="the-refresh-token", calendar_id="work@example.test", share_customer_details=True)
    assert listed["calendar_id"] == "work@example.test" and listed["account_label"] == "Work" and listed["status"] == "active" and listed["share_customer_details"] is True
    assert listed["resource_id"] is None and listed["resource_name"] is None and listed["last_sync_at"] is None and (listed["pending"], listed["synced"], listed["failed"]) == (0, 0, 0)
    row = await connection_row(seeded, listed["id"])
    assert b"the-refresh-token" not in row.refresh_token_enc and decrypt_field(row.refresh_token_enc, seeded.tenant_id) == "the-refresh-token"
    assert row.access_token_enc is not None and b"access-" not in row.access_token_enc and decrypt_field(row.access_token_enc, seeded.tenant_id).startswith("access-")
    assert row.access_expires_at is not None and row.access_expires_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=50)
    (exchange,) = google.forms("/token")
    assert exchange["grant_type"] == "authorization_code" and exchange["redirect_uri"] == REDIRECT and exchange["client_secret"] == CLIENT_SECRET
    summary_call = google.on("GET", "/calendars/work")
    assert len(summary_call) == 1 and summary_call[0].headers["authorization"].startswith("Bearer access-")  # (the calendar was checked with the new token)
    assert "the-refresh-token" not in json.dumps((await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json())


async def test_the_state_works_once(seeded: Seed, google: Google) -> None:
    r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    google.codes["c1"], google.codes["c2"] = "refresh-1", "refresh-2"
    assert (await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c1", "state": state})).status_code == 200
    again = await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c2", "state": state})
    assert again.status_code == 400 and again.json() == {"status": "error", "reason": "invalid_state"}
    assert google.codes == {"c2": "refresh-2"}  # the second code was never exchanged


@pytest.mark.parametrize("params", [{}, {"code": "c"}, {"state": ""}, {"code": "c", "state": "never-issued"}])
async def test_a_callback_without_a_good_state_does_nothing(seeded: Seed, google: Google, params: dict[str, str]) -> None:
    r = await seeded.client.get("/v1/calendar/oauth/callback", params=params)
    assert r.status_code == 400 and r.json() == {"status": "error", "reason": "invalid_state"} and google.calls == []


async def test_a_callback_needs_no_key_and_carries_the_safety_headers(client: httpx.AsyncClient, google: Google) -> None:
    r = await client.get("/v1/calendar/oauth/callback", params={"state": "x"})
    assert r.status_code == 400 and r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("error, reason", [("access_denied", "denied"), ("server_error", "google_error"), ("anything", "google_error")])
async def test_refusing_at_google_is_reported_and_uses_up_the_state(seeded: Seed, google: Google, error: str, reason: str) -> None:
    r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    done = await seeded.client.get("/v1/calendar/oauth/callback", params={"error": error, "state": state})
    assert done.status_code == 400 and done.json()["reason"] == reason and google.calls == []
    assert (await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c", "state": state})).json()["reason"] == "invalid_state"
    assert (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json() == []


async def test_a_callback_with_a_state_but_no_code_is_a_google_error(seeded: Seed, google: Google) -> None:
    r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    assert (await seeded.client.get("/v1/calendar/oauth/callback", params={"state": state})).json()["reason"] == "google_error"


@pytest.mark.parametrize("fail, reason", [
    (("POST /token", 400, {"error": "invalid_grant"}), "google_refused"),
    (("POST /token", 401, {"error": "invalid_client"}), "google_refused"),
    (("POST /token", 500, None), "google_unavailable"),
    (("POST /token", 429, None), "google_unavailable"),
    (("POST /token", httpx.ConnectError("down"), None), "google_unavailable"),
    (("/calendars/primary", 404, {"error": {"code": 404, "message": "Not Found"}}), "google_refused"),
    (("/calendars/primary", 403, {"error": {"code": 403, "message": "no", "errors": [{"reason": "forbidden"}]}}), "google_refused"),
    (("/calendars/primary", 500, None), "google_unavailable"),
    (("/calendars/primary", 401, None), "google_refused"),
])
async def test_when_google_will_not_complete_the_sign_in_nothing_is_stored(seeded: Seed, google: Google, fail: Any, reason: str) -> None:
    r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    google.codes["c1"] = "refresh-1"
    google.fail.append(fail)
    done = await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c1", "state": state})
    assert done.status_code == 400 and done.json() == {"status": "error", "reason": reason}
    assert (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json() == []


async def test_a_sign_in_that_yields_no_refresh_token_is_refused(seeded: Seed, google: Google) -> None:
    r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    google.codes["c1"] = None
    done = await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c1", "state": state})
    assert done.status_code == 400 and done.json()["reason"] == "no_refresh_token"
    assert (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json() == []


async def test_the_browser_is_sent_on_to_the_page_that_asked(seeded: Seed, google: Google, monkeypatch: pytest.MonkeyPatch) -> None:
    for done_url, expected_start in (("https://shop.example.test/settings", "https://shop.example.test/settings?"), ("https://shop.example.test/settings?tab=calendar", "https://shop.example.test/settings?tab=calendar&")):
        monkeypatch.setattr(get_settings(), "calendar_done_url", done_url)
        r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
        google.codes["c1"] = "refresh-1"
        ok = await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c1", "state": state})
        assert ok.status_code == 303 and ok.headers["location"] == expected_start + "calendar=connected"
        bad = await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c1", "state": state})
        assert bad.status_code == 303 and bad.headers["location"] == expected_start + "calendar=error&reason=invalid_state"


async def test_connecting_the_same_calendar_again_replaces_the_sign_in(seeded: Seed, google: Google) -> None:
    first = await connect(seeded, google, refresh="old-refresh")
    row = await connection_row(seeded, first["id"])
    async with tenant_session(seeded.tenant_id) as db:  # (as if it had been paused)
        stored = await db.get(CalendarConnection, row.id)
        assert stored is not None
        stored.status, stored.consecutive_failures, stored.last_error = "needs_reconnect", 7, "gone"
    second = await connect(seeded, google, refresh="new-refresh", calendar_id="work@example.test")
    assert second["id"] == first["id"] and second["status"] == "active" and second["calendar_id"] == "work@example.test" and second["last_error"] is None
    again = await connection_row(seeded, first["id"])
    assert again.consecutive_failures == 0 and decrypt_field(again.refresh_token_enc, seeded.tenant_id) == "new-refresh"
    assert len((await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json()) == 1


async def test_the_business_and_each_staff_member_have_one_calendar_each(seeded: Seed, google: Google) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    shop = await connect(seeded, google)
    a = await connect(seeded, google, resource_id=str(mei))
    b = await connect(seeded, google, resource_id=str(lin))
    again = await connect(seeded, google, resource_id=str(mei))
    assert len({shop["id"], a["id"], b["id"]}) == 3 and again["id"] == a["id"]
    listed = (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json()
    assert len(listed) == 3 and {c["resource_name"] for c in listed} == {None, "Mei", "Lin"}


async def test_a_staff_member_removed_while_signing_in_is_reported(seeded: Seed, google: Google) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/admin/calendar/connect", json={"resource_id": str(mei)}, headers=seeded.admin)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("DELETE FROM resource_shifts WHERE resource_id = :r"), {"r": mei})
        await db.execute(text("DELETE FROM resources WHERE id = :r"), {"r": mei})
    google.codes["c1"] = "refresh-1"
    done = await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c1", "state": state})
    assert done.status_code == 400 and done.json()["reason"] == "resource_gone"
    assert (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json() == []


async def test_a_suspended_business_cannot_finish_connecting(client: httpx.AsyncClient, google: Google) -> None:
    s = await make_seed(client)
    r = await s.client.post("/v1/admin/calendar/connect", json={}, headers=s.admin)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    async with tenant_session(None) as db:
        await db.execute(text("UPDATE tenants SET status = 'suspended' WHERE id = :t"), {"t": s.tenant_id})
    google.codes["c1"] = "refresh-1"
    done = await s.client.get("/v1/calendar/oauth/callback", params={"code": "c1", "state": state})
    assert done.status_code == 400 and done.json()["reason"] == "invalid_state"
    async with tenant_session(s.tenant_id) as db:
        assert (await db.scalars(select(CalendarConnection))).all() == []


async def test_each_business_sees_only_its_own_connections(client: httpx.AsyncClient, google: Google) -> None:
    a, b = await make_seed(client), await make_seed(client)
    ours = await connect(a, google)
    assert (await b.client.get("/v1/admin/calendar/connections", headers=b.admin)).json() == []
    for call in (b.client.patch(f"/v1/admin/calendar/connections/{ours['id']}", json={"share_customer_details": True}, headers=b.admin),
                 b.client.post(f"/v1/admin/calendar/connections/{ours['id']}/resync", headers=b.admin),
                 b.client.delete(f"/v1/admin/calendar/connections/{ours['id']}", headers=b.admin)):
        assert (await call).status_code == 404
    assert len((await a.client.get("/v1/admin/calendar/connections", headers=a.admin)).json()) == 1


# --------------------------------------------------------------------- managing a connection
async def test_the_connection_list_counts_the_events_and_never_shows_a_token(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google, refresh="secret-refresh-value")
    mei = await seeded.add_staff("Mei")
    await book(seeded, mei, 10)
    await tick(seeded)
    listed = (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json()
    assert [(c["pending"], c["synced"], c["failed"]) for c in listed] == [(0, 1, 0)] and listed[0]["last_sync_at"]
    assert set(listed[0]) == {"id", "resource_id", "resource_name", "calendar_id", "account_label", "status", "share_customer_details", "last_sync_at", "last_error", "pending", "synced", "failed"}
    assert "secret-refresh-value" not in json.dumps(listed) and conn["id"] == listed[0]["id"]


async def test_changing_the_sharing_rewrites_every_event(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    await book(seeded, mei, 10, customer={"name": "Aisha", "phone": "+60187654321"})
    await tick(seeded)
    assert "Aisha" not in json.dumps(only_event(google))
    r = await seeded.client.patch(f"/v1/admin/calendar/connections/{conn['id']}", json={"share_customer_details": True}, headers=seeded.admin)
    assert r.status_code == 200 and r.json()["share_customer_details"] is True and r.json()["pending"] == 1
    await tick(seeded)
    assert only_event(google)["summary"] == "Massage - Aisha" and "Phone: +60187654321" in only_event(google)["description"]
    await seeded.client.patch(f"/v1/admin/calendar/connections/{conn['id']}", json={"share_customer_details": False}, headers=seeded.admin)
    await tick(seeded)
    assert "Aisha" not in json.dumps(only_event(google)) and "87654321" not in json.dumps(only_event(google))


async def test_only_the_owner_changes_a_connection(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    manager = await seeded.token("manager")
    for call in (seeded.client.patch(f"/v1/admin/calendar/connections/{conn['id']}", json={"share_customer_details": True}, headers=manager),
                 seeded.client.post(f"/v1/admin/calendar/connections/{conn['id']}/resync", headers=manager),
                 seeded.client.delete(f"/v1/admin/calendar/connections/{conn['id']}", headers=manager)):
        assert (await call).status_code == 403
    assert (await seeded.client.patch(f"/v1/admin/calendar/connections/{conn['id']}", json={"share_customer_details": True, "calendar_id": "x"}, headers=seeded.admin)).status_code == 422
    assert (await seeded.client.patch(f"/v1/admin/calendar/connections/{uuid.uuid4()}", json={"share_customer_details": True}, headers=seeded.admin)).status_code == 404


async def test_a_resync_writes_everything_again_and_gives_failures_another_chance(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    await book(seeded, mei, 10)
    await book(seeded, mei, 12)
    google.fail.append(("/events", 400, {"error": {"code": 400, "message": "bad"}}))
    await tick(seeded)
    assert sorted(r.state for r in await rows(seeded)) == ["failed", "synced"]
    google.calls.clear()
    r = await seeded.client.post(f"/v1/admin/calendar/connections/{conn['id']}/resync", headers=seeded.admin)
    assert r.status_code == 200 and r.json()["queued"] >= 2
    await tick(seeded)
    assert sorted(r.state for r in await rows(seeded)) == ["synced", "synced"] and len(google.writes()) == 2 and len(google.stored()) == 2  # (even the unchanged one was sent again)


async def test_a_paused_connection_cannot_be_resynced(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    async with tenant_session(seeded.tenant_id) as db:
        row = await db.get(CalendarConnection, uuid.UUID(conn["id"]))
        assert row is not None
        row.status = "needs_reconnect"
    r = await seeded.client.post(f"/v1/admin/calendar/connections/{conn['id']}/resync", headers=seeded.admin)
    assert r.status_code == 409 and "connected again" in r.json()["detail"]


async def test_disconnecting_forgets_the_calendar_and_tells_google(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google, refresh="refresh-to-revoke")
    mei = await seeded.add_staff("Mei")
    await book(seeded, mei, 10)
    await tick(seeded)
    assert len(google.stored()) == 1
    r = await seeded.client.delete(f"/v1/admin/calendar/connections/{conn['id']}", headers=seeded.admin)
    assert r.status_code == 204 and r.content == b""
    assert google.revoked == ["refresh-to-revoke"]
    assert (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json() == [] and await rows(seeded) == []
    assert len(google.stored()) == 1  # (the events already in the calendar stay there)
    await book(seeded, mei, 12)
    google.calls.clear()
    await tick(seeded)
    assert google.calls == []


async def test_disconnecting_works_even_if_google_cannot_be_told(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    google.fail.append(("/revoke", httpx.ConnectError("down"), None))
    assert (await seeded.client.delete(f"/v1/admin/calendar/connections/{conn['id']}", headers=seeded.admin)).status_code == 204
    assert (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json() == []


async def test_connection_changes_are_audited_without_tokens(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google, refresh="audited-refresh")
    await seeded.client.patch(f"/v1/admin/calendar/connections/{conn['id']}", json={"share_customer_details": True}, headers=seeded.admin)
    await seeded.client.delete(f"/v1/admin/calendar/connections/{conn['id']}", headers=seeded.admin)
    r = await seeded.client.get("/v1/admin/audit-log", headers=seeded.admin)
    actions = [e["action"] for e in r.json()]
    assert "POST /v1/admin/calendar/connect" in actions and "PATCH /v1/admin/calendar/connections/{connection_id}" in actions and "DELETE /v1/admin/calendar/connections/{connection_id}" in actions
    assert "audited-refresh" not in r.text and "access-" not in r.text


# --------------------------------------------------------------------- bookings in the calendar
async def test_a_confirmed_booking_appears_in_the_businesss_calendar(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, 10)
    report = await tick(seeded)
    assert report.calendar_synced == 1 and report.calendar_removed == 0 and report.calendar_failed == 0
    event_ = only_event(google)
    assert event_["summary"] == "Massage" and event_["status"] == "confirmed" and event_["extendedProperties"]["private"]["bookingId"] == bid
    assert event_["start"] == {"dateTime": at(seeded.day, 10).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": "Asia/Kuala_Lumpur"}
    assert event_["end"]["dateTime"] == at(seeded.day, 11).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert event_["description"] == f"Booking {bid[:8]}\nStatus: confirmed\nWith: Mei"
    (row,) = await rows(seeded)
    assert row.state == "synced" and row.external_event_id == event_["id"] == event_id_for(row.id) and row.attempts == 0 and row.synced_at is not None
    assert row.content_hash == content_hash(event_) and str(row.booking_id) == bid and str(row.connection_id) == conn["id"]
    (write,) = google.writes()
    assert write.method == "POST" and write.url.params["sendUpdates"] == "none" and write.headers["authorization"].startswith("Bearer access-")
    assert (await connection_row(seeded, conn["id"])).last_sync_at is not None


async def test_a_booking_made_before_the_calendar_was_connected_is_written_when_it_is(seeded: Seed, google: Google) -> None:
    mei = await seeded.add_staff("Mei")
    await book(seeded, mei, 10)
    await tick(seeded)  # (no calendar yet: nothing happens)
    assert google.calls == [] and await rows(seeded) == []
    await connect(seeded, google)
    assert [r.state for r in await rows(seeded)] == ["pending"]
    await tick(seeded)
    assert len(google.stored()) == 1


async def test_a_booking_awaiting_an_answer_has_no_event_until_it_is_confirmed(client: httpx.AsyncClient, google: Google) -> None:
    s = await make_seed(client, default_status="pending")
    await connect(s, google)
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, 10)
    await tick(s)
    assert google.stored() == {} and await rows(s) == []
    await set_status(s, bid, "confirmed")
    await tick(s)
    assert len(google.stored()) == 1 and only_event(google)["extendedProperties"]["private"]["bookingId"] == bid


async def test_a_rejected_booking_never_gets_an_event(client: httpx.AsyncClient, google: Google) -> None:
    s = await make_seed(client, default_status="pending")
    await connect(s, google)
    bid = await book(s, await s.add_staff("Mei"), 10)
    await set_status(s, bid, "rejected")
    await tick(s)
    assert google.calls == [] or google.writes() == []
    assert await rows(s) == []


@pytest.mark.parametrize("status", ["cancelled", "no-show"])
async def test_a_booking_that_will_not_happen_leaves_the_calendar(seeded: Seed, google: Google, status: str) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    assert len(google.stored()) == 1
    await set_status(seeded, bid, status)
    report = await tick(seeded)
    assert google.stored() == {} and report.calendar_removed == 1 and await rows(seeded) == []
    assert [c.method for c in google.writes()] == ["POST", "DELETE"]


async def test_a_finished_booking_stays_in_the_calendar_with_its_new_status(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    await set_status(seeded, bid, "completed")
    await tick(seeded)
    assert "Status: completed" in only_event(google)["description"] and [c.method for c in google.writes()] == ["POST", "PUT"]
    (row,) = await rows(seeded)
    assert row.state == "synced"


async def test_a_moved_booking_moves_in_the_calendar(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, 10)
    await tick(seeded)
    before = only_event(google)["id"]
    r = await seeded.client.post(f"/v1/admin/bookings/{bid}/reschedule", json={"start_at": at(seeded.day, 15).isoformat()}, headers=seeded.admin)
    assert r.status_code == 200, r.text
    await tick(seeded)
    moved = only_event(google)
    assert moved["id"] == before and moved["start"]["dateTime"] == at(seeded.day, 15).astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ") and len(google.stored()) == 1
    assert [c.method for c in google.writes()] == ["POST", "PUT"]


async def test_nothing_is_sent_when_nothing_changed(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, 10)
    await tick(seeded)
    google.calls.clear()
    await tick(seeded)
    assert google.calls == []
    async with tenant_session(seeded.tenant_id) as db:  # a nudge for a booking that is still the same
        assert await calendar.mark_booking(db, uuid.UUID(bid)) == 1
    report = await tick(seeded)
    assert google.writes() == [] and report.calendar_synced == 0
    assert [r.state for r in await rows(seeded)] == ["synced"]


async def test_health_notes_never_reach_the_calendar(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google, share_customer_details=True)
    await book(seeded, await seeded.add_staff("Mei"), 10, health_notes="diabetic, allergic to latex", special_request="quiet room")
    await tick(seeded)
    text_ = json.dumps(only_event(google))
    assert "diabetic" not in text_ and "latex" not in text_ and "Request: quiet room" in text_ and conn["share_customer_details"] is True


async def test_an_erased_customers_name_is_not_shared(seeded: Seed, google: Google) -> None:
    await connect(seeded, google, share_customer_details=True)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10, customer={"name": "Aisha", "phone": "+60187654321"})
    await tick(seeded)
    assert "Aisha" in json.dumps(only_event(google))
    async with tenant_session(seeded.tenant_id) as db:
        booking = await db.get(Booking, uuid.UUID(bid))
        assert booking is not None
        await db.execute(text("UPDATE customers SET erased_at = now(), name = 'Erased customer' WHERE id = :c"), {"c": booking.customer_id})
    async with tenant_session(seeded.tenant_id) as db:
        await calendar.mark_booking(db, uuid.UUID(bid))
    await tick(seeded)
    assert "Aisha" not in json.dumps(only_event(google)) and "87654321" not in json.dumps(only_event(google))


# --------------------------------------------------------------------- whose calendar
async def test_a_staff_calendar_receives_only_that_staff_members_bookings(seeded: Seed, google: Google) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    shop = await connect(seeded, google, calendar_id="primary")
    google.calendars["lin@example.test"] = "Lin"
    lins = await connect(seeded, google, resource_id=str(lin), calendar_id="lin@example.test")
    b_mei, b_lin = await book(seeded, mei, 10), await book(seeded, lin, 10)
    await tick(seeded)
    assert {e["extendedProperties"]["private"]["bookingId"] for e in google.stored("primary").values()} == {b_mei, b_lin}  # the business sees both
    assert [e["extendedProperties"]["private"]["bookingId"] for e in google.stored("lin@example.test").values()] == [b_lin]  # Lin sees hers
    assert shop["id"] != lins["id"]


async def test_a_booking_that_changes_hands_changes_calendars(seeded: Seed, google: Google) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    google.calendars["mei@example.test"], google.calendars["lin@example.test"] = "Mei", "Lin"
    await connect(seeded, google, resource_id=str(mei), calendar_id="mei@example.test")
    await connect(seeded, google, resource_id=str(lin), calendar_id="lin@example.test")
    bid = await book(seeded, mei, 10)
    await tick(seeded)
    assert len(google.stored("mei@example.test")) == 1 and google.stored("lin@example.test") == {}
    r = await seeded.client.post(f"/v1/admin/bookings/{bid}/reschedule", json={"start_at": at(seeded.day, 11).isoformat(), "resource_id": str(lin)}, headers=seeded.admin)
    assert r.status_code == 200, r.text
    await tick(seeded)
    assert google.stored("mei@example.test") == {} and len(google.stored("lin@example.test")) == 1
    assert len(await rows(seeded)) == 1  # (Mei's record went with her event)


async def test_a_finished_bookings_staff_member_keeps_it(seeded: Seed, google: Google) -> None:
    mei = await seeded.add_staff("Mei")
    google.calendars["mei@example.test"] = "Mei"
    await connect(seeded, google, resource_id=str(mei), calendar_id="mei@example.test")
    bid = await book(seeded, mei, 10)
    await tick(seeded)
    await set_status(seeded, bid, "completed")  # (its allocations are released now)
    await tick(seeded)
    assert len(google.stored("mei@example.test")) == 1 and "Status: completed" in next(iter(google.stored("mei@example.test").values()))["description"]


async def test_staff_calendars_are_filled_from_their_own_bookings_when_connected(seeded: Seed, google: Google) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    await book(seeded, mei, 10)
    await book(seeded, lin, 12)
    google.calendars["mei@example.test"] = "Mei"
    await connect(seeded, google, resource_id=str(mei), calendar_id="mei@example.test")
    await tick(seeded)
    assert len(google.stored("mei@example.test")) == 1


async def test_bookings_long_over_are_not_written_when_connecting(seeded: Seed, google: Google) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, 10)
    await tick(seeded)  # (nothing is connected yet: the booking's change is published and forgotten)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE bookings SET start_at = now() - interval '10 days', end_at = now() - interval '10 days' + interval '1 hour' WHERE id = :b"), {"b": uuid.UUID(bid)})
    await connect(seeded, google)
    await tick(seeded)
    assert google.stored() == {}


async def test_one_calendar_failing_does_not_hold_up_the_other(seeded: Seed, google: Google) -> None:
    mei = await seeded.add_staff("Mei")
    google.calendars["mei@example.test"] = "Mei"
    await connect(seeded, google)
    await connect(seeded, google, resource_id=str(mei), calendar_id="mei@example.test")
    await book(seeded, mei, 10)
    google.fail.append(("/calendars/primary/events", 500, None))
    report = await tick(seeded)
    assert report.calendar_synced == 1 and len(google.stored("mei@example.test")) == 1 and google.stored("primary") == {}
    assert sorted(r.state for r in await rows(seeded)) == ["pending", "synced"]


# --------------------------------------------------------------------- when Google is difficult
async def test_a_server_error_is_retried_later_with_backoff(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", 500, None))
    t0 = dt.datetime.now(dt.UTC)
    report = await tick(seeded, t0)
    assert report.calendar_synced == 0 and google.stored() == {}
    (row,) = await rows(seeded)
    assert row.state == "pending" and row.attempts == 1 and "500" in (row.last_error or "") and row.next_attempt_at == t0 + dt.timedelta(seconds=30)
    google.calls.clear()
    await tick(seeded, t0 + dt.timedelta(seconds=10))
    assert google.calls == []  # not yet
    report = await tick(seeded, t0 + dt.timedelta(seconds=31))
    assert report.calendar_synced == 1 and len(google.stored()) == 1
    (row,) = await rows(seeded)
    assert row.state == "synced" and row.attempts == 0 and row.last_error is None
    assert (await connection_row(seeded, conn["id"])).last_error is None


@pytest.mark.parametrize("fail", [
    (429, {"error": {"code": 429, "message": "slow down"}}),
    (403, {"error": {"code": 403, "message": "Rate Limit Exceeded", "errors": [{"reason": "rateLimitExceeded"}]}}),
    (403, {"error": {"code": 403, "message": "User Rate Limit Exceeded", "errors": [{"reason": "userRateLimitExceeded"}]}}),
    (503, {"error": {"code": 503, "message": "Backend Error", "errors": [{"reason": "backendError"}]}}),
    (httpx.ReadTimeout("slow"), None),
    (httpx.ConnectError("down"), None),
])
async def test_what_asks_us_to_slow_down_is_tried_again(seeded: Seed, google: Google, fail: tuple[Any, Any]) -> None:
    await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", fail[0], fail[1]))
    t0 = dt.datetime.now(dt.UTC)
    await tick(seeded, t0)
    (row,) = await rows(seeded)
    assert row.state == "pending" and row.attempts == 1
    await tick(seeded, t0 + dt.timedelta(minutes=2))
    assert [r.state for r in await rows(seeded)] == ["synced"] and len(google.stored()) == 1


@pytest.mark.parametrize("status, body", [
    (400, {"error": {"code": 400, "message": "Invalid value"}}),
    (403, {"error": {"code": 403, "message": "Forbidden", "errors": [{"reason": "forbidden"}]}}),
    (404, None),
])
async def test_what_google_refuses_for_good_is_not_tried_again(seeded: Seed, google: Google, status: int, body: dict[str, Any] | None) -> None:
    conn = await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", status, body))
    report = await tick(seeded)
    assert report.calendar_failed == 1
    (row,) = await rows(seeded)
    assert row.state == "failed" and row.attempts == 1 and str(status) in (row.last_error or "")
    google.calls.clear()
    await tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(hours=3))
    assert google.calls == []
    state = await connection_row(seeded, conn["id"])
    assert state.consecutive_failures == 1 and state.status == "active" and state.last_error == row.last_error
    listed = (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json()[0]
    assert (listed["pending"], listed["synced"], listed["failed"]) == (0, 0, 1)


async def test_an_event_is_given_up_on_after_enough_attempts(seeded: Seed, google: Google, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_max_attempts", 3)
    await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    now = dt.datetime.now(dt.UTC)
    for attempt in range(3):
        google.fail.append(("/events", 500, None))
        await tick(seeded, now + dt.timedelta(hours=attempt))
    (row,) = await rows(seeded)
    assert row.state == "failed" and row.attempts == 3


async def test_a_calendar_that_keeps_refusing_is_paused_and_the_business_told(seeded: Seed, google: Google, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_pause_after_failures", 2)
    await set_contact(seeded)
    conn = await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    for hour in (10, 12, 14):
        await book(seeded, mei, hour)
    google.fail.extend([("/events", 400, {"error": {"code": 400, "message": "bad"}})] * 3)
    report = await tick(seeded)
    assert report.calendar_failed == 2  # (the third waits: the calendar was paused after the second)
    state = await connection_row(seeded, conn["id"])
    assert state.status == "needs_reconnect" and "keeps refusing" in (state.last_error or "")
    assert sorted(r.state for r in await rows(seeded)) == ["failed", "failed", "pending"]
    writes_so_far = len(google.writes())
    await tick(seeded)  # the outbox carries the alert out
    assert len(google.writes()) == writes_so_far and sorted(r.state for r in await rows(seeded)) == ["failed", "failed", "pending"]  # (the paused calendar is left alone)
    alerts = [m for m in mail.sent if (m.subject or "").startswith("Your Google Calendar for")]
    assert len(alerts) == 1 and alerts[0].recipient == OWNER_EMAIL and "Lotus Spa" in alerts[0].subject
    assert "business's Google Calendar" in alerts[0].body and "keeps refusing" in alerts[0].body and "/v1/admin/calendar/connect" in alerts[0].body
    google.calls.clear()
    await tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(hours=3))
    assert google.writes() == []  # paused: nothing more is sent


async def test_a_staff_calendar_alert_names_the_staff_member(seeded: Seed, google: Google, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_pause_after_failures", 1)
    await set_contact(seeded)
    mei = await seeded.add_staff("Mei")
    google.calendars["mei@example.test"] = "Mei"
    await connect(seeded, google, resource_id=str(mei), calendar_id="mei@example.test")
    await book(seeded, mei, 10)
    google.fail.append(("/events", 400, None))
    await tick(seeded)
    await tick(seeded)
    (alert,) = [m for m in mail.sent if (m.subject or "").startswith("Your Google Calendar for")]
    assert "Mei's Google Calendar" in alert.body


async def test_reconnecting_a_paused_calendar_writes_everything_again(seeded: Seed, google: Google, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_pause_after_failures", 1)
    conn = await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", 400, None))
    await tick(seeded)
    assert (await connection_row(seeded, conn["id"])).status == "needs_reconnect"
    again = await connect(seeded, google)
    assert again["id"] == conn["id"] and again["status"] == "active"
    await tick(seeded)
    assert len(google.stored()) == 1 and [r.state for r in await rows(seeded)] == ["synced"]


async def test_the_alert_is_not_sent_if_the_calendar_was_connected_again_first(seeded: Seed, google: Google, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_pause_after_failures", 1)
    await set_contact(seeded)
    await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", 400, None))
    # pause, then reconnect before the outbox is worked off: the alert has nothing left to say
    await tick(seeded)  # (pauses; the same sweep worked the outbox before the sync, so the alert is still queued)
    await connect(seeded, google)
    await tick(seeded)
    assert not [m for m in mail.sent if (m.subject or "").startswith("Your Google Calendar for")]


async def test_without_a_contact_email_the_alert_is_noted_not_sent(seeded: Seed, google: Google, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_pause_after_failures", 1)
    await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", 400, None))
    await tick(seeded)
    await tick(seeded)
    assert not [m for m in mail.sent if (m.subject or "").startswith("Your Google Calendar for")]
    async with tenant_session(seeded.tenant_id) as db:
        logged = (await db.execute(text("SELECT status, error FROM notification_log WHERE event = 'calendar.connection_lost'"))).all()
    assert logged == [("skipped", "no business contact email on file")]


async def test_a_failed_alert_is_retried_by_the_outbox(seeded: Seed, google: Google, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_pause_after_failures", 1)
    await set_contact(seeded)
    await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", 400, None))
    mail.failing["email"] = RuntimeError("smtp down")
    t0 = dt.datetime.now(dt.UTC)
    await tick(seeded, t0)
    await tick(seeded, t0 + dt.timedelta(seconds=1))
    assert not [m for m in mail.sent if (m.subject or "").startswith("Your Google Calendar for")]
    mail.failing.clear()
    await tick(seeded, t0 + dt.timedelta(minutes=10))
    assert len([m for m in mail.sent if (m.subject or "").startswith("Your Google Calendar for")]) == 1


# --------------------------------------------------------------------- tokens
async def test_one_access_token_serves_many_events(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    for hour in (10, 12, 14):
        await book(seeded, mei, hour)
    await tick(seeded)
    assert len(google.stored()) == 3 and [f["grant_type"] for f in google.forms("/token")] == ["authorization_code"]  # (no refresh was needed)
    assert len({c.headers["authorization"] for c in google.writes()}) == 1


async def test_an_expired_access_token_is_replaced_from_the_refresh_token(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google, refresh="long-lived")
    async with tenant_session(seeded.tenant_id) as db:
        row = await db.get(CalendarConnection, uuid.UUID(conn["id"]))
        assert row is not None
        row.access_expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)  # (inside the minute of margin)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    refreshes = [f for f in google.forms("/token") if f["grant_type"] == "refresh_token"]
    assert len(refreshes) == 1 and refreshes[0]["refresh_token"] == "long-lived" and refreshes[0]["client_secret"] == CLIENT_SECRET
    assert len(google.stored()) == 1
    fresh = await connection_row(seeded, conn["id"])
    assert fresh.access_expires_at is not None and fresh.access_expires_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=50)
    assert decrypt_field(fresh.access_token_enc or b"", seeded.tenant_id) == google.writes()[0].headers["authorization"].removeprefix("Bearer ")


async def test_an_access_token_google_refuses_early_is_renewed_once(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google, refresh="long-lived")
    google.live_access.clear()  # revoked behind our back
    await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    assert len(google.stored()) == 1 and len([f for f in google.forms("/token") if f["grant_type"] == "refresh_token"]) == 1
    assert (await connection_row(seeded, conn["id"])).status == "active"


async def test_a_sign_in_google_no_longer_accepts_pauses_the_calendar(seeded: Seed, google: Google, mail: Recorder) -> None:
    await set_contact(seeded)
    conn = await connect(seeded, google, refresh="will-be-revoked")
    async with tenant_session(seeded.tenant_id) as db:
        row = await db.get(CalendarConnection, uuid.UUID(conn["id"]))
        assert row is not None
        row.access_expires_at = None
    google.live_refresh.clear()  # the owner removed the app in their Google account
    mei = await seeded.add_staff("Mei")
    for hour in (10, 12, 14):
        await book(seeded, mei, hour)
    report = await tick(seeded)
    assert report.calendar_failed == 0 and google.writes() == []
    assert len([f for f in google.forms("/token") if f["grant_type"] == "refresh_token"]) == 1  # (asked once, not once per event)
    state = await connection_row(seeded, conn["id"])
    assert state.status == "needs_reconnect" and "no longer accepts the saved sign-in" in (state.last_error or "")
    assert [r.state for r in await rows(seeded)] == ["pending"] * 3  # (waiting for the reconnect, not lost)
    await tick(seeded)
    (alert,) = [m for m in mail.sent if (m.subject or "").startswith("Your Google Calendar for")]
    assert "no longer accepts the saved sign-in" in alert.body and "will-be-revoked" not in alert.body
    await connect(seeded, google)
    await tick(seeded)
    assert len(google.stored()) == 3


async def test_an_access_token_that_stays_refused_pauses_the_calendar(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.extend([("/events", 401, None), ("/events", 401, None)])
    await tick(seeded)
    assert (await connection_row(seeded, conn["id"])).status == "needs_reconnect"


async def test_google_being_down_for_a_refresh_is_just_a_retry(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    async with tenant_session(seeded.tenant_id) as db:
        row = await db.get(CalendarConnection, uuid.UUID(conn["id"]))
        assert row is not None
        row.access_expires_at = None
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/token", 503, None))
    t0 = dt.datetime.now(dt.UTC)
    await tick(seeded, t0)
    assert (await connection_row(seeded, conn["id"])).status == "active" and [r.state for r in await rows(seeded)] == ["pending"]
    await tick(seeded, t0 + dt.timedelta(minutes=2))
    assert len(google.stored()) == 1


# --------------------------------------------------------------------- Google and we disagree
async def test_an_event_somebody_deleted_in_google_is_brought_back(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    google.stored().clear()  # deleted by hand in Google (it remembers the id, and an update brings the event back)
    await set_status(seeded, bid, "completed")  # the booking changes, so the event is written again
    await tick(seeded)
    assert len(google.stored()) == 1 and "Status: completed" in only_event(google)["description"]
    assert [c.method for c in google.writes()] == ["POST", "PUT"]


async def test_an_event_google_has_forgotten_entirely_is_made_again(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    google.stored().clear()
    google.ever_made["primary"].clear()  # purged: the id is free again
    await set_status(seeded, bid, "completed")
    await tick(seeded)
    assert len(google.stored()) == 1 and [c.method for c in google.writes()] == ["POST", "PUT", "POST"]


async def test_an_id_that_is_taken_but_cannot_be_updated_is_retried_not_lost(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    google.fail.extend([("PUT", 404, None), ("POST", 409, None)])
    await set_status(seeded, bid, "completed")
    await tick(seeded)
    (row,) = await rows(seeded)
    assert row.state == "pending" and row.attempts == 1 and "taken" in (row.last_error or "")


async def test_a_retry_after_a_lost_answer_finds_the_event_it_made(seeded: Seed, google: Google, monkeypatch: pytest.MonkeyPatch) -> None:
    await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    lost = {"done": False}

    def lost_answer(request: httpx.Request) -> httpx.Response:  # the request reaches Google (which makes the event) but the answer never comes back
        response = google(request)
        if request.method == "POST" and "/events" in request.url.path and not lost["done"]:
            lost["done"] = True
            raise httpx.ReadTimeout("answer lost")
        return response

    monkeypatch.setattr(calendar, "_transport", httpx.MockTransport(lost_answer))
    t0 = dt.datetime.now(dt.UTC)
    await tick(seeded, t0)
    monkeypatch.setattr(calendar, "_transport", httpx.MockTransport(google))
    assert len(google.stored()) == 1  # Google has it; we do not know
    (row,) = await rows(seeded)
    assert row.state == "pending"
    await tick(seeded, t0 + dt.timedelta(minutes=2))
    assert len(google.stored()) == 1 and [r.state for r in await rows(seeded)] == ["synced"]  # the same event, updated: not a second one
    assert [c.method for c in google.writes()] == ["POST", "POST", "PUT"]


async def test_deleting_an_event_that_is_already_gone_is_fine(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    google.stored().clear()
    await set_status(seeded, bid, "cancelled")
    report = await tick(seeded)
    assert report.calendar_removed == 1 and await rows(seeded) == []


async def test_a_busy_calendar_is_worked_off_in_batches(seeded: Seed, google: Google, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_batch", 2)
    await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    for hour in (9, 10, 11, 14, 15):
        await book(seeded, mei, hour)
    assert (await tick(seeded)).calendar_synced == 2
    assert (await tick(seeded)).calendar_synced == 2
    assert (await tick(seeded)).calendar_synced == 1 and len(google.stored()) == 5


# --------------------------------------------------------------------- the outbox side
async def test_only_booking_changes_nudge_the_calendar(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    publisher = CalendarPublisher()
    for kind, aggregate in (("booking.reminder", "booking"), ("booking.schedule_conflict", "booking"), ("review.created", "booking"), ("shift.updated", "booking"),
                            ("booking.confirmed", "review"), ("booking.cancelled", "webhook")):
        await publisher.publish(OutboxEvent(tenant_id=seeded.tenant_id, event_type=kind, aggregate_type=aggregate, aggregate_id=uuid.UUID(bid), payload={}))
    assert [r.state for r in await rows(seeded)] == ["synced"]  # nobody nudged it
    await publisher.publish(OutboxEvent(tenant_id=seeded.tenant_id, event_type="booking.rescheduled", aggregate_type="booking", aggregate_id=uuid.UUID(bid), payload={}))
    assert [r.state for r in await rows(seeded)] == ["pending"]


@pytest.mark.parametrize("kind", sorted(calendar.TRIGGER_EVENTS))
async def test_every_change_of_a_booking_nudges_the_calendar(seeded: Seed, google: Google, kind: str) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    await CalendarPublisher().publish(OutboxEvent(tenant_id=seeded.tenant_id, event_type=kind, aggregate_type="booking", aggregate_id=uuid.UUID(bid), payload={}))
    assert [r.state for r in await rows(seeded)] == ["pending"]


async def test_with_no_calendar_connected_a_nudge_does_nothing(seeded: Seed, google: Google) -> None:
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    async with tenant_session(seeded.tenant_id) as db:
        assert await calendar.mark_booking(db, uuid.UUID(bid)) == 0
        assert await calendar.mark_booking(db, uuid.uuid4()) == 0
    assert await rows(seeded) == []


async def test_a_paused_calendar_is_not_nudged(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    async with tenant_session(seeded.tenant_id) as db:
        row = await db.get(CalendarConnection, uuid.UUID(conn["id"]))
        assert row is not None
        row.status = "needs_reconnect"
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    async with tenant_session(seeded.tenant_id) as db:
        assert await calendar.mark_booking(db, uuid.UUID(bid)) == 0


async def test_the_nudge_makes_the_event_due_at_the_moment_of_the_change(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    moment = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    async with tenant_session(seeded.tenant_id) as db:
        await calendar.mark_booking(db, uuid.UUID(bid), due=moment)
    (row,) = await rows(seeded)
    assert row.next_attempt_at == moment
    async with tenant_session(seeded.tenant_id) as db:  # a second nudge moves a waiting event's time too
        await calendar.mark_booking(db, uuid.UUID(bid), due=moment + dt.timedelta(days=1))
    assert (await rows(seeded))[0].next_attempt_at == moment + dt.timedelta(days=1)


# --------------------------------------------------------------------- the sweep and the worker
def test_the_worker_reports_calendar_events() -> None:
    report = SweepReport(ran=True, tenants=[TenantSweep(uuid.uuid4(), calendar_synced=3, calendar_removed=1), TenantSweep(uuid.uuid4(), calendar_synced=2)])
    assert "6 calendar events written" in worker.summarise(report)


async def test_the_worker_puts_the_calendar_publisher_on_the_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    published: list[Any] = []
    monkeypatch.setattr(worker.outbox, "set_event_publisher", lambda p: published.append(p))

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(worker.hardening, "enforce", nothing)
    monkeypatch.setattr(worker, "run", nothing)
    monkeypatch.setattr(worker, "dispose_engine", nothing)
    monkeypatch.setattr(worker.logs, "configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(worker.observability, "init_sentry", lambda *a, **k: False)
    await worker.amain(["--once"])
    (composite,) = published
    assert [type(p) for p in composite.publishers] == [NotificationPublisher, worker.WebhookPublisher, CalendarPublisher]


async def test_calendar_tables_are_private_to_each_business(client: httpx.AsyncClient, google: Google) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await connect(a, google)
    await book(a, await a.add_staff("Mei"), 10)
    await tick(a)
    async with tenant_session(b.tenant_id) as db:
        assert (await db.scalars(select(CalendarConnection))).all() == [] and (await db.scalars(select(CalendarEvent))).all() == []


# --------------------------------------------------------------------- gaps found by mutation testing
def test_how_googles_answers_are_sorted() -> None:
    check = GoogleApi._check
    check(httpx.Response(200), (200,))
    check(httpx.Response(404), (200, 404))
    with pytest.raises(calendar.AccessExpired):
        check(httpx.Response(401), (200,))
    for status in (429, 500, 502, 599):
        with pytest.raises(calendar.Retryable):
            check(httpx.Response(status), (200,))
    for status in (400, 403, 404, 409, 499):
        with pytest.raises(calendar.Rejected):
            check(httpx.Response(status, json={"error": {"message": "no", "errors": [{"reason": "forbidden"}]}}), (200,))
    for reason in ("rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"):
        with pytest.raises(calendar.Retryable):
            check(httpx.Response(403, json={"error": {"errors": [{"reason": reason}]}}), (200,))
    with pytest.raises(calendar.Rejected):
        check(httpx.Response(400, text="not json"), (200,))


def test_googles_error_text_is_kept_on_one_line_and_short() -> None:
    answer = httpx.Response(400, json={"error": {"message": "bad\r\nthing " + "x" * 500, "errors": [{"reason": "invalid"}]}})
    with pytest.raises(calendar.Rejected) as caught:
        GoogleApi._check(answer, (200,))
    text_ = str(caught.value)
    assert "\n" not in text_ and "\r" not in text_ and len(text_) == 300 and text_.startswith("Google answered HTTP 400 invalid: bad thing")


def test_text_from_people_is_trimmed_and_cut() -> None:
    assert calendar.clean("  a\tb  ") == "a b" and calendar.clean("x" * 300, 10) == "x" * 10 and calendar.clean("x" * 300) == "x" * 200


def test_a_very_long_event_is_cut_to_what_google_allows() -> None:
    body = event(services=["S" * 150] * 5, staff=["T" * 100] * 40)
    assert len(body["summary"]) == 300 and len(body["description"]) == 2000


async def test_a_network_failure_is_stored_without_its_text(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", httpx.ConnectError("cannot reach https://secret.example.test/?token=abc123"), None))
    await tick(seeded)
    (row,) = await rows(seeded)
    assert "ConnectError" in (row.last_error or "") and "abc123" not in (row.last_error or "") and "secret.example" not in (row.last_error or "")


async def test_a_sign_in_answer_without_a_token_is_refused(seeded: Seed, google: Google) -> None:
    r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
    state = urllib.parse.parse_qs(urllib.parse.urlsplit(r.json()["authorization_url"]).query)["state"][0]
    google.codes["c1"] = "refresh-1"
    google.fail.append(("/token", 200, {"unexpected": True}))
    done = await seeded.client.get("/v1/calendar/oauth/callback", params={"code": "c1", "state": state})
    assert done.status_code == 400 and done.json()["reason"] == "google_refused"


async def test_the_access_token_is_cached_for_as_long_as_google_says(seeded: Seed, google: Google) -> None:
    google.expires_in = 300
    conn = await connect(seeded, google)
    row = await connection_row(seeded, conn["id"])
    now = dt.datetime.now(dt.UTC)
    assert row.access_expires_at is not None and now + dt.timedelta(seconds=240) < row.access_expires_at < now + dt.timedelta(seconds=330)
    async with tenant_session(seeded.tenant_id) as db:
        stored = await db.get(CalendarConnection, row.id)
        assert stored is not None
        stored.access_expires_at = None
    google.expires_in = 120
    await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    again = await connection_row(seeded, conn["id"])
    assert again.access_expires_at is not None and now + dt.timedelta(seconds=60) < again.access_expires_at < now + dt.timedelta(seconds=180)


async def test_the_calendar_id_is_quoted_in_the_address(seeded: Seed, google: Google) -> None:
    await connect(seeded, google, calendar_id="work@example.test")
    await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    (write,) = google.writes()
    assert b"/calendars/work%40example.test/events" in write.url.raw_path


async def test_the_calendars_name_is_cut(seeded: Seed, google: Google) -> None:
    google.calendars["primary"] = "L" * 500
    assert len((await connect(seeded, google))["account_label"]) == 200


async def test_a_failed_update_is_retried(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    await set_status(seeded, bid, "completed")
    google.fail.append(("PUT", 500, None))
    t0 = dt.datetime.now(dt.UTC)
    await tick(seeded, t0)
    (row,) = await rows(seeded)
    assert row.state == "pending" and row.attempts == 1 and "Status: confirmed" in only_event(google)["description"]
    await tick(seeded, t0 + dt.timedelta(minutes=2))
    assert [r.state for r in await rows(seeded)] == ["synced"] and "Status: completed" in only_event(google)["description"]


async def test_a_failed_delete_is_retried(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    await set_status(seeded, bid, "cancelled")
    google.fail.append(("DELETE", 500, None))
    t0 = dt.datetime.now(dt.UTC)
    await tick(seeded, t0)
    (row,) = await rows(seeded)
    assert row.state == "pending" and row.attempts == 1 and len(google.stored()) == 1  # (still there, and still owed)
    await tick(seeded, t0 + dt.timedelta(minutes=2))
    assert await rows(seeded) == [] and google.stored() == {}


async def test_a_delete_google_answers_not_found_to_is_done(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    await set_status(seeded, bid, "cancelled")
    google.fail.append(("DELETE", 404, None))
    report = await tick(seeded)
    assert report.calendar_removed == 1 and report.calendar_failed == 0 and await rows(seeded) == []


async def test_an_event_that_never_reached_google_is_removed_without_asking_google(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    async with tenant_session(seeded.tenant_id) as db:
        assert await calendar.mark_booking(db, uuid.UUID(bid)) == 1  # a record is waiting to write it ...
    await set_status(seeded, bid, "cancelled")  # ... but the booking is cancelled before the sweep: the calendar never heard of it
    google.calls.clear()
    await tick(seeded)
    assert google.writes() == [] and await rows(seeded) == []


async def test_staff_of_counts_held_places_and_leaves_tables_out(client: httpx.AsyncClient, google: Google) -> None:
    s = await make_seed(client, default_status="pending")
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, 10)  # pending: its place is held, not yet active
    table = (await s.client.post("/v1/admin/resources", json={"type": "table", "name": "T1", "capacity": 4}, headers=s.admin)).json()["id"]
    async with tenant_session(s.tenant_id) as db:
        await db.execute(text("INSERT INTO booking_allocations (tenant_id, booking_id, resource_id, period, state) SELECT tenant_id, id, :r, tstzrange(start_at, end_at), 'active' FROM bookings WHERE id = :b"),
                         {"r": uuid.UUID(table), "b": uuid.UUID(bid)})
        assert await calendar.staff_of(db, uuid.UUID(bid)) == {mei}
        assert await calendar.staff_of(db, uuid.uuid4()) == set()


async def test_a_nudge_marks_only_the_calendars_the_booking_belongs_in(seeded: Seed, google: Google) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    google.calendars["lin@example.test"] = "Lin"
    await connect(seeded, google)
    await connect(seeded, google, resource_id=str(lin), calendar_id="lin@example.test")
    bid = await book(seeded, mei, 10)
    await tick(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        assert await calendar.mark_booking(db, uuid.UUID(bid)) == 1  # the business's, not Lin's


async def test_a_nudge_gives_a_failed_event_a_fresh_start(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE calendar_events SET state = 'failed', attempts = 3"))
        await calendar.mark_booking(db, uuid.UUID(bid))
    (row,) = await rows(seeded)
    assert row.state == "pending" and row.attempts == 0


async def test_connecting_queues_only_bookings_that_belong(client: httpx.AsyncClient, google: Google) -> None:
    s = await make_seed(client, default_status="pending")
    await book(s, await s.add_staff("Mei"), 10)  # awaiting an answer: no event
    await connect(s, google)
    assert await rows(s) == []


async def test_a_booking_that_ended_a_few_hours_ago_is_still_written_when_connecting(seeded: Seed, google: Google) -> None:
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE bookings SET start_at = now() - interval '13 hours', end_at = now() - interval '12 hours' WHERE id = :b"), {"b": uuid.UUID(bid)})
    await connect(seeded, google)
    assert len(await rows(seeded)) == 1


async def test_resyncing_one_calendar_leaves_the_others_alone(seeded: Seed, google: Google) -> None:
    mei = await seeded.add_staff("Mei")
    google.calendars["mei@example.test"] = "Mei"
    shop = await connect(seeded, google)
    hers = await connect(seeded, google, resource_id=str(mei), calendar_id="mei@example.test")
    await book(seeded, mei, 10)
    await tick(seeded)
    assert sorted(r.state for r in await rows(seeded)) == ["synced", "synced"]
    assert (await seeded.client.post(f"/v1/admin/calendar/connections/{hers['id']}/resync", headers=seeded.admin)).json()["queued"] == 1
    states = {str(r.connection_id): r.state for r in await rows(seeded)}
    assert states == {shop["id"]: "synced", hers["id"]: "pending"}


async def test_the_sync_report_counts_what_happened(seeded: Seed, google: Google) -> None:
    await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    ids = [uuid.UUID(await book(seeded, mei, hour)) for hour in (9, 10, 11, 14)]
    async with tenant_session(seeded.tenant_id) as db:
        for booking_id in ids:
            await calendar.mark_booking(db, booking_id)
    google.fail.extend([("/events", 500, None), ("/events", 400, None)])
    t0 = dt.datetime.now(dt.UTC)
    async with tenant_session(seeded.tenant_id) as db:
        first = await calendar.sync_due(db, t0)
    assert (first.synced, first.removed, first.unchanged, first.retried, first.failed, first.paused) == (2, 0, 0, 1, 1, 0)
    async with tenant_session(seeded.tenant_id) as db:
        second = await calendar.sync_due(db, t0 + dt.timedelta(minutes=2))
    assert (second.synced, second.retried, second.failed) == (1, 0, 0)
    async with tenant_session(seeded.tenant_id) as db:  # one more nudge for a booking that has not changed, and one for a booking that is cancelled
        synced_one = (await db.scalars(select(CalendarEvent.booking_id).where(CalendarEvent.state == "synced"))).all()
        await calendar.mark_booking(db, synced_one[0])
        await db.execute(text("UPDATE bookings SET status = 'cancelled' WHERE id = :b"), {"b": synced_one[1]})
        await calendar.mark_booking(db, synced_one[1])
    async with tenant_session(seeded.tenant_id) as db:
        third = await calendar.sync_due(db, t0 + dt.timedelta(minutes=3))
    assert (third.synced, third.removed, third.unchanged, third.retried, third.failed) == (0, 1, 1, 0, 0)


async def test_a_success_clears_the_failure_count(seeded: Seed, google: Google) -> None:
    conn = await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    await book(seeded, mei, 10)
    await book(seeded, mei, 12)
    google.fail.append(("/events", 400, None))  # the first event written is refused, the second goes through
    await tick(seeded)
    state = await connection_row(seeded, conn["id"])
    assert state.consecutive_failures == 0 and state.last_error is None and sorted(r.state for r in await rows(seeded)) == ["failed", "synced"]


async def test_a_shared_event_has_the_address_and_an_erased_customers_has_none(seeded: Seed, google: Google) -> None:
    from app.models import CustomerAddress

    await connect(seeded, google, share_customer_details=True)
    bid = await book(seeded, await seeded.add_staff("Mei"), 10)
    await tick(seeded)
    assert "location" not in only_event(google)
    async with tenant_session(seeded.tenant_id) as db:
        booking = await db.get(Booking, uuid.UUID(bid))
        assert booking is not None
        place = CustomerAddress(tenant_id=seeded.tenant_id, customer_id=booking.customer_id, line1="1 Jalan Bukit", city="Kuala Lumpur", postcode="50450")
        db.add(place)
        await db.flush()
        booking.address_id = place.id
        await calendar.mark_booking(db, booking.id)
    await tick(seeded)
    assert only_event(google)["location"] == "1 Jalan Bukit, 50450, Kuala Lumpur"
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE customers SET erased_at = now() WHERE id = (SELECT customer_id FROM bookings WHERE id = :b)"), {"b": uuid.UUID(bid)})
        await calendar.mark_booking(db, uuid.UUID(bid))
    await tick(seeded)
    assert "location" not in only_event(google) and "1 Jalan" not in json.dumps(only_event(google))


async def test_services_are_listed_in_the_order_they_were_booked(seeded: Seed, google: Google) -> None:
    ids = []
    for name in ("Zebra wrap", "Alpha scrub"):
        r = await seeded.client.post("/v1/admin/services", json={"name": name, "duration_minutes": 30, "buffer_minutes": 0, "price": "10.00"}, headers=seeded.admin)
        assert r.status_code == 201
        ids.append(r.json()["id"])
    await connect(seeded, google)
    mei = await seeded.add_staff("Mei")
    await book(seeded, mei, 10, service_ids=ids)
    await book(seeded, mei, 12, service_ids=ids[::-1])
    await tick(seeded)
    assert sorted(e["summary"] for e in google.stored().values()) == ["Alpha scrub, Zebra wrap", "Zebra wrap, Alpha scrub"]  # (each in the order it was booked, whatever the names)
    assert {e["summary"]: e["start"]["dateTime"][11:13] for e in google.stored().values()}["Zebra wrap, Alpha scrub"] == at(seeded.day, 10).astimezone(dt.UTC).strftime("%H")


async def test_a_server_missing_part_of_its_google_settings_cannot_connect(seeded: Seed, google: Google, monkeypatch: pytest.MonkeyPatch) -> None:
    for name, empty in (("google_oauth_client_id", ""), ("google_oauth_client_secret", SecretStr("")), ("google_oauth_redirect_uri", "")):
        with monkeypatch.context() as m:
            m.setattr(get_settings(), name, empty)
            r = await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)
            assert r.status_code == 503 and r.json()["error"] == "calendar_not_configured", name


async def test_every_sign_in_gets_a_state_of_its_own(seeded: Seed, google: Google, redis: Any) -> None:  # noqa: F811
    urls = [(await seeded.client.post("/v1/admin/calendar/connect", json={}, headers=seeded.admin)).json()["authorization_url"] for _ in range(3)]
    states = {urllib.parse.parse_qs(urllib.parse.urlsplit(u).query)["state"][0] for u in urls}
    assert len(states) == 3 and all(len(s) >= 40 for s in states) and len(await redis.keys("gcal:state:*")) == 3


async def test_connections_are_listed_in_the_order_they_were_made(seeded: Seed, google: Google) -> None:
    google.calendars.update({f"{n}@example.test": n for n in ("a", "b", "c")})
    made = [(await connect(seeded, google))["id"]]
    for n in ("a", "b", "c"):
        made.append((await connect(seeded, google, resource_id=str(await seeded.add_staff(n.upper())), calendar_id=f"{n}@example.test"))["id"])
    assert [c["id"] for c in (await seeded.client.get("/v1/admin/calendar/connections", headers=seeded.admin)).json()] == made


@pytest.mark.parametrize("params", [{"code": "x" * 2049}, {"state": "x" * 513}, {"error": "x" * 201}])
async def test_the_callback_refuses_absurdly_long_values(seeded: Seed, google: Google, params: dict[str, str]) -> None:
    assert (await seeded.client.get("/v1/calendar/oauth/callback", params=params)).status_code == 422
    assert (await seeded.client.get("/v1/calendar/oauth/callback", params={k: v[:-1] for k, v in params.items()})).status_code == 400  # (one less is merely a bad state)


async def test_the_callback_is_limited_per_address(client: httpx.AsyncClient, google: Google, limited: Any) -> None:
    codes = [(await client.get("/v1/calendar/oauth/callback", params={"state": "x"})).status_code for _ in range(31)]
    assert codes == [400] * 30 + [429]


async def test_the_alert_is_sent_once_however_often_the_event_is_published(seeded: Seed, google: Google, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "calendar_pause_after_failures", 1)
    await set_contact(seeded)
    await connect(seeded, google)
    await book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", 400, None))
    await tick(seeded)
    await tick(seeded)
    subjects = lambda: [m for m in mail.sent if (m.subject or "").startswith("Your Google Calendar for")]  # noqa: E731
    assert len(subjects()) == 1
    async with tenant_session(seeded.tenant_id) as db:
        lost = (await db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == "calendar.connection_lost"))).one()
    await NotificationPublisher().publish(lost)
    await NotificationPublisher().publish(lost)
    assert len(subjects()) == 1
