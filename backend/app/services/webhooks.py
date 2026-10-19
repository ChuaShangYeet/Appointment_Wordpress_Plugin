"""Outgoing webhooks (blueprint §3, §14): a business subscribes its own URLs to booking events.

How an event reaches a URL:

1. The outbox publishes an event. :class:`WebhookPublisher` (one of the worker's publishers) writes one ``webhook_deliveries`` row for every
   active endpoint subscribed to that event type, with the JSON envelope copied in. It never sends anything and never fails an event, so a
   dead customer URL can neither delay the outbox nor hold back another consumer (the customer's confirmation email) of the same booking.
2. The sweep calls :func:`deliver_due`: due deliveries are POSTed (concurrently, short timeouts), and each is retried on its own with the
   outbox's back-off until ``WEBHOOK_MAX_ATTEMPTS``, then left ``failed`` (an owner can redeliver it). An endpoint that fails
   ``WEBHOOK_DISABLE_AFTER_FAILURES`` times in a row is switched off, with its pending deliveries failed, until an owner turns it back on.

The wire format (documented in the README for whoever builds the receiver):

* body: ``{"id": <event id>, "type": ..., "seq": ..., "created_at": ..., "tenant_id": ..., "data": {ids, status and times only}}``;
* ``X-Webhook-Id`` (the event: identical for every endpoint and every retry - deduplicate on it), ``X-Webhook-Delivery``, ``X-Webhook-Event``,
  ``X-Webhook-Timestamp`` (unix seconds, fresh on every attempt) and ``X-Webhook-Signature: v1=<hex HMAC-SHA256 of "{timestamp}.{body}">``
  with the endpoint's secret - the scheme the WordPress sync already uses, so receivers check it the same way and reject old timestamps.
* delivery is at least once and **not ordered** (a retry of an earlier event can arrive after a later one): use ``seq`` / ``created_at``.

**The URL is chosen by a tenant, so the server must not be talkable into reaching its own network (SSRF).** Defences, all enforced both when
the URL is registered and again at send time: https only; no credentials in the URL; the host is resolved by us and *every* address must
be a public one (no loopback, private, link-local - which includes cloud metadata endpoints -, shared, multicast, reserved, or an IPv6
address that embeds a non-public IPv4 one); the connection then goes to the address we validated (so a DNS answer that changes between
check and connect - rebinding - gains nothing), with the real host name as ``Host`` header and as the TLS name that the certificate must
match; redirects are not followed; a response body is never read; timeouts are short.
``WEBHOOK_ALLOW_INSECURE=true`` (development only) lifts the https and public-address rules so a receiver on localhost can be used.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import ipaddress
import json
import logging
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import ValidationFailed
from app.core.security import decrypt_field, sign_payload
from app.models.events import OutboxEvent
from app.models.webhooks import WEBHOOK_EVENTS, WebhookDelivery, WebhookEndpoint
from app.services import events, outbox

log = logging.getLogger(__name__)

MAX_URL_LENGTH = 500
MAX_ENDPOINTS = 10
USER_AGENT = "JustSimple-Webhooks/1"


class UnsafeUrl(ValidationFailed):
    """The URL cannot be used as a webhook target."""

    code = "unsafe_webhook_url"


# --------------------------------------------------------------------------- name resolution and the safety rules
Resolver = Callable[[str], Awaitable[list[str]]]


async def system_resolver(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


_resolver: Resolver = system_resolver
_transport: httpx.AsyncBaseTransport | None = None
_verify: Any = True


def set_resolver(resolver: Resolver | None) -> None:
    global _resolver
    _resolver = resolver or system_resolver


def set_transport(transport: httpx.AsyncBaseTransport | None, verify: Any = True) -> None:
    """Replace the HTTP transport (tests) and, optionally, the TLS trust (an ``ssl.SSLContext``)."""
    global _transport, _verify
    _transport, _verify = transport, verify


def is_public(address: str) -> bool:
    """Whether ``address`` is an ordinary public internet address."""
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public(str(ip.ipv4_mapped))
        if int(ip) >> 120 == 0 or ip.is_site_local:  # ::/8 (reserved: the unspecified and loopback addresses and the obsolete IPv4-compatible form ``::127.0.0.1``) and fec0::/10 (site-local): never public
            return False
        if ip.sixtofour is not None and not is_public(str(ip.sixtofour)):
            return False
        if ip.teredo is not None or ip in ipaddress.ip_network("64:ff9b::/96"):  # tunnels that can hide a private IPv4 address
            return False
    return bool(ip.is_global) and not ip.is_multicast


#: The one answer for a host that cannot be used: it does not resolve, resolves too slowly, or resolves to a private or reserved address. One text for all of them, so
#: registering a webhook cannot be used to probe which internal host names exist.
UNUSABLE_HOST = "The host name does not resolve to a public server; webhooks can only be sent to public servers"


def _host_ips(host: str) -> list[str] | None:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        return None


async def resolve_public(host: str, *, allow_private: bool = False) -> list[str]:
    """The addresses to connect to for ``host``, all of which must be public (unless ``allow_private``)."""
    literal = _host_ips(host)
    try:
        # (the name server is someone else's: without a limit a name whose server never answers would hold the sweep for every business)
        ips = literal or await asyncio.wait_for(_resolver(host), timeout=get_settings().webhook_timeout_seconds)
    except OSError as exc:  # (a time-out is an OSError too)
        raise UnsafeUrl(UNUSABLE_HOST) from exc
    if not ips:
        raise UnsafeUrl(UNUSABLE_HOST)
    if not allow_private and not all(is_public(ip) for ip in ips):
        raise UnsafeUrl(UNUSABLE_HOST)
    return ips


def _parse(url: str, *, allow_insecure: bool) -> tuple[str, int | None, str]:
    """``(host, port, scheme)`` of a syntactically acceptable webhook URL."""
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeUrl(f"The URL is longer than {MAX_URL_LENGTH} characters")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeUrl("That is not a valid URL") from exc
    if parts.scheme not in (("https", "http") if allow_insecure else ("https",)):
        raise UnsafeUrl("Webhook URLs must use https")
    if not parts.hostname:
        raise UnsafeUrl("The URL has no host")
    if parts.username is not None or parts.password is not None:
        raise UnsafeUrl("The URL must not contain a user name or password")
    if parts.fragment:
        raise UnsafeUrl("The URL must not contain a fragment")
    if any(ord(ch) < 33 or ord(ch) == 127 for ch in url):
        raise UnsafeUrl("The URL contains spaces or control characters")
    if port == 0:
        raise UnsafeUrl("That is not a valid port")
    return parts.hostname.lower(), port, parts.scheme


async def check_url(url: str) -> None:
    """Refuse a URL that cannot safely be a webhook target (raises :class:`UnsafeUrl`)."""
    allow = get_settings().webhook_allow_insecure
    host, _, _ = _parse(url, allow_insecure=allow)
    await resolve_public(host, allow_private=allow)


# --------------------------------------------------------------------------- the envelope and the request
def envelope(event: OutboxEvent) -> dict[str, Any]:
    return {
        "id": str(event.id), "type": event.event_type, "seq": event.seq, "created_at": event.created_at.isoformat(),
        "tenant_id": str(event.tenant_id), "data": event.payload,
    }


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def request_headers(secret: str, payload: dict[str, Any], body: bytes, delivery_id: uuid.UUID, timestamp: int | None = None) -> dict[str, str]:
    ts = str(int(time.time()) if timestamp is None else timestamp)
    return {
        "Content-Type": "application/json", "User-Agent": USER_AGENT, "X-Webhook-Id": str(payload["id"]),
        "X-Webhook-Delivery": str(delivery_id), "X-Webhook-Event": str(payload["type"]), "X-Webhook-Timestamp": ts,
        "X-Webhook-Signature": "v1=" + sign_payload(secret, ts, body),
    }


@dataclasses.dataclass(frozen=True)
class SendResult:
    status: int | None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300


def _short(text: str) -> str:
    return text[:300]


async def send(url: str, body: bytes, headers: dict[str, str]) -> SendResult:
    """POST ``body`` to ``url`` safely (see the module notes). Never raises: the outcome is the result."""
    settings = get_settings()
    allow = settings.webhook_allow_insecure
    try:
        host, port, scheme = _parse(url, allow_insecure=allow)
        ips = await resolve_public(host, allow_private=allow)
    except UnsafeUrl as exc:
        return SendResult(None, _short(f"blocked: {exc.message}"))
    parts = urlsplit(url)
    default_port = 443 if scheme == "https" else 80
    host_header = host if port in (None, default_port) else f"{host}:{port}"
    timeout = httpx.Timeout(settings.webhook_timeout_seconds)
    last = "could not connect"
    async with httpx.AsyncClient(transport=_transport, timeout=timeout, follow_redirects=False, verify=_verify, trust_env=False) as client:
        for ip in ips[:3]:
            literal = f"[{ip}]" if ":" in ip else ip
            netloc = literal if port is None else f"{literal}:{port}"
            target = parts._replace(netloc=netloc).geturl()  # the validated address, not the name: a changed DNS answer is not followed
            try:
                async with client.stream(
                    "POST", target, content=body, headers={**headers, "Host": host_header},
                    extensions={"sni_hostname": host} if scheme == "https" else {},
                ) as response:
                    status = response.status_code  # the body is never read
                return SendResult(status, None if 200 <= status < 300 else f"HTTP {status}")
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last = f"could not connect ({type(exc).__name__})"
                continue  # try the host's next address
            except httpx.HTTPError as exc:
                return SendResult(None, _short(f"{type(exc).__name__}"))
    return SendResult(None, last)


# --------------------------------------------------------------------------- publishing (fan-out) and delivery
class WebhookPublisher:
    """An outbox publisher that records what each subscribed endpoint is owed. Sends nothing."""

    async def publish(self, event: OutboxEvent) -> None:
        if event.event_type not in WEBHOOK_EVENTS:
            return
        payload = envelope(event)
        async with tenant_session(event.tenant_id) as db:
            endpoints = (await db.scalars(
                select(WebhookEndpoint).where(
                    WebhookEndpoint.active.is_(True),
                    WebhookEndpoint.events.overlap(["*", event.event_type]),
                )
            )).all()
            for endpoint in endpoints:
                await db.execute(
                    pg_insert(WebhookDelivery)
                    .values(tenant_id=event.tenant_id, endpoint_id=endpoint.id, outbox_event_id=event.id, event_type=event.event_type, payload=payload,
                            next_attempt_at=event.created_at)  # due as soon as the event is: the sweep that publishes it sends it too
                    .on_conflict_do_nothing(constraint="uq_webhook_deliveries_endpoint_event")
                )


@dataclasses.dataclass
class DeliveryReport:
    delivered: int = 0
    failed_attempts: int = 0
    gave_up: int = 0
    endpoints_disabled: int = 0


ENDPOINT_DISABLED_EVENT = "webhook.endpoint_disabled"


async def disable_endpoint(db: AsyncSession, endpoint: WebhookEndpoint, reason: str, *, alert: bool = False, last_error: str | None = None) -> None:
    """Switch an endpoint off; what it was still owed is marked failed (it can be redelivered once the endpoint is back).

    With ``alert`` (the system did it, nobody asked) an outbox event is queued in the same transaction, which the notification
    publisher turns into an email to the business - otherwise a dead receiver would just stop getting bookings, silently.
    The event carries ids, a count and the last error (``HTTP 500``, a class name: never the URL, which can hold secrets - the host
    is looked up when the email is written).
    """
    endpoint.active, endpoint.disabled_reason = False, reason
    if alert:
        events.enqueue(
            db, endpoint.tenant_id, ENDPOINT_DISABLED_EVENT, "webhook", endpoint.id,
            {"endpoint_id": str(endpoint.id), "reason": reason, "consecutive_failures": endpoint.consecutive_failures, "last_error": last_error},
        )
    await db.execute(
        update(WebhookDelivery)
        .where(WebhookDelivery.endpoint_id == endpoint.id, WebhookDelivery.status == "pending")
        .values(status="failed", error="endpoint disabled")
    )


async def deliver_due(db: AsyncSession, now: dt.datetime | None = None) -> DeliveryReport:
    """Send the deliveries that are due (at most ``WEBHOOK_BATCH``, ``WEBHOOK_CONCURRENCY`` at a time) and record each outcome."""
    now = now or dt.datetime.now(dt.UTC)
    settings = get_settings()
    report = DeliveryReport()
    rows = (await db.execute(
        select(WebhookDelivery, WebhookEndpoint)
        .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
        .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= now, WebhookEndpoint.active.is_(True))
        .order_by(WebhookDelivery.created_at, WebhookDelivery.id)
        .limit(settings.webhook_batch)
        .with_for_update(of=WebhookDelivery, skip_locked=True)
    )).all()
    gate = asyncio.Semaphore(max(1, settings.webhook_concurrency))

    async def attempt(delivery: WebhookDelivery, endpoint: WebhookEndpoint) -> SendResult:
        body = encode(delivery.payload)
        secret = decrypt_field(endpoint.secret_enc, endpoint.tenant_id)
        async with gate:
            return await send(endpoint.url, body, request_headers(secret, delivery.payload, body, delivery.id))

    results = await asyncio.gather(*(attempt(d, e) for d, e in rows))
    for (delivery, endpoint), result in zip(rows, results, strict=True):
        delivery.attempts += 1
        delivery.last_attempt_at = now
        delivery.response_status = result.status
        if result.ok:
            delivery.status, delivery.delivered_at, delivery.error = "delivered", now, None
            endpoint.consecutive_failures, endpoint.last_success_at = 0, now
            report.delivered += 1
            continue
        delivery.error = result.error
        endpoint.consecutive_failures += 1
        endpoint.last_failure_at = now
        report.failed_attempts += 1
        if delivery.attempts >= settings.webhook_max_attempts:
            delivery.status = "failed"
            report.gave_up += 1
        else:
            delivery.next_attempt_at = now + outbox.backoff(delivery.attempts)
        if endpoint.active and endpoint.consecutive_failures >= settings.webhook_disable_after_failures:
            await disable_endpoint(db, endpoint, "too many consecutive failures", alert=True, last_error=result.error)
            report.endpoints_disabled += 1
    await db.flush()
    return report


async def send_test(endpoint: WebhookEndpoint) -> SendResult:
    """One signed ``webhook.test`` request, now. Not recorded as a delivery and not counted against the endpoint."""
    payload: dict[str, Any] = {
        "id": str(uuid.uuid4()), "type": "webhook.test", "seq": 0, "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "tenant_id": str(endpoint.tenant_id), "data": {"message": "This is a test from your booking system."},
    }
    body = encode(payload)
    return await send(endpoint.url, body, request_headers(decrypt_field(endpoint.secret_enc, endpoint.tenant_id), payload, body, uuid.uuid4()))


async def purge_old(db: AsyncSession, *, older_than: dt.datetime) -> int:
    """Delete finished deliveries (delivered or failed) created before ``older_than``."""
    result = await db.execute(delete(WebhookDelivery).where(WebhookDelivery.status != "pending", WebhookDelivery.created_at < older_than))
    return int(result.rowcount or 0)  # type: ignore[attr-defined]
