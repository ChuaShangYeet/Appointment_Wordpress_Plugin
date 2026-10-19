"""When the system switches a webhook off, the business is told (an email to its contact address)."""
from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.events import OutboxEvent
from app.models.webhooks import WEBHOOK_EVENTS
from app.services import outbox
from app.services import webhooks as hooks
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_customer_identity import booked_with_email
from tests.test_notifications import log_rows, run_outbox
from tests.test_webhooks import H, Receiver, add_hook, endpoint, net, tick, worker_publishers  # noqa: F401  (fixtures)

OWNER_EMAIL = "owner@lotus.example.com"
SECRET_URL = "https://hooks.example.com/booking-hook?token=SECRET123"


async def set_contact(s: Seed, email: str | None = OWNER_EMAIL, name: str = "Lotus Spa") -> None:
    body: dict[str, Any] = {"business_name": name}
    if email:
        body["contact_email"] = email
    assert (await s.client.put("/v1/admin/business-profile", json=body, headers=s.admin)).status_code == 200


async def break_the_receiver(s: Seed, net: Receiver, monkeypatch: pytest.MonkeyPatch, *, after: int = 2, url: str = SECRET_URL) -> dict[str, Any]:
    """An endpoint that always answers 500, with several bookings owed so that it fails ``after`` times in a row in one pass."""
    monkeypatch.setattr(get_settings(), "webhook_disable_after_failures", after)
    hook = await add_hook(s, url, ["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    mei = await s.add_staff("Mei")
    for hour in range(9, 9 + after):
        r = await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, hour), mei, customer={"name": "A", "phone": f"+6012000{hour:04d}"}))
        assert r.status_code == 201
    return hook


def alerts(rec: Recorder) -> list[Any]:
    return [m for m in rec.sent if (m.subject or "").startswith("A webhook for")]


async def alert_events(s: Seed) -> list[OutboxEvent]:
    async with tenant_session(s.tenant_id) as db:
        return list((await db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == hooks.ENDPOINT_DISABLED_EVENT).order_by(OutboxEvent.seq))).all())


# --------------------------------------------------------------------- the alert
async def test_the_business_is_emailed_when_a_webhook_is_switched_off_for_failing(
    seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_contact(seeded)
    hook = await break_the_receiver(seeded, net, monkeypatch)
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)  # the failures happen and the endpoint is switched off...
    assert (await endpoint(seeded, hook["id"])).active is False and alerts(worker_publishers) == []
    await tick(seeded, clock + dt.timedelta(minutes=1))  # ...and the next pass sends the alert
    (message,) = alerts(worker_publishers)
    assert (message.channel, message.recipient, message.subject) == ("email", OWNER_EMAIL, "A webhook for Lotus Spa was switched off")
    assert "hooks.example.com" in message.body and "failed 2 times in a row" in message.body and "HTTP 500" in message.body
    assert f"PATCH /v1/admin/webhooks/{hook['id']}" in message.body and '"active": true' in message.body and "redelivered" in message.body
    assert "SECRET123" not in message.body and "booking-hook" not in message.body  # the path and query can hold secrets
    rows = [r for r in await log_rows(seeded) if r.event == hooks.ENDPOINT_DISABLED_EVENT]
    assert [(r.channel, r.status, r.recipient, r.booking_id, r.customer_id) for r in rows] == [("email", "sent", "o***@lotus.example.com", None, None)]
    await tick(seeded, clock + dt.timedelta(minutes=5))
    assert len(alerts(worker_publishers)) == 1  # one alert per switch-off


async def test_the_event_carries_ids_and_counts_never_the_url(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    hook = await break_the_receiver(seeded, net, monkeypatch)
    await tick(seeded)
    (event,) = await alert_events(seeded)
    assert event.aggregate_type == "webhook" and str(event.aggregate_id) == hook["id"]
    assert event.payload == {"endpoint_id": hook["id"], "reason": "too many consecutive failures", "consecutive_failures": 2, "last_error": "HTTP 500"}
    assert "SECRET123" not in json.dumps(event.payload)


async def test_switching_it_off_by_hand_sends_no_alert(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    await set_contact(seeded)
    hook = await add_hook(seeded)
    assert (await seeded.client.patch(f"/v1/admin/webhooks/{hook['id']}", json={"active": False}, headers=seeded.admin)).status_code == 200
    await tick(seeded)
    await tick(seeded, dt.datetime.now(dt.UTC) + H)
    assert await alert_events(seeded) == [] and alerts(worker_publishers) == []


async def test_a_business_without_a_contact_email_is_noted_in_the_log_not_retried(
    seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    await break_the_receiver(seeded, net, monkeypatch)
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)
    report = await tick(seeded, clock + dt.timedelta(minutes=1))
    assert alerts(worker_publishers) == [] and report.events_failed == 0
    rows = [r for r in await log_rows(seeded) if r.event == hooks.ENDPOINT_DISABLED_EVENT]
    assert [(r.status, r.error, r.recipient) for r in rows] == [("skipped", "no business contact email on file", None)]


async def test_a_failing_email_is_retried_and_eventually_given_up_on(
    seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_contact(seeded)
    monkeypatch.setattr(get_settings(), "notify_max_attempts", 3)
    await break_the_receiver(seeded, net, monkeypatch)
    worker_publishers.failing["email"] = RuntimeError("smtp down")
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)
    first = await tick(seeded, clock + dt.timedelta(minutes=1))
    assert first.events_failed >= 1 and alerts(worker_publishers) == []
    rows = [r for r in await log_rows(seeded) if r.event == hooks.ENDPOINT_DISABLED_EVENT]
    assert [(r.status, r.attempts, r.error) for r in rows] == [("failed", 1, "RuntimeError: smtp down")]
    worker_publishers.failing.clear()
    await tick(seeded, clock + dt.timedelta(minutes=5))  # the retry succeeds
    assert len(alerts(worker_publishers)) == 1
    rows = [r for r in await log_rows(seeded) if r.event == hooks.ENDPOINT_DISABLED_EVENT]
    assert [(r.status, r.attempts, r.error) for r in rows] == [("sent", 2, None)]


async def test_an_alert_that_never_gets_through_is_dropped_after_the_attempt_limit(
    seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_contact(seeded)
    monkeypatch.setattr(get_settings(), "notify_max_attempts", 2)
    await break_the_receiver(seeded, net, monkeypatch)
    worker_publishers.failing["email"] = RuntimeError("smtp down")
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)
    for minutes in (1, 10, 120):
        await tick(seeded, clock + dt.timedelta(minutes=minutes))
    rows = [r for r in await log_rows(seeded) if r.event == hooks.ENDPOINT_DISABLED_EVENT]
    assert [(r.status, r.attempts) for r in rows] == [("failed", 2)]
    worker_publishers.failing.clear()
    await tick(seeded, clock + dt.timedelta(hours=5))
    assert alerts(worker_publishers) == []  # given up: the log says so, and the admin shows the endpoint as switched off anyway


async def test_a_redelivered_event_does_not_email_twice(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    await set_contact(seeded)
    await break_the_receiver(seeded, net, monkeypatch)
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)
    await tick(seeded, clock + dt.timedelta(minutes=1))
    assert len(alerts(worker_publishers)) == 1
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE outbox_events SET published_at = NULL, available_at = now()"))
    await run_outbox(seeded)
    assert len(alerts(worker_publishers)) == 1


async def test_nothing_is_sent_if_the_webhook_is_back_on_or_gone_by_then(
    seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_contact(seeded)
    back = await break_the_receiver(seeded, net, monkeypatch)
    clock = dt.datetime.now(dt.UTC)
    # the alert is queued but not yet delivered when the owner fixes the receiver and switches it on
    async with tenant_session(seeded.tenant_id) as db:  # (fan out what is owed, then try to send it, without delivering the alert)
        await outbox.dispatch_pending(db, outbox.get_event_publisher(), now=clock)  # type: ignore[arg-type]
    async with tenant_session(seeded.tenant_id) as db:
        await hooks.deliver_due(db, clock)
    net.respond = lambda request: httpx.Response(200)
    assert (await seeded.client.patch(f"/v1/admin/webhooks/{back['id']}", json={"active": True}, headers=seeded.admin)).status_code == 200
    await run_outbox(seeded, now=clock + dt.timedelta(minutes=1))
    assert alerts(worker_publishers) == []
    assert [(r.status, r.error) for r in await log_rows(seeded) if r.event == hooks.ENDPOINT_DISABLED_EVENT] == [("skipped", "the webhook was switched on again")]
    # and a webhook that was deleted before the alert went out has nothing to warn about
    other = await add_hook(seeded, "https://other.example.com/in", ["booking.cancelled"])
    async with tenant_session(seeded.tenant_id) as db:
        endpoint_row = await db.get(hooks.WebhookEndpoint, uuid.UUID(other["id"]))
        assert endpoint_row is not None
        endpoint_row.consecutive_failures = 9
        await hooks.disable_endpoint(db, endpoint_row, "too many consecutive failures", alert=True)
    assert (await seeded.client.delete(f"/v1/admin/webhooks/{other['id']}", headers=seeded.admin)).status_code == 204
    before = len(await log_rows(seeded))
    result = await run_outbox(seeded, now=clock + dt.timedelta(minutes=2))
    assert result.failed == 0 and result.published >= 1  # handled quietly, not crashed and retried
    assert alerts(worker_publishers) == [] and len(await log_rows(seeded)) == before


async def test_each_switch_off_gets_its_own_alert(client: httpx.AsyncClient, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client)
    await set_contact(s)
    hook = await break_the_receiver(s, net, monkeypatch)
    clock = dt.datetime.now(dt.UTC)
    await tick(s, clock)
    await tick(s, clock + dt.timedelta(minutes=1))
    assert len(alerts(worker_publishers)) == 1
    net.respond = lambda request: httpx.Response(500)
    assert (await s.client.patch(f"/v1/admin/webhooks/{hook['id']}", json={"active": True}, headers=s.admin)).status_code == 200
    mei = await s.add_staff("Lin")
    for hour in (9, 10):
        r = await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day + dt.timedelta(days=1), hour), mei, customer={"name": "B", "phone": f"+6013000{hour:04d}"}))
        assert r.status_code == 201
    await tick(s, clock + dt.timedelta(minutes=10))
    await tick(s, clock + dt.timedelta(minutes=11))
    assert len(alerts(worker_publishers)) == 2 and len(await alert_events(s)) == 2


async def test_the_alert_goes_to_the_business_that_owns_the_webhook(
    client: httpx.AsyncClient, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await set_contact(a, "a@a.example.com", "Business A")
    await set_contact(b, "b@b.example.com", "Business B")
    await break_the_receiver(b, net, monkeypatch, url="https://b-hooks.example.com/in")
    clock = dt.datetime.now(dt.UTC)
    for s in (a, b):
        await tick(s, clock)
        await tick(s, clock + dt.timedelta(minutes=1))
    (message,) = alerts(worker_publishers)
    assert message.recipient == "b@b.example.com" and "Business B" in message.subject and "b-hooks.example.com" in message.body


async def test_the_business_name_cannot_inject_lines_into_the_subject(
    seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    await set_contact(seeded, name="Lotus\r\nBcc: x@example.com")
    await break_the_receiver(seeded, net, monkeypatch)
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)
    await tick(seeded, clock + dt.timedelta(minutes=1))
    (message,) = alerts(worker_publishers)
    assert "\n" not in message.subject and "\r" not in message.subject and message.subject == "A webhook for Lotus Bcc: x@example.com was switched off"


# --------------------------------------------------------------------- what it must not become
async def test_the_alert_is_not_something_a_webhook_or_a_template_can_subscribe_to(seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    assert hooks.ENDPOINT_DISABLED_EVENT not in WEBHOOK_EVENTS
    assert (await seeded.client.post("/v1/admin/webhooks", json={"url": "https://x.example.com/in", "events": [hooks.ENDPOINT_DISABLED_EVENT]}, headers=seeded.admin)).status_code == 422
    put = await seeded.client.put(f"/v1/admin/notification-templates/{hooks.ENDPOINT_DISABLED_EVENT}/email", json={"subject": "s", "body": "b"}, headers=seeded.admin)
    assert put.status_code == 422
    listing = (await seeded.client.get("/v1/admin/notification-templates", headers=seeded.admin)).json()
    assert hooks.ENDPOINT_DISABLED_EVENT not in {t["event"] for t in listing}
    # a catch-all subscriber to the same business never receives it either
    await set_contact(seeded)
    watcher = await add_hook(seeded, "https://watcher.example.com/in", ["*"])
    await break_the_receiver(seeded, net, monkeypatch, url="https://broken.example.com/in")
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)
    await tick(seeded, clock + dt.timedelta(minutes=1))
    types = {json.loads(r.content)["type"] for r in net.requests if r.headers["host"] == "watcher.example.com"}
    assert hooks.ENDPOINT_DISABLED_EVENT not in types and watcher


def test_only_the_host_of_a_webhook_url_is_ever_shown() -> None:
    from app.services.notifications import _webhook_host

    assert _webhook_host("https://hooks.example.com/in?token=SECRET") == "hooks.example.com"
    assert _webhook_host("https://hooks.example.com:8443/in") == "hooks.example.com:8443"
    assert _webhook_host("https://user:pw@hooks.example.com:8443/in?x=1#f") == "hooks.example.com:8443"  # (such URLs are refused at registration; old rows are still safe)
    assert _webhook_host("not a url") == "(unknown)"


async def test_the_last_error_in_the_email_is_a_single_line(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    await set_contact(seeded)
    hook = await add_hook(seeded, SECRET_URL, ["booking.confirmed"])
    async with tenant_session(seeded.tenant_id) as db:
        row = await db.get(hooks.WebhookEndpoint, uuid.UUID(hook["id"]))
        assert row is not None
        row.consecutive_failures = 3
        await hooks.disable_endpoint(db, row, "too many consecutive failures", alert=True, last_error="HTTP 500\r\nBcc: x@example.com")
    await run_outbox(seeded)
    (message,) = alerts(worker_publishers)
    assert "(most recent problem: HTTP 500 Bcc: x@example.com)" in message.body and "\r" not in message.body
