"""Google Calendar sync (blueprint §12.3, §15): confirmed bookings appear as events in the business's own calendar and in the calendar of the staff member who has them.

**One way**: the booking system is the source of truth; events are created, moved and removed to match it, and edits made in Google are overwritten the next
time the booking changes. Nothing is read back.

**How it stays right.** A booking event on the outbox (:class:`CalendarPublisher`) only *nudges*: it marks the booking's ``calendar_events`` row ``pending``.
The sweep (:func:`sync_due`) then looks at the booking *as it is now* and makes Google match it: create or update the event if the booking is confirmed (or under way, or
done) and belongs to this calendar, delete it otherwise. Retries, reordering and repeated nudges all end in the same place, and an unchanged booking costs no request
(a hash of what was last sent). The event's id is derived from the row's id, so a retry after a crash finds the event it already made instead of making a second.

**Whose calendar.** A *business* connection (``resource_id`` empty) receives every booking; a *staff* connection receives the bookings that member is allocated to
(the live allocations; for a finished booking, whoever had it).

**Privacy.** An event carries the services, the staff, the status and a short booking reference. The customer's name and phone, the party size, their request and the
address go in only if the owner switched ``share_customer_details`` on for that calendar - Google is then a processor of that data (list it in the privacy notice). Health
notes are never sent.

**Connecting** is Google's OAuth 2.0 web flow: the owner asks for an authorisation address (:func:`begin_authorization`), signs in at Google, and Google sends the browser to
``GOOGLE_OAUTH_REDIRECT_URI`` with a code, which is exchanged for tokens (:func:`complete_authorization`). The state travels through Redis, single use, ten minutes. Tokens are
stored encrypted. The scope is ``calendar.events`` (events only). When Google stops accepting the saved sign-in - or the calendar keeps refusing events - the connection is
paused (``needs_reconnect``) and the business gets an email.

Only tested against a fake Google (an httpx mock transport). No real Google account, OAuth client or calendar has been used.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import logging
import re
import secrets
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import quote, urlencode

import httpx
from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import tenant_session
from app.core.exceptions import CalendarNotConfigured, NotFound, ValidationFailed
from app.core.redis import get_redis
from app.core.security import decrypt_field, encrypt_field, hash_api_key
from app.models import Booking, BookingAllocation, BookingItem, Customer, CustomerAddress, Resource, Service, Tenant
from app.models.calendar import CalendarConnection, CalendarEvent
from app.models.events import OutboxEvent
from app.services import events, outbox

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/calendar.events"
#: A booking has an event while it is confirmed, under way or done; pending (not yet answered), cancelled, rejected and no-show ones do not.
WANTED_STATUSES = frozenset({"confirmed", "in-progress", "waiting-for-parts", "completed"})
#: The outbox events after which a booking's calendar events may have to change.
TRIGGER_EVENTS = frozenset({
    "booking.created", "booking.confirmed", "booking.rejected", "booking.cancelled", "booking.rescheduled",
    "booking.in_progress", "booking.waiting_for_parts", "booking.completed", "booking.no_show",
    "booking.meeting_link_ready",  # (the event then shows the new link)
})
CONNECTION_LOST_EVENT = "calendar.connection_lost"
STATE_PREFIX = "gcal:state:"
_CALENDAR_ID = re.compile(r"^[A-Za-z0-9._@%+#-]{1,254}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
#: Google answers a 403 with one of these reasons when it only wants us to slow down.
_SLOW_DOWN = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError"})
_NAMESPACE = uuid.UUID("6f1c9a4e-2b7d-4c58-9e1a-0d3f5b8a7c21")

#: Replaced in tests with a mock transport.
_transport: httpx.AsyncBaseTransport | None = None


# --------------------------------------------------------------------------- talking to Google
class GoogleError(Exception):
    """Base of what goes wrong when asking Google. The text is safe to log and to store (no token, no address)."""


class Retryable(GoogleError):
    """Not now: Google is slow, down or asked us to slow down."""


class Rejected(GoogleError):
    """Google refused this request and asking again will not change that."""


class TokenRejected(GoogleError):
    """Google no longer accepts the saved sign-in (revoked, expired, or the OAuth client changed): the owner has to connect again."""


class AccessExpired(GoogleError):
    """The access token was refused (a 401): refresh it and try once more."""


@dataclasses.dataclass(frozen=True)
class Tokens:
    access_token: str
    expires_in: int
    refresh_token: str | None = None


class GoogleApi:
    """The few Google endpoints the sync uses: OAuth (authorise, exchange, refresh, revoke) and Calendar (read a calendar, write and delete an event)."""

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.client_id = settings.google_oauth_client_id
        self.client_secret = settings.google_oauth_client_secret.get_secret_value()
        self.redirect_uri = settings.google_oauth_redirect_uri
        self.accounts_url, self.token_url, self.revoke_url = settings.google_accounts_url, settings.google_token_url, settings.google_revoke_url
        self.calendar_url = settings.google_calendar_url.rstrip("/")
        self.timeout, self.transport = settings.calendar_timeout_seconds, transport

    async def _send(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport, trust_env=False) as client:
                return await client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:  # (the exception text can carry the address, and the address can carry ids: keep only the type)
            raise Retryable(f"could not reach Google ({type(exc).__name__})") from exc

    # --- OAuth
    def authorization_url(self, state: str) -> str:
        query = urlencode({
            "client_id": self.client_id, "redirect_uri": self.redirect_uri, "response_type": "code", "scope": SCOPE,
            "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false", "state": state,
        })
        return f"{self.accounts_url}?{query}"

    async def _tokens(self, response: httpx.Response) -> Tokens:
        try:
            body = response.json()
            error = str(body.get("error", "")) if isinstance(body, dict) else ""
        except ValueError:
            body, error = {}, ""
        if response.status_code == 200 and isinstance(body, dict) and body.get("access_token"):
            try:
                return Tokens(str(body["access_token"]), int(body.get("expires_in", 3600)), str(body["refresh_token"]) if body.get("refresh_token") else None)
            except (TypeError, ValueError) as exc:
                raise Retryable("unreadable answer from Google") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise Retryable(f"Google answered HTTP {response.status_code}")
        if error == "invalid_grant":
            raise TokenRejected("the saved sign-in was revoked or has expired")
        if error in ("invalid_client", "unauthorized_client"):
            log.error("calendar: Google refused our OAuth client (%s): check GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET", error)
        raise Rejected(f"Google refused the sign-in ({error or 'HTTP ' + str(response.status_code)})")

    async def exchange_code(self, code: str) -> Tokens:
        return await self._tokens(await self._send("POST", self.token_url, data={
            "code": code, "client_id": self.client_id, "client_secret": self.client_secret, "redirect_uri": self.redirect_uri, "grant_type": "authorization_code",
        }))

    async def refresh(self, refresh_token: str) -> Tokens:
        return await self._tokens(await self._send("POST", self.token_url, data={
            "refresh_token": refresh_token, "client_id": self.client_id, "client_secret": self.client_secret, "grant_type": "refresh_token",
        }))

    async def revoke(self, token: str) -> None:
        """Best effort: the connection is removed here whether or not Google heard."""
        try:
            await self._send("POST", self.revoke_url, data={"token": token})
        except GoogleError:
            log.warning("calendar: could not tell Google to revoke a saved sign-in")

    # --- Calendar
    @staticmethod
    def _auth(access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}

    @staticmethod
    def _check(response: httpx.Response, ok: tuple[int, ...]) -> None:
        if response.status_code in ok:
            return
        reason, message = "", ""
        try:
            error = response.json().get("error", {})
            if isinstance(error, dict):
                message = str(error.get("message", ""))
                reason = str((error.get("errors") or [{}])[0].get("reason", ""))
        except (ValueError, AttributeError, IndexError, TypeError):
            pass
        if response.status_code == 401:
            raise AccessExpired("the access token was refused")
        if response.status_code == 429 or response.status_code >= 500 or reason in _SLOW_DOWN:
            raise Retryable(f"Google answered HTTP {response.status_code}{' ' + reason if reason else ''}")
        raise Rejected(_CONTROL.sub(" ", f"Google answered HTTP {response.status_code} {reason}: {message}")[:300])

    def _events_url(self, calendar_id: str) -> str:
        return f"{self.calendar_url}/calendars/{quote(calendar_id, safe='')}/events"

    async def calendar_summary(self, access_token: str, calendar_id: str) -> str:
        """The calendar's name; fails (``Rejected``) if the account cannot see or write it."""
        response = await self._send("GET", f"{self.calendar_url}/calendars/{quote(calendar_id, safe='')}", headers=self._auth(access_token))
        self._check(response, (200,))
        try:
            return str(response.json().get("summary", ""))[:200]
        except (ValueError, AttributeError) as exc:
            raise Retryable("unreadable answer from Google") from exc

    async def upsert_event(self, access_token: str, calendar_id: str, event_id: str, body: dict[str, Any], *, create_first: bool) -> None:
        """Make the event exist with this content. ``create_first``: it is not known to exist yet (insert, and update if the id turns out to be taken); otherwise update first
        (and insert if it is gone)."""
        url, params, headers = self._events_url(calendar_id), {"sendUpdates": "none"}, self._auth(access_token)
        if create_first:
            response = await self._send("POST", url, params=params, json=body, headers=headers)
            if response.status_code == 409:  # a retry after a crash: it was made already
                response = await self._send("PUT", f"{url}/{event_id}", params=params, json=body, headers=headers)
            self._check(response, (200,))
            return
        response = await self._send("PUT", f"{url}/{event_id}", params=params, json=body, headers=headers)
        if response.status_code == 404:  # deleted in Google by somebody: put it back
            response = await self._send("POST", url, params=params, json=body, headers=headers)
            if response.status_code == 409:
                raise Retryable("the event id is taken but the event cannot be updated")
        self._check(response, (200,))

    async def delete_event(self, access_token: str, calendar_id: str, event_id: str) -> None:
        response = await self._send("DELETE", f"{self._events_url(calendar_id)}/{event_id}", params={"sendUpdates": "none"}, headers=self._auth(access_token))
        self._check(response, (200, 204, 404, 410))  # already gone is as good as deleted


def google_api(settings: Settings | None = None) -> GoogleApi:
    return GoogleApi(settings or get_settings(), _transport)


def is_configured(settings: Settings) -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret.get_secret_value() and settings.google_oauth_redirect_uri)


# --------------------------------------------------------------------------- what an event says
def clean(value: object, limit: int = 200) -> str:
    return _CONTROL.sub(" ", str(value)).strip()[:limit]


def build_event(
    *, booking_id: uuid.UUID, status: str, start_at: dt.datetime, end_at: dt.datetime, timezone: str, services: list[str], staff: list[str],
    share: bool, customer_name: str | None = None, customer_phone: str | None = None, party_size: int | None = None,
    request_text: str | None = None, address: str | None = None, meeting_link: str | None = None,
) -> dict[str, Any]:
    """The Google event for a booking. Without ``share`` it holds the services, staff, status, a booking reference and the join link of an online appointment only."""
    summary = ", ".join(clean(s) for s in services) or "Booking"
    lines = [f"Booking {str(booking_id)[:8]}", f"Status: {status}"]
    if staff:
        lines.append("With: " + ", ".join(clean(s) for s in staff))
    if meeting_link:
        lines.append(f"Join online: {clean(meeting_link, 500)}")
    if share:
        if customer_name:
            summary = f"{summary} - {clean(customer_name, 100)}"
        if customer_phone:
            lines.append(f"Phone: {clean(customer_phone, 40)}")
        if party_size:
            lines.append(f"Party of {party_size}")
        if request_text:
            lines.append(f"Request: {clean(request_text, 500)}")
    body: dict[str, Any] = {
        "status": "confirmed", "summary": summary[:300], "description": "\n".join(lines)[:2000],
        "start": {"dateTime": start_at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": timezone},
        "end": {"dateTime": end_at.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "timeZone": timezone},
        "extendedProperties": {"private": {"bookingId": str(booking_id)}},
        "reminders": {"useDefault": True},
    }
    if share and address:
        body["location"] = clean(address, 300)
    return body


def content_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def event_id_for(row_id: uuid.UUID) -> str:
    """Google accepts our own event ids of 5-1024 characters from ``a-v0-9``; 32 hex digits qualify. Derived from the row, never random, so a retry finds its event."""
    return uuid.uuid5(_NAMESPACE, str(row_id)).hex


async def staff_of(db: AsyncSession, booking_id: uuid.UUID) -> set[uuid.UUID]:
    """The staff members a booking belongs to: those allocated to it now, or (once it is over and the allocations are released) those that had it."""
    rows = (await db.execute(
        select(BookingAllocation.resource_id, BookingAllocation.state)
        .join(Resource, Resource.id == BookingAllocation.resource_id)
        .where(BookingAllocation.booking_id == booking_id, Resource.type == "staff")
    )).all()
    live = {rid for rid, state in rows if state in ("held", "active")}
    return live or {rid for rid, _ in rows}


async def _describe(db: AsyncSession, booking: Booking, connection: CalendarConnection, timezone: str) -> dict[str, Any]:
    services = list((await db.scalars(
        select(Service.name).join(BookingItem, BookingItem.service_id == Service.id).where(BookingItem.booking_id == booking.id).order_by(BookingItem.seq)
    )).all())
    staff_ids = await staff_of(db, booking.id)
    staff = list((await db.scalars(select(Resource.name).where(Resource.id.in_(staff_ids)).order_by(Resource.name))).all()) if staff_ids else []
    name = phone = address = None
    if connection.share_customer_details:
        customer = await db.get(Customer, booking.customer_id)
        if customer is not None and customer.erased_at is None:
            name, phone = customer.name, customer.phone
        place = await db.get(CustomerAddress, booking.address_id) if booking.address_id else None
        if place is not None and customer is not None and customer.erased_at is None:
            address = ", ".join(part for part in (place.line1, place.postcode, place.city) if part)
    return build_event(
        booking_id=booking.id, status=booking.status, start_at=booking.start_at, end_at=booking.end_at, timezone=timezone, services=services, staff=staff,
        share=connection.share_customer_details, customer_name=name, customer_phone=phone, party_size=booking.party_size, request_text=booking.special_request,
        address=address, meeting_link=booking.meeting_link,
    )


# --------------------------------------------------------------------------- nudging: which events must be looked at again
async def mark_booking(db: AsyncSession, booking_id: uuid.UUID, *, due: dt.datetime | None = None) -> int:
    """Mark the calendar events of one booking ``pending``: those it should have in the connected calendars, and those it already has (to be removed if it no longer should).
    ``due`` is when they may be written (the moment of the change, so the sweep that publishes it also writes it); default now. Returns how many rows were marked."""
    connections = list((await db.scalars(select(CalendarConnection).where(CalendarConnection.status == "active"))).all())
    if not connections:
        return 0
    booking = await db.get(Booking, booking_id)
    targets: set[uuid.UUID] = set()
    if booking is not None and booking.status in WANTED_STATUSES:
        staff = await staff_of(db, booking_id)
        targets = {c.id for c in connections if c.resource_id is None or c.resource_id in staff}
    existing = set((await db.scalars(select(CalendarEvent.connection_id).where(CalendarEvent.booking_id == booking_id))).all())
    chosen = [c for c in connections if c.id in targets | existing]
    if not chosen:
        return 0
    statement = pg_insert(CalendarEvent).values([
        {"tenant_id": c.tenant_id, "connection_id": c.id, "booking_id": booking_id, **({"next_attempt_at": due} if due is not None else {})} for c in chosen
    ])
    await db.execute(statement.on_conflict_do_update(
        constraint="uq_calendar_events_connection_booking",
        set_={"state": "pending", "attempts": 0, "next_attempt_at": due if due is not None else func.clock_timestamp()},
    ))
    return len(chosen)


async def mark_upcoming(db: AsyncSession, connection: CalendarConnection, *, now: dt.datetime | None = None, force: bool = False) -> int:
    """Queue every booking that belongs in this calendar and is not over (or ended within the last day), and everything it already has - on connecting, and on a re-sync.
    ``force`` forgets what was last sent, so every event is written again (after the settings that shape an event changed)."""
    now = now or dt.datetime.now(dt.UTC)
    select_bookings = select(Booking.tenant_id, literal(connection.id, CalendarEvent.connection_id.type), Booking.id).where(
        Booking.status.in_(sorted(WANTED_STATUSES)), Booking.end_at >= now - dt.timedelta(days=1),
    )
    if connection.resource_id is not None:
        select_bookings = select_bookings.where(select(BookingAllocation.id).where(
            BookingAllocation.booking_id == Booking.id, BookingAllocation.resource_id == connection.resource_id).exists())
    statement = pg_insert(CalendarEvent).from_select(["tenant_id", "connection_id", "booking_id"], select_bookings)
    queued = await db.execute(statement.on_conflict_do_nothing(constraint="uq_calendar_events_connection_booking"))
    values: dict[str, Any] = {"state": "pending", "attempts": 0, "next_attempt_at": func.clock_timestamp(), "last_error": None}
    if force:
        values["content_hash"] = None
    reset = await db.execute(update(CalendarEvent).where(CalendarEvent.connection_id == connection.id).values(**values))
    return int(queued.rowcount or 0) + int(reset.rowcount or 0)  # type: ignore[attr-defined]


class CalendarPublisher:
    """An outbox publisher that nudges the calendar events of a booking whenever the booking changes. Sends nothing to Google (the sweep does)."""

    async def publish(self, event: OutboxEvent) -> None:
        if event.aggregate_type != "booking" or event.event_type not in TRIGGER_EVENTS:
            return
        async with tenant_session(event.tenant_id) as db:
            await mark_booking(db, event.aggregate_id, due=event.created_at)


# --------------------------------------------------------------------------- doing the work
@dataclasses.dataclass
class SyncReport:
    synced: int = 0
    removed: int = 0
    unchanged: int = 0
    retried: int = 0
    failed: int = 0
    paused: int = 0


T = TypeVar("T")


async def access_token(api: GoogleApi, connection: CalendarConnection, now: dt.datetime, *, force: bool = False) -> str:
    """A usable access token: the cached one, or a fresh one made from the refresh token (and cached)."""
    if not force and connection.access_token_enc and connection.access_expires_at and connection.access_expires_at > now + dt.timedelta(seconds=60):
        return decrypt_field(connection.access_token_enc, connection.tenant_id)
    tokens = await api.refresh(decrypt_field(connection.refresh_token_enc, connection.tenant_id))
    connection.access_token_enc = encrypt_field(tokens.access_token, connection.tenant_id)
    connection.access_expires_at = now + dt.timedelta(seconds=tokens.expires_in)
    return tokens.access_token


async def _with_token(api: GoogleApi, connection: CalendarConnection, now: dt.datetime, action: Callable[[str], Awaitable[T]]) -> T:
    token = await access_token(api, connection, now)
    try:
        return await action(token)
    except AccessExpired:  # revoked or expired early: one fresh token, one more try
        token = await access_token(api, connection, now, force=True)
        try:
            return await action(token)
        except AccessExpired as exc:
            raise TokenRejected("Google refused a fresh access token") from exc


async def _reconcile(db: AsyncSession, api: GoogleApi, connection: CalendarConnection, row: CalendarEvent, timezone: str, now: dt.datetime) -> str:
    """Make Google match the booking for this row. Returns ``synced``, ``unchanged`` or ``removed``."""
    booking = await db.get(Booking, row.booking_id)
    belongs = booking is not None and booking.status in WANTED_STATUSES and (connection.resource_id is None or connection.resource_id in await staff_of(db, booking.id))
    event_id = row.external_event_id or event_id_for(row.id)
    if not belongs or booking is None:
        if row.external_event_id is not None:  # it may exist in Google (even if the answer was lost): delete it, and "not there" is fine
            await _with_token(api, connection, now, lambda token: api.delete_event(token, connection.calendar_id, event_id))
        await db.delete(row)
        return "removed"
    body = await _describe(db, booking, connection, timezone)
    body["id"] = event_id
    digest = content_hash(body)
    outcome = "unchanged"
    if row.synced_at is None or row.content_hash != digest:
        await _with_token(api, connection, now, lambda token: api.upsert_event(token, connection.calendar_id, event_id, body, create_first=row.synced_at is None))
        outcome = "synced"
    row.external_event_id, row.content_hash = event_id, digest
    row.state, row.attempts, row.last_error, row.synced_at = "synced", 0, None, now
    return outcome


def _pause(db: AsyncSession, connection: CalendarConnection, why: str) -> None:
    """Stop syncing this calendar until the owner connects it again, and tell the business."""
    connection.status, connection.last_error = "needs_reconnect", why[:300]
    events.enqueue(db, connection.tenant_id, CONNECTION_LOST_EVENT, "calendar_connection", connection.id, {"connection_id": str(connection.id), "reason": why[:300]})


async def sync_due(db: AsyncSession, now: dt.datetime | None = None) -> SyncReport:
    """Work off the calendar events that are due (at most ``CALENDAR_BATCH`` per tenant per sweep), one request at a time."""
    now = now or dt.datetime.now(dt.UTC)
    settings = get_settings()
    report = SyncReport()
    rows = (await db.execute(
        select(CalendarEvent, CalendarConnection)
        .join(CalendarConnection, CalendarConnection.id == CalendarEvent.connection_id)
        .where(CalendarEvent.state == "pending", CalendarEvent.next_attempt_at <= now, CalendarConnection.status == "active")
        .order_by(CalendarEvent.next_attempt_at, CalendarEvent.id)
        .limit(settings.calendar_batch)
        .with_for_update(of=CalendarEvent, skip_locked=True)
    )).all()
    if not rows:
        return report
    tenant = await db.get(Tenant, rows[0][0].tenant_id)
    if tenant is None:
        return report
    api = google_api(settings)
    paused: set[uuid.UUID] = set()
    for row, connection in rows:
        if connection.id in paused:
            continue
        try:
            outcome = await _reconcile(db, api, connection, row, tenant.timezone, now)
        except TokenRejected as exc:
            _pause(db, connection, f"Google no longer accepts the saved sign-in ({exc}): connect the calendar again")
            paused.add(connection.id)
            report.paused += 1
            continue
        except (Retryable, Rejected) as exc:
            row.attempts += 1
            row.last_error = str(exc)[:300]
            connection.last_error = row.last_error
            if isinstance(exc, Rejected) or row.attempts >= settings.calendar_max_attempts:
                row.state = "failed"
                connection.consecutive_failures += 1
                report.failed += 1
                if connection.consecutive_failures >= settings.calendar_pause_after_failures:
                    _pause(db, connection, f"the calendar keeps refusing events (last problem: {row.last_error}): check it and connect it again")
                    paused.add(connection.id)
                    report.paused += 1
            else:
                row.next_attempt_at = now + outbox.backoff(row.attempts)
                report.retried += 1
            continue
        connection.consecutive_failures, connection.last_error, connection.last_sync_at = 0, None, now
        if outcome == "synced":
            report.synced += 1
        elif outcome == "removed":
            report.removed += 1
        else:
            report.unchanged += 1
    await db.flush()
    return report


# --------------------------------------------------------------------------- connecting a calendar (OAuth)
def _state_key(state: str) -> str:
    return STATE_PREFIX + hash_api_key(state)


async def begin_authorization(db: AsyncSession, tenant_id: uuid.UUID, *, resource_id: uuid.UUID | None, calendar_id: str, share_customer_details: bool) -> str:
    """Where to send the owner's browser to sign in at Google. Nothing is stored until Google sends the browser back."""
    settings = get_settings()
    if not is_configured(settings):
        raise CalendarNotConfigured("Google Calendar is not set up on this server (GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, GOOGLE_OAUTH_REDIRECT_URI)")
    if not _CALENDAR_ID.match(calendar_id):
        raise ValidationFailed("calendar_id must be 'primary' or the id of a calendar (letters, digits and . _ @ % + # -)")
    if resource_id is not None:
        resource = await db.get(Resource, resource_id)
        if resource is None:
            raise NotFound("Resource not found")
        if resource.type != "staff":
            raise ValidationFailed("Only a staff member can have a calendar of their own; leave resource_id out for the business's calendar")
    state = secrets.token_urlsafe(32)
    await get_redis().set(
        _state_key(state), json.dumps({"tenant_id": str(tenant_id), "resource_id": str(resource_id) if resource_id else None, "calendar_id": calendar_id, "share": share_customer_details}),
        ex=settings.calendar_state_ttl_seconds,
    )
    return google_api(settings).authorization_url(state)


@dataclasses.dataclass(frozen=True)
class Completion:
    ok: bool
    reason: str | None = None
    connection_id: uuid.UUID | None = None


async def complete_authorization(*, code: str | None, state: str | None, error: str | None) -> Completion:
    """Google sent the browser back. Exchange the code, check the calendar can be written, store the connection and queue what belongs in it. Never raises:
    the answer is a :class:`Completion` whose ``reason`` is safe to show."""
    if not state:
        return Completion(False, "invalid_state")
    raw = await get_redis().getdel(_state_key(state))  # atomic: the state works once
    if not raw:
        return Completion(False, "invalid_state")
    if error:
        return Completion(False, "denied" if error == "access_denied" else "google_error")
    if not code:
        return Completion(False, "google_error")
    request = json.loads(raw)
    tenant_id = uuid.UUID(request["tenant_id"])
    resource_id = uuid.UUID(request["resource_id"]) if request["resource_id"] else None
    api = google_api()
    try:
        tokens = await api.exchange_code(code)
        if not tokens.refresh_token:
            return Completion(False, "no_refresh_token")
        label = await api.calendar_summary(tokens.access_token, request["calendar_id"])
    except Retryable:
        return Completion(False, "google_unavailable")
    except (Rejected, TokenRejected, AccessExpired):
        return Completion(False, "google_refused")
    now = dt.datetime.now(dt.UTC)
    try:
        async with tenant_session(tenant_id) as db:
            tenant = await db.get(Tenant, tenant_id)
            if tenant is None or tenant.status != "active":
                return Completion(False, "invalid_state")
            where = CalendarConnection.resource_id.is_(None) if resource_id is None else CalendarConnection.resource_id == resource_id
            connection = await db.scalar(select(CalendarConnection).where(where))
            if connection is None:
                connection = CalendarConnection(tenant_id=tenant_id, resource_id=resource_id)
                db.add(connection)
            connection.calendar_id, connection.account_label = request["calendar_id"], label or None
            connection.refresh_token_enc = encrypt_field(tokens.refresh_token, tenant_id)
            connection.access_token_enc = encrypt_field(tokens.access_token, tenant_id)
            connection.access_expires_at = now + dt.timedelta(seconds=tokens.expires_in)
            connection.status, connection.consecutive_failures, connection.last_error = "active", 0, None
            connection.share_customer_details = bool(request["share"])
            await db.flush()
            await mark_upcoming(db, connection, now=now, force=True)
            connection_id = connection.id
    except IntegrityError:  # the staff member was removed while the owner was signing in
        return Completion(False, "resource_gone")
    return Completion(True, None, connection_id)


async def disconnect(db: AsyncSession, connection: CalendarConnection) -> None:
    """Forget the calendar: tell Google to revoke the saved sign-in (best effort), delete the connection and its event records. Events already in Google stay there."""
    token = decrypt_field(connection.refresh_token_enc, connection.tenant_id)
    await db.delete(connection)
    await db.flush()
    await google_api().revoke(token)
