"""Outgoing webhooks: registration, SSRF protection, signing, fan-out, delivery, retries, auto-disable, and independence from emails."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import ssl
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from app import worker
from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import ValidationFailed
from app.core.security import decrypt_field, verify_signature
from app.models.events import OutboxEvent
from app.models.webhooks import WEBHOOK_EVENTS, WebhookDelivery, WebhookEndpoint
from app.services import outbox
from app.services import webhooks as hooks
from app.services.notifications import NotificationPublisher
from app.services.sweep import sweep_all
from app.services.webhooks import UnsafeUrl, WebhookPublisher, check_url, is_public
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_customer_cancel import PHONE, cancel, login, otp  # noqa: F401
from tests.test_customer_identity import EMAIL, booked_with_email

PUBLIC = "93.184.216.34"
URL = "https://hooks.example.com/booking"
H = dt.timedelta(hours=1)


# --------------------------------------------------------------------- fixtures
class Receiver:
    """Stands in for the internet: records every request and answers as told."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.respond: Callable[[httpx.Request], httpx.Response | Exception] = lambda request: httpx.Response(200)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.respond(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(r.content) for r in self.requests]


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Iterator[Receiver]:
    """DNS answers with a public address for every name, and every request goes to a recording receiver."""
    receiver = Receiver()

    async def dns(host: str) -> list[str]:
        return [PUBLIC]

    hooks.set_resolver(dns)
    hooks.set_transport(httpx.MockTransport(receiver))
    yield receiver
    hooks.set_resolver(None)
    hooks.set_transport(None)


@pytest.fixture
def worker_publishers(monkeypatch: pytest.MonkeyPatch, mail: Recorder) -> Recorder:
    monkeypatch.setattr(outbox, "_publisher", outbox.CompositePublisher(NotificationPublisher(), WebhookPublisher()))
    return mail


async def add_hook(s: Seed, url: str = URL, events: list[str] | None = None, **extra: Any) -> dict[str, Any]:
    r = await s.client.post("/v1/admin/webhooks", json={"url": url, "events": events or ["*"], **extra}, headers=s.admin)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def tick(s: Seed, now: dt.datetime | None = None) -> Any:
    report = await sweep_all(tenant_ids=[s.tenant_id], now=now)
    assert report.ran and not report.errors, report
    return report.tenants[0]


async def deliveries(s: Seed, hook_id: str | None = None) -> list[WebhookDelivery]:
    async with tenant_session(s.tenant_id) as db:
        q = select(WebhookDelivery).order_by(WebhookDelivery.created_at, WebhookDelivery.id)
        if hook_id:
            q = q.where(WebhookDelivery.endpoint_id == uuid.UUID(hook_id))
        return list((await db.scalars(q)).all())


async def endpoint(s: Seed, hook_id: str) -> WebhookEndpoint:
    async with tenant_session(s.tenant_id) as db:
        row = await db.get(WebhookEndpoint, uuid.UUID(hook_id))
        assert row is not None
        return row


# --------------------------------------------------------------------- pure: which addresses are "public"
@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111"])
def test_ordinary_internet_addresses_are_public(address: str) -> None:
    assert is_public(address)


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.1.2.3", "10.0.0.1", "172.16.5.4", "172.31.255.255", "192.168.1.1", "169.254.169.254", "169.254.0.1", "0.0.0.0",
    "100.64.0.1", "100.127.255.254", "224.0.0.1", "255.255.255.255", "192.0.0.1", "198.18.0.1", "240.0.0.1", "192.0.2.1",
    "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1", "::ffff:10.0.0.1", "::ffff:127.0.0.1", "::ffff:169.254.169.254",
    "2002:0a00:0001::1", "2002:7f00:0001::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2", "64:ff9b::a00:1", "64:ff9b::7f00:1",
])
def test_private_loopback_link_local_and_reserved_addresses_are_not(address: str) -> None:
    assert not is_public(address)


# --------------------------------------------------------------------- pure: URL rules
async def test_a_public_https_url_is_accepted(net: Receiver) -> None:
    await check_url("https://hooks.example.com/booking?x=1")
    await check_url("https://hooks.example.com:8443/in")
    await check_url("https://93.184.216.34/in")


@pytest.mark.parametrize("url", [
    "http://hooks.example.com/in", "ftp://hooks.example.com/in", "hooks.example.com/in", "//hooks.example.com/in", "https:///in", "https://",
    "https://user:pass@hooks.example.com/in", "https://user@hooks.example.com/in", "https://hooks.example.com/in#frag",
    "https://hooks.example.com:0/in", "https://hooks.example.com:99999/in", "https://hooks.example.com/ in", "https://hooks.example.com/in\nHost: x",
    "https://" + "a" * 600 + ".example.com/", "file:///etc/passwd", "javascript:alert(1)",
])
async def test_malformed_or_unsafe_url_shapes_are_refused(net: Receiver, url: str) -> None:
    with pytest.raises(UnsafeUrl):
        await check_url(url)


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/in", "https://localhost/in", "https://10.1.2.3/in", "https://[::1]/in", "https://169.254.169.254/latest/meta-data/",
    "https://192.168.0.10:8443/in", "https://[::ffff:10.0.0.1]/in", "https://0.0.0.0/in", "https://100.64.0.1/in", "https://[fd00::1]/in",
])
async def test_addresses_on_internal_networks_are_refused(url: str) -> None:
    async def dns(host: str) -> list[str]:
        return ["127.0.0.1"] if host == "localhost" else [PUBLIC]

    hooks.set_resolver(dns)
    try:
        with pytest.raises(UnsafeUrl):
            await check_url(url)
    finally:
        hooks.set_resolver(None)


async def test_a_name_that_resolves_inside_the_network_is_refused_even_if_one_answer_is_public() -> None:
    answers: dict[str, list[str]] = {
        "internal.example.com": ["10.0.0.5"], "mixed.example.com": [PUBLIC, "10.0.0.5"], "meta.example.com": ["169.254.169.254"],
        "v6.example.com": ["fd00::1"], "mapped.example.com": ["::ffff:192.168.1.1"], "good.example.com": [PUBLIC, "2606:4700:4700::1111"],
    }

    async def dns(host: str) -> list[str]:
        if host not in answers:
            raise OSError("no such host")
        return answers[host]

    hooks.set_resolver(dns)
    try:
        for host in ("internal", "mixed", "meta", "v6", "mapped"):
            with pytest.raises(UnsafeUrl):
                await check_url(f"https://{host}.example.com/in")
        await check_url("https://good.example.com/in")
        with pytest.raises(UnsafeUrl, match="does not resolve"):
            await check_url("https://nowhere.example.com/in")
    finally:
        hooks.set_resolver(None)


async def test_the_development_switch_allows_http_and_local_receivers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def dns(host: str) -> list[str]:
        return ["127.0.0.1"]

    hooks.set_resolver(dns)
    try:
        with pytest.raises(UnsafeUrl):
            await check_url("http://localhost:9000/in")
        monkeypatch.setattr(get_settings(), "webhook_allow_insecure", True)
        await check_url("http://localhost:9000/in")
        await check_url("https://localhost:9000/in")
        with pytest.raises(UnsafeUrl):
            await check_url("ftp://localhost/in")  # even then, only http(s)
    finally:
        hooks.set_resolver(None)


# --------------------------------------------------------------------- registration
async def test_registering_a_webhook_returns_the_secret_once_and_stores_it_encrypted(seeded: Seed, net: Receiver) -> None:
    created = await add_hook(seeded, events=["booking.confirmed", "booking.cancelled"], description="WordPress")
    assert created["secret"].startswith("whsec_") and len(created["secret"]) > 30
    assert created["events"] == ["booking.cancelled", "booking.confirmed"] and created["active"] is True and created["description"] == "WordPress"
    listing = (await seeded.client.get("/v1/admin/webhooks", headers=seeded.admin)).json()
    assert [w["id"] for w in listing] == [created["id"]] and "secret" not in listing[0] and "secret_enc" not in listing[0]
    row = await endpoint(seeded, created["id"])
    assert created["secret"].encode() not in row.secret_enc
    assert decrypt_field(row.secret_enc, seeded.tenant_id) == created["secret"]


async def test_registration_refuses_unsafe_urls_bad_events_and_too_many(seeded: Seed, net: Receiver) -> None:
    post = lambda body: seeded.client.post("/v1/admin/webhooks", json=body, headers=seeded.admin)  # noqa: E731
    for url in ("http://hooks.example.com/in", "https://127.0.0.1/in", "https://169.254.169.254/", "https://user:pw@hooks.example.com/"):
        r = await post({"url": url, "events": ["*"]})
        assert r.status_code == 422 and r.json()["error"] == "unsafe_webhook_url", url
    for events in ([], ["booking.exploded"], ["booking.confirmed", "nope"]):
        assert (await post({"url": URL, "events": events})).status_code == 422, events
    assert (await post({"url": URL})).status_code == 422
    assert (await post({"url": "x" * 501, "events": ["*"]})).status_code == 422
    assert await endpoints_count(seeded) == 0
    for n in range(hooks.MAX_ENDPOINTS):
        await add_hook(seeded, f"https://hooks.example.com/{n}")
    assert (await post({"url": URL, "events": ["*"]})).status_code == 409


async def endpoints_count(s: Seed) -> int:
    async with tenant_session(s.tenant_id) as db:
        return int(await db.scalar(select(func.count()).select_from(WebhookEndpoint)) or 0)


async def test_all_events_means_all_and_the_list_of_events_is_available(seeded: Seed, net: Receiver) -> None:
    created = await add_hook(seeded, events=["booking.confirmed", "*", "booking.cancelled"])
    assert created["events"] == ["*"]
    listed = (await seeded.client.get("/v1/admin/webhook-events", headers=seeded.admin)).json()
    assert listed == list(WEBHOOK_EVENTS) and "booking.reminder" in listed and "review.created" in listed


async def test_a_webhook_can_be_changed_switched_off_and_on_and_removed(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded)
    path = f"/v1/admin/webhooks/{hook['id']}"
    patch = lambda body: seeded.client.patch(path, json=body, headers=seeded.admin)  # noqa: E731
    changed = await patch({"url": "https://other.example.com/in", "events": ["booking.created"], "description": "new"})
    assert changed.status_code == 200 and changed.json()["url"] == "https://other.example.com/in" and changed.json()["events"] == ["booking.created"]
    assert (await patch({"url": "http://insecure.example.com/"})).status_code == 422  # the same rules as at registration
    assert (await patch({"url": "https://10.0.0.1/"})).status_code == 422
    for body in ({"url": None}, {"events": None}, {"active": None}, {"events": []}, {"events": ["x"]}, {"secret": "mine"}, {"consecutive_failures": 0}):
        assert (await patch(body)).status_code == 422, body
    assert (await patch({"active": False})).json()["active"] is False
    again = await patch({"active": True})
    assert again.json()["active"] is True and again.json()["disabled_reason"] is None
    assert (await seeded.client.delete(path, headers=seeded.admin)).status_code == 204
    assert (await seeded.client.delete(path, headers=seeded.admin)).status_code == 404
    assert (await patch({"active": True})).status_code == 404


async def test_webhooks_are_private_to_their_business_and_to_owners(client: httpx.AsyncClient, net: Receiver) -> None:
    a, b = await make_seed(client), await make_seed(client)
    hook = await add_hook(a)
    assert (await client.get("/v1/admin/webhooks", headers=b.admin)).json() == []
    for call in (client.patch(f"/v1/admin/webhooks/{hook['id']}", json={"active": False}, headers=b.admin),
                 client.delete(f"/v1/admin/webhooks/{hook['id']}", headers=b.admin),
                 client.post(f"/v1/admin/webhooks/{hook['id']}/rotate-secret", headers=b.admin),
                 client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=b.admin),
                 client.get(f"/v1/admin/webhooks/{hook['id']}/deliveries", headers=b.admin)):
        assert (await call).status_code == 404
    manager, staff = await a.token("manager"), await a.token("staff")
    assert (await client.get("/v1/admin/webhooks", headers=manager)).status_code == 200  # a manager may look...
    assert (await client.get(f"/v1/admin/webhooks/{hook['id']}/deliveries", headers=manager)).status_code == 200
    assert (await client.get("/v1/admin/webhook-events", headers=manager)).status_code == 200
    body = {"url": URL, "events": ["*"]}
    for who in (manager, staff):  # ...but only an owner changes them
        assert (await client.post("/v1/admin/webhooks", json=body, headers=who)).status_code == 403
        assert (await client.patch(f"/v1/admin/webhooks/{hook['id']}", json={"active": False}, headers=who)).status_code == 403
        assert (await client.delete(f"/v1/admin/webhooks/{hook['id']}", headers=who)).status_code == 403
        assert (await client.post(f"/v1/admin/webhooks/{hook['id']}/rotate-secret", headers=who)).status_code == 403
        assert (await client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=who)).status_code == 403
    assert (await client.get("/v1/admin/webhooks", headers=staff)).status_code == 403
    assert (await client.get("/v1/admin/webhooks", headers=a.pk)).status_code == 401


# --------------------------------------------------------------------- the request that goes out
async def test_the_ping_sends_a_signed_request_to_the_validated_address(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded, "https://hooks.example.com:8443/booking?src=x")
    r = await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)
    assert r.status_code == 200 and r.json() == {"delivered": True, "status_code": 200, "error": None}
    (request,) = net.requests
    assert request.method == "POST"
    assert (request.url.host, request.url.port, request.url.path, request.url.query) == (PUBLIC, 8443, "/booking", b"src=x")  # the address, not the name
    assert request.headers["host"] == "hooks.example.com:8443" and request.extensions["sni_hostname"] == "hooks.example.com"
    body = json.loads(request.content)
    assert body["type"] == "webhook.test" and body["tenant_id"] == str(seeded.tenant_id) and uuid.UUID(body["id"])
    assert request.headers["content-type"] == "application/json" and request.headers["user-agent"] == "JustSimple-Webhooks/1"
    assert request.headers["x-webhook-id"] == body["id"] and request.headers["x-webhook-event"] == "webhook.test"
    ts, signature = request.headers["x-webhook-timestamp"], request.headers["x-webhook-signature"]
    assert abs(time.time() - int(ts)) < 30 and signature.startswith("v1=")
    verify_signature(hook["secret"], ts, request.content, signature[3:])  # the receiver's check, with the scheme the WordPress sync already uses
    from app.core.exceptions import Unauthorized

    with pytest.raises(Unauthorized):
        verify_signature("whsec_wrong", ts, request.content, signature[3:])
    with pytest.raises(Unauthorized):
        verify_signature(hook["secret"], ts, request.content + b" ", signature[3:])  # a changed body does not verify
    assert await deliveries(seeded) == [] and (await endpoint(seeded, hook["id"])).consecutive_failures == 0  # a ping is not a delivery


async def test_the_ping_reports_what_went_wrong_without_counting_it(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded)
    net.respond = lambda request: httpx.Response(503)
    r = (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()
    assert r == {"delivered": False, "status_code": 503, "error": "HTTP 503"}
    net.respond = lambda request: httpx.ReadTimeout("slow")
    r = (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()
    assert r["delivered"] is False and r["status_code"] is None and r["error"] == "ReadTimeout"
    assert (await endpoint(seeded, hook["id"])).consecutive_failures == 0


async def test_a_secret_can_be_rotated_and_the_old_one_stops_working(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded)
    new = (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/rotate-secret", headers=seeded.admin)).json()["secret"]
    assert new.startswith("whsec_") and new != hook["secret"]
    await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)
    (request,) = net.requests
    sig = request.headers["x-webhook-signature"][3:]
    verify_signature(new, request.headers["x-webhook-timestamp"], request.content, sig)
    from app.core.exceptions import Unauthorized

    with pytest.raises(Unauthorized):
        verify_signature(hook["secret"], request.headers["x-webhook-timestamp"], request.content, sig)


async def test_the_address_is_checked_again_when_sending(seeded: Seed, net: Receiver) -> None:
    """DNS rebinding: the name was fine when the webhook was registered and now points inside the network."""
    hook = await add_hook(seeded)

    async def evil(host: str) -> list[str]:
        return ["169.254.169.254"]

    hooks.set_resolver(evil)
    r = (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()
    assert r["delivered"] is False and r["status_code"] is None and r["error"].startswith("blocked:")
    assert net.requests == []  # nothing was sent


async def test_a_redirect_is_not_followed(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded)
    net.respond = lambda request: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
    r = (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()
    assert r == {"delivered": False, "status_code": 302, "error": "HTTP 302"}
    assert len(net.requests) == 1  # it never went to the redirect target


async def test_the_next_address_is_tried_when_the_first_cannot_be_reached(seeded: Seed, net: Receiver) -> None:
    async def dns(host: str) -> list[str]:
        return ["93.184.216.34", "93.184.216.35", "2606:4700:4700::1111"]

    hooks.set_resolver(dns)
    hook = await add_hook(seeded)
    net.respond = lambda request: httpx.ConnectError("refused") if request.url.host == "93.184.216.34" else httpx.Response(200)
    r = (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()
    assert r["delivered"] is True
    assert [q.url.host for q in net.requests] == ["93.184.216.34", "93.184.216.35"]
    net.requests.clear()
    net.respond = lambda request: httpx.ConnectError("refused")
    r = (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()
    assert r["delivered"] is False and r["error"].startswith("could not connect") and len(net.requests) == 3
    net.requests.clear()
    await add_hook(seeded, "https://[2606:4700:4700::1111]/in")  # a literal IPv6 address is brackets in the URL and a bare address in the checks
    hooks.set_resolver(None)


async def test_the_receivers_answer_body_is_never_read(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded)

    class Endless(httpx.AsyncByteStream):
        read = False

        async def __aiter__(self) -> AsyncIterator[bytes]:
            Endless.read = True
            yield b"x" * 10

    net.respond = lambda request: httpx.Response(200, stream=Endless())
    assert (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()["delivered"] is True
    assert Endless.read is False


# --------------------------------------------------------------------- the real TLS handshake
def _certificate(tmp_path: Path, name: str) -> tuple[Path, Path, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False).sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_file, key_file = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_file.write_bytes(pem)
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return cert_file, key_file, pem


async def _tls_server(cert: Path, key: Path, seen: list[dict[str, Any]]) -> tuple[asyncio.Server, int]:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            headers = {line.split(":", 1)[0].lower(): line.split(":", 1)[1].strip() for line in head.split("\r\n")[1:] if ":" in line}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            seen.append({"request_line": head.split("\r\n")[0], "headers": headers, "body": body})
            writer.write(b"HTTP/1.1 204 No Content\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (asyncio.IncompleteReadError, ssl.SSLError, ConnectionError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    return server, int(server.sockets[0].getsockname()[1])


async def test_a_real_tls_connection_is_made_to_the_address_and_checked_against_the_name(
    seeded: Seed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(get_settings(), "webhook_allow_insecure", True)  # the receiver is on this machine
    good_cert, good_key, good_pem = _certificate(tmp_path, "hooks.example.com")
    bad_cert, bad_key, _ = _certificate(tmp_path, "someone-else.example.com")
    seen_good: list[dict[str, Any]] = []
    seen_bad: list[dict[str, Any]] = []
    good, good_port = await _tls_server(good_cert, good_key, seen_good)
    bad, bad_port = await _tls_server(bad_cert, bad_key, seen_bad)

    async def dns(host: str) -> list[str]:
        return ["127.0.0.1"]

    hooks.set_resolver(dns)
    hooks.set_transport(None, ssl.create_default_context(cadata=good_pem.decode()))  # trusts only the first certificate
    try:
        body = b'{"hello":"world"}'
        ok = await hooks.send(f"https://hooks.example.com:{good_port}/in", body, {"X-Test": "1"})
        assert ok.status == 204 and ok.ok is True
        (request,) = seen_good
        assert request["request_line"] == "POST /in HTTP/1.1" and request["body"] == body
        assert request["headers"]["host"] == f"hooks.example.com:{good_port}" and request["headers"]["x-test"] == "1"
        # the certificate belongs to another name: the connection is refused, whatever address it was made to
        wrong = await hooks.send(f"https://hooks.example.com:{bad_port}/in", body, {})
        assert wrong.status is None and wrong.error is not None and seen_bad == []
    finally:
        hooks.set_resolver(None)
        hooks.set_transport(None)
        good.close()
        bad.close()
        await good.wait_closed()
        await bad.wait_closed()


# --------------------------------------------------------------------- fan-out: who is owed what
async def test_an_event_is_owed_to_the_subscribed_active_endpoints_of_its_own_business(client: httpx.AsyncClient, net: Receiver, worker_publishers: Recorder) -> None:
    a, b = await make_seed(client), await make_seed(client)
    everything = await add_hook(a, "https://one.example.com/in", ["*"])
    confirmed_only = await add_hook(a, "https://two.example.com/in", ["booking.confirmed"])
    cancelled_only = await add_hook(a, "https://three.example.com/in", ["booking.cancelled"])
    off = await add_hook(a, "https://four.example.com/in", ["*"])
    assert (await client.patch(f"/v1/admin/webhooks/{off['id']}", json={"active": False}, headers=a.admin)).status_code == 200
    other_business = await add_hook(b, "https://five.example.com/in", ["*"])
    await booked_with_email(a, hour=10)
    await tick(a)
    mine = {h["id"]: sorted(d.event_type for d in await deliveries(a, h["id"])) for h in (everything, confirmed_only, cancelled_only, off)}
    assert mine[everything["id"]] == ["booking.confirmed", "booking.created", "shift.updated"]
    assert mine[confirmed_only["id"]] == ["booking.confirmed"] and mine[cancelled_only["id"]] == [] and mine[off["id"]] == []
    assert await deliveries(b, other_business["id"]) == []  # business B had no events, and A's never reach it


async def test_publishing_twice_owes_the_endpoint_once(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded)
    event = OutboxEvent(
        id=uuid.uuid4(), tenant_id=seeded.tenant_id, event_type="booking.confirmed", aggregate_type="booking", aggregate_id=uuid.uuid4(),
        payload={"booking_id": "x"}, seq=7, created_at=dt.datetime.now(dt.UTC),
    )
    for _ in range(3):
        await WebhookPublisher().publish(event)
    (delivery,) = await deliveries(seeded, hook["id"])
    assert delivery.payload == {"id": str(event.id), "type": "booking.confirmed", "seq": 7, "created_at": event.created_at.isoformat(),
                                "tenant_id": str(seeded.tenant_id), "data": {"booking_id": "x"}}
    await WebhookPublisher().publish(OutboxEvent(id=uuid.uuid4(), tenant_id=seeded.tenant_id, event_type="not.an.event", aggregate_type="x",
                                                 aggregate_id=uuid.uuid4(), payload={}, seq=8, created_at=dt.datetime.now(dt.UTC)))
    assert len(await deliveries(seeded, hook["id"])) == 1


async def test_the_composite_publisher_tries_everyone_and_reports_failures() -> None:
    seen: list[str] = []

    class Ok:
        async def publish(self, event: OutboxEvent) -> None:
            seen.append("ok")

    class Boom:
        def __init__(self, message: str) -> None:
            self.message = message

        async def publish(self, event: OutboxEvent) -> None:
            seen.append(self.message)
            raise RuntimeError(self.message)

    event: Any = object()
    await outbox.CompositePublisher(Ok(), Ok()).publish(event)
    assert seen == ["ok", "ok"]
    seen.clear()
    with pytest.raises(RuntimeError, match="^one$"):
        await outbox.CompositePublisher(Boom("one"), Ok()).publish(event)
    assert seen == ["one", "ok"]  # the one after the failure still ran
    seen.clear()
    with pytest.raises(RuntimeError, match="one.*two"):
        await outbox.CompositePublisher(Boom("one"), Ok(), Boom("two")).publish(event)
    assert seen == ["one", "ok", "two"]


# --------------------------------------------------------------------- delivery
async def test_a_booking_reaches_the_subscribed_url_signed_and_exactly_once(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    hook = await add_hook(seeded, events=["booking.confirmed"])
    bid = await booked_with_email(seeded, hour=10)
    report = await tick(seeded)
    assert (report.webhooks_delivered, report.webhooks_failed) == (1, 0)
    (request,) = net.requests
    body = net.bodies()[0]
    assert body["type"] == "booking.confirmed" and body["data"]["booking_id"] == bid and body["data"]["status"] == "confirmed"
    assert body["tenant_id"] == str(seeded.tenant_id) and isinstance(body["seq"], int)
    assert set(body["data"]) == {"booking_id", "status", "previous_status", "start_at", "end_at"}  # ids, status and times: no names, phones or emails
    assert request.headers["x-webhook-id"] == body["id"] and request.headers["x-webhook-event"] == "booking.confirmed"
    verify_signature(hook["secret"], request.headers["x-webhook-timestamp"], request.content, request.headers["x-webhook-signature"][3:])
    (delivery,) = await deliveries(seeded)
    assert (delivery.status, delivery.attempts, delivery.response_status, delivery.error) == ("delivered", 1, 200, None)
    assert request.headers["x-webhook-delivery"] == str(delivery.id)
    assert (await endpoint(seeded, hook["id"])).last_success_at is not None
    await tick(seeded)
    await tick(seeded, dt.datetime.now(dt.UTC) + 2 * H)
    assert len(net.requests) == 1  # delivered means done
    for text_ in (EMAIL, "Aisha", PHONE):
        assert text_ not in request.content.decode()


async def test_every_subscribed_endpoint_gets_its_own_signature(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    first = await add_hook(seeded, "https://one.example.com/in", ["booking.confirmed"])
    second = await add_hook(seeded, "https://two.example.com/in", ["booking.confirmed"])
    await booked_with_email(seeded, hour=10)
    await tick(seeded)
    assert len(net.requests) == 2
    secrets_by_host = {"one.example.com": first["secret"], "two.example.com": second["secret"]}
    for request in net.requests:
        secret = secrets_by_host[request.headers["host"]]
        verify_signature(secret, request.headers["x-webhook-timestamp"], request.content, request.headers["x-webhook-signature"][3:])
    assert net.requests[0].headers["x-webhook-id"] == net.requests[1].headers["x-webhook-id"]  # the same event, whoever receives it


async def test_a_failing_endpoint_is_retried_with_backoff_and_a_success_resets_the_count(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    hook = await add_hook(seeded, events=["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    await booked_with_email(seeded, hour=10)
    start = dt.datetime.now(dt.UTC)
    first = await tick(seeded, start)
    assert (first.webhooks_delivered, first.webhooks_failed) == (0, 1)
    (delivery,) = await deliveries(seeded)
    assert (delivery.status, delivery.attempts, delivery.response_status, delivery.error) == ("pending", 1, 500, "HTTP 500")
    assert delivery.next_attempt_at == start + outbox.backoff(1)  # 30 s
    assert (await endpoint(seeded, hook["id"])).consecutive_failures == 1
    await tick(seeded, start + dt.timedelta(seconds=10))
    assert len(net.requests) == 1  # not due yet
    await tick(seeded, start + dt.timedelta(seconds=31))
    (delivery,) = await deliveries(seeded)
    assert delivery.attempts == 2 and delivery.next_attempt_at == start + dt.timedelta(seconds=31) + outbox.backoff(2)  # 1 min
    net.respond = lambda request: httpx.Response(204)
    await tick(seeded, start + dt.timedelta(minutes=5))
    (delivery,) = await deliveries(seeded)
    assert (delivery.status, delivery.attempts, delivery.response_status) == ("delivered", 3, 204)
    ep = await endpoint(seeded, hook["id"])
    assert ep.consecutive_failures == 0 and ep.last_failure_at is not None and ep.active is True
    bodies = net.bodies()
    assert bodies[0] == bodies[1] == bodies[2]  # the same event every time (receivers deduplicate on its id)
    stamps = [r.headers["x-webhook-timestamp"] for r in net.requests]
    assert all(abs(time.time() - int(t)) < 60 for t in stamps)


@pytest.mark.parametrize("answer", [httpx.Response(404), httpx.Response(301), httpx.Response(429), httpx.ReadTimeout("slow"), httpx.ConnectError("down"), httpx.RemoteProtocolError("bad")])
async def test_anything_but_a_2xx_counts_as_a_failure(seeded: Seed, net: Receiver, worker_publishers: Recorder, answer: Any) -> None:
    await add_hook(seeded, events=["booking.confirmed"])
    net.respond = lambda request: answer
    await booked_with_email(seeded, hour=10)
    assert (await tick(seeded)).webhooks_failed == 1
    (delivery,) = await deliveries(seeded)
    assert delivery.status == "pending" and delivery.error and delivery.attempts == 1


async def test_a_delivery_that_keeps_failing_is_given_up_on_and_can_be_redelivered(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_max_attempts", 3)
    hook = await add_hook(seeded, events=["booking.confirmed"])
    net.respond = lambda request: httpx.Response(502)
    await booked_with_email(seeded, hour=10)
    clock = dt.datetime.now(dt.UTC)
    for n in range(5):
        await tick(seeded, clock + n * H)
    (delivery,) = await deliveries(seeded)
    assert (delivery.status, delivery.attempts, delivery.error) == ("failed", 3, "HTTP 502") and len(net.requests) == 3
    listing = (await seeded.client.get(f"/v1/admin/webhooks/{hook['id']}/deliveries", params={"status": "failed"}, headers=seeded.admin)).json()
    assert [d["id"] for d in listing] == [str(delivery.id)]
    assert (await seeded.client.get(f"/v1/admin/webhooks/{hook['id']}/deliveries", params={"status": "pending"}, headers=seeded.admin)).json() == []
    net.respond = lambda request: httpx.Response(200)
    path = f"/v1/admin/webhooks/{hook['id']}/deliveries/{delivery.id}/redeliver"
    r = await seeded.client.post(path, headers=seeded.admin)
    assert r.status_code == 200 and r.json()["status"] == "pending" and r.json()["attempts"] == 0 and r.json()["error"] is None
    assert (await seeded.client.post(path, headers=await seeded.token("manager"))).status_code == 403
    await tick(seeded, clock + 9 * H)
    (delivery,) = await deliveries(seeded)
    assert (delivery.status, delivery.attempts) == ("delivered", 1)
    assert (await seeded.client.post(path, headers=seeded.admin)).status_code == 409  # only a failed one can be redelivered
    assert (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/deliveries/{uuid.uuid4()}/redeliver", headers=seeded.admin)).status_code == 404


async def test_a_delivery_of_another_webhook_cannot_be_redelivered_through_this_one(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_max_attempts", 1)
    one, two = await add_hook(seeded, "https://one.example.com/in", ["booking.confirmed"]), await add_hook(seeded, "https://two.example.com/in", ["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    await booked_with_email(seeded, hour=10)
    await tick(seeded)
    mine = (await deliveries(seeded, one["id"]))[0]
    r = await seeded.client.post(f"/v1/admin/webhooks/{two['id']}/deliveries/{mine.id}/redeliver", headers=seeded.admin)
    assert r.status_code == 404
    assert two


async def test_an_endpoint_that_fails_too_often_in_a_row_is_switched_off(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_disable_after_failures", 3)
    hook = await add_hook(seeded, events=["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    mei = await seeded.add_staff("Mei")
    for hour in (10, 11, 12):
        r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, hour), mei, customer={"name": "A", "phone": f"+6012000{hour:04d}"}))
        assert r.status_code == 201
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)  # three deliveries, three failures: the limit is reached
    ep = await endpoint(seeded, hook["id"])
    assert ep.active is False and ep.consecutive_failures == 3 and ep.disabled_reason == "too many consecutive failures"
    states = sorted((d.status, d.error) for d in await deliveries(seeded))
    assert states == [("failed", "endpoint disabled")] * 3
    sent = len(net.requests)
    await booked_with_email(seeded, "+60122220009", "z@example.com", hour=15)
    await tick(seeded, clock + 2 * H)
    assert len(net.requests) == sent and len(await deliveries(seeded)) == 3  # nothing new is owed to a switched-off endpoint
    # switching it on again resets it
    net.respond = lambda request: httpx.Response(200)
    r = await seeded.client.patch(f"/v1/admin/webhooks/{hook['id']}", json={"active": True}, headers=seeded.admin)
    assert r.json()["consecutive_failures"] == 0 and r.json()["disabled_reason"] is None


async def test_switching_an_endpoint_off_fails_what_it_was_owed_and_stops_sending(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    hook = await add_hook(seeded, events=["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    await booked_with_email(seeded, hour=10)
    await tick(seeded)
    assert (await seeded.client.patch(f"/v1/admin/webhooks/{hook['id']}", json={"active": False}, headers=seeded.admin)).json()["disabled_reason"] == "switched off by an administrator"
    (delivery,) = await deliveries(seeded)
    assert (delivery.status, delivery.error) == ("failed", "endpoint disabled")
    sent = len(net.requests)
    await tick(seeded, dt.datetime.now(dt.UTC) + 3 * H)
    assert len(net.requests) == sent
    net.respond = lambda request: httpx.Response(200)
    assert (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/deliveries/{delivery.id}/redeliver", headers=seeded.admin)).status_code == 409  # switch it on first


async def test_a_url_that_became_unsafe_while_off_cannot_be_switched_back_on(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded)
    await seeded.client.patch(f"/v1/admin/webhooks/{hook['id']}", json={"active": False}, headers=seeded.admin)

    async def rebound(host: str) -> list[str]:
        return ["10.0.0.9"]

    hooks.set_resolver(rebound)
    r = await seeded.client.patch(f"/v1/admin/webhooks/{hook['id']}", json={"active": True}, headers=seeded.admin)
    assert r.status_code == 422 and r.json()["error"] == "unsafe_webhook_url"
    assert (await endpoint(seeded, hook["id"])).active is False


async def test_a_dead_endpoint_does_not_hold_back_the_customers_email(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    await add_hook(seeded)
    net.respond = lambda request: httpx.Response(500)
    await booked_with_email(seeded, hour=10)
    report = await tick(seeded)
    assert report.events_failed == 0 and report.webhooks_failed >= 1
    assert [m.subject for m in worker_publishers.sent] == ["Your booking is confirmed"]  # sent in the very first pass


async def test_a_failing_email_does_not_stop_the_webhook(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    hook = await add_hook(seeded, events=["booking.confirmed"])
    worker_publishers.failing["email"] = RuntimeError("smtp down")
    await booked_with_email(seeded, hour=10)
    report = await tick(seeded)
    assert report.events_failed == 1 and report.webhooks_delivered == 1  # the event is retried for the email; the webhook already went out
    assert len(net.requests) == 1
    worker_publishers.failing.clear()
    await tick(seeded, dt.datetime.now(dt.UTC) + H)
    assert len(net.requests) == 1  # and the retry does not send it again
    assert len(await deliveries(seeded, hook["id"])) == 1


async def test_deliveries_for_one_endpoint_do_not_wait_for_each_other(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    """A sweep sends a batch concurrently: a slow receiver costs one timeout, not one per delivery."""
    monkeypatch.setattr(get_settings(), "webhook_concurrency", 10)
    await add_hook(seeded, events=["booking.confirmed"])
    running = peak = 0
    real_send = hooks.send

    async def slow(url: str, body: bytes, headers: dict[str, str]) -> hooks.SendResult:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return await real_send(url, body, headers)

    monkeypatch.setattr(hooks, "send", slow)
    mei = await seeded.add_staff("Mei")
    for hour in range(9, 13):
        await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, hour), mei, customer={"name": "A", "phone": f"+6012000{hour:04d}"}))
    report = await tick(seeded)
    assert report.webhooks_delivered == 4 and peak == 4


async def test_the_number_of_requests_in_flight_is_limited(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_concurrency", 2)
    await add_hook(seeded, events=["booking.confirmed"])
    state = {"running": 0, "peak": 0}
    real_send = hooks.send

    async def slow(url: str, body: bytes, headers: dict[str, str]) -> hooks.SendResult:
        state["running"] += 1
        state["peak"] = max(state["peak"], state["running"])
        await asyncio.sleep(0.05)
        state["running"] -= 1
        return await real_send(url, body, headers)

    monkeypatch.setattr(hooks, "send", slow)
    mei = await seeded.add_staff("Mei")
    for hour in (9, 10, 11, 12, 14, 15):
        await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, hour), mei, customer={"name": "A", "phone": f"+6012000{hour:04d}"}))
    assert (await tick(seeded)).webhooks_delivered == 6 and state["peak"] == 2


async def test_a_batch_is_capped_per_pass_and_the_rest_waits(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_batch", 2)
    await add_hook(seeded, events=["booking.confirmed"])
    mei = await seeded.add_staff("Mei")
    for hour in range(9, 14 + 1):
        if hour == 13:
            continue
        await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, hour), mei, customer={"name": "A", "phone": f"+6012000{hour:04d}"}))
    counts = [(await tick(seeded)).webhooks_delivered for _ in range(4)]
    assert counts == [2, 2, 1, 0]


async def test_two_sweeps_at_once_send_each_delivery_once(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    await add_hook(seeded, events=["booking.confirmed"])
    mei = await seeded.add_staff("Mei")
    for hour in (9, 10, 11, 12):
        await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, hour), mei, customer={"name": "A", "phone": f"+6012000{hour:04d}"}))
    from app.services.sweep import sweep_tenant

    await asyncio.gather(sweep_tenant(seeded.tenant_id), sweep_tenant(seeded.tenant_id))
    await tick(seeded)
    ids = [r.headers["x-webhook-id"] for r in net.requests]
    assert len(ids) == 4 and len(set(ids)) == 4  # no event went out twice


async def test_the_receiver_gets_what_it_is_owed_after_a_cancellation_and_a_move(seeded: Seed, net: Receiver, worker_publishers: Recorder, otp: Any) -> None:
    await add_hook(seeded, events=["booking.rescheduled", "booking.cancelled"])
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": PHONE, "email": EMAIL}))
    bid = r.json()["id"]
    me = await login(seeded)
    from tests.test_customer_reschedule import move

    assert (await move(seeded, me, bid, at(seeded.day, 15))).status_code == 200
    assert (await cancel(seeded, me, bid)).status_code == 200
    await tick(seeded)
    got = sorted(net.bodies(), key=lambda b: b["seq"])
    assert [b["type"] for b in got] == ["booking.rescheduled", "booking.cancelled"]
    assert got[0]["data"]["previous_start_at"] and got[0]["seq"] < got[1]["seq"]  # seq lets a receiver put them in order


async def test_finished_deliveries_are_purged_after_the_retention_period(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    await add_hook(seeded, events=["booking.confirmed"])
    await booked_with_email(seeded, hour=10)
    await tick(seeded)
    assert len(await deliveries(seeded)) == 1
    monkeypatch.setattr(get_settings(), "webhook_log_retention_days", 30)
    await tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(days=29))
    assert len(await deliveries(seeded)) == 1
    await tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(days=31))
    assert await deliveries(seeded) == []


async def test_a_pending_delivery_is_never_purged(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    await add_hook(seeded, events=["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    await booked_with_email(seeded, hour=10)
    await tick(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE webhook_deliveries SET created_at = now() - interval '90 days', next_attempt_at = now() + interval '1 day'"))
    await tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    assert len(await deliveries(seeded)) == 1


async def test_deleting_a_webhook_deletes_its_deliveries(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    hook = await add_hook(seeded, events=["booking.confirmed"])
    await booked_with_email(seeded, hour=10)
    await tick(seeded)
    assert len(await deliveries(seeded)) == 1
    assert (await seeded.client.delete(f"/v1/admin/webhooks/{hook['id']}", headers=seeded.admin)).status_code == 204
    assert await deliveries(seeded) == []


async def test_the_worker_installs_the_webhook_publisher_next_to_the_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_publisher", None)
    seen: list[Any] = []

    async def fake_run(*a: Any, **k: Any) -> None:
        seen.append(outbox.get_event_publisher())

    async def nothing() -> None:
        return None

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(worker, "dispose_engine", nothing)
    await worker.amain(["--once"])
    (publisher,) = seen
    assert isinstance(publisher, outbox.CompositePublisher)
    assert sorted(type(p).__name__ for p in publisher.publishers) == ["CalendarPublisher", "NotificationPublisher", "WebhookPublisher"]


def test_the_worker_summary_counts_webhooks() -> None:
    from app.services.sweep import SweepReport, TenantSweep

    line = worker.summarise(SweepReport(True, [TenantSweep(uuid.uuid4(), webhooks_delivered=3), TenantSweep(uuid.uuid4(), webhooks_delivered=4)]))
    assert "7 webhooks delivered" in line


async def test_validation_errors_are_domain_errors_not_crashes() -> None:
    with pytest.raises(ValidationFailed):
        await check_url("https://")
    with pytest.raises(ValidationFailed):
        await check_url("")
