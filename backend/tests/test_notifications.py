"""Customer notifications (blueprint §15): providers, templates, booking messages from the outbox, sign-in messages."""
from __future__ import annotations

import datetime as dt
import logging
import smtplib
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from app import worker
from app.core.config import Settings, get_settings
from app.core.database import tenant_session
from app.core.exceptions import ValidationFailed
from app.models import Booking, Customer, NotificationLog, Tenant
from app.models.events import OutboxEvent
from app.services import compliance, delivery, outbox
from app.services import notifications as notes
from app.services.delivery import DeliveryError, LoggingProvider, Message, NoProviderError, SmtpEmailProvider, mask_recipient
from app.services.notifications import NotificationPublisher
from app.services.sweep import sweep_all
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_automotive import gbook, make_garage
from tests.test_customer_cancel import CODE, PHONE, cancel, login, otp, request_code, set_default_policy, verify  # noqa: F401
from tests.test_customer_identity import EMAIL, ask, booked_with_email, links, redeem  # noqa: F401
from tests.test_customer_reschedule import move


# --------------------------------------------------------------------- pure: templates
def test_placeholders_are_found_in_both_syntaxes_and_a_dollar_can_be_escaped() -> None:
    assert notes.placeholders_in("Hi $customer_name, ${services} at $time. Cost: $$5") == {"customer_name", "services", "time"}
    assert notes.placeholders_in("no placeholders") == set()


@pytest.mark.parametrize("text_", ["costs $5", "ends with $", "$ name", "${}", "${unclosed"])
def test_a_stray_dollar_sign_is_refused(text_: str) -> None:
    with pytest.raises(ValidationFailed):
        notes.placeholders_in(text_)


def test_unknown_placeholders_are_refused_when_saving() -> None:
    notes.validate_template("Hi $customer_name", "Your $services at $time")
    for subject, body in ((None, "Hello $password"), ("About $secret", "fine"), (None, "${customer_name.__class__}")):
        with pytest.raises(ValidationFailed):
            notes.validate_template(subject, body)


def test_rendering_substitutes_and_never_evaluates() -> None:
    ctx = {"customer_name": "Aisha", "time": "10:00"}
    assert notes.render("Hi $customer_name at ${time}$$", ctx) == "Hi Aisha at 10:00$"
    assert notes.render("$customer_name.__class__ and $missing", ctx) == "Aisha.__class__ and $missing"  # attributes are just text
    assert notes.render("{customer_name} {0}", ctx) == "{customer_name} {0}"  # str.format syntax means nothing here


def test_text_a_person_typed_loses_its_control_characters() -> None:
    assert notes.clean("Evil\r\nBcc: victim@example.com") == "Evil Bcc: victim@example.com"
    assert notes.clean("tab\there\x00nul") == "tab here nul"
    assert notes.clean("  plain  ") == "plain"


def test_every_notifiable_event_has_a_default_and_every_default_only_uses_known_placeholders() -> None:
    from app.models.notifications import NOTIFIABLE_EVENTS

    assert set(notes.DEFAULTS) == set(NOTIFIABLE_EVENTS)
    for subject, body in notes.DEFAULTS.values():
        notes.validate_template(subject, body)


def test_recipients_are_masked() -> None:
    assert mask_recipient("email", "aisha.karim@example.com") == "a***@example.com"
    assert mask_recipient("sms", "+60123450001") == "+60****0001"
    assert mask_recipient("whatsapp", "12345") == "****"
    for masked in (mask_recipient("email", "aisha.karim@example.com"), mask_recipient("sms", "+60123450001")):
        assert "karim" not in masked and "345000" not in masked


# --------------------------------------------------------------------- pure: providers
async def test_without_a_provider_delivery_fails_loudly() -> None:
    with pytest.raises(NoProviderError):
        await delivery.deliver(Message(channel="sms", recipient="+60123450001", body="x"))


async def test_the_logging_provider_writes_to_the_log(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.services.delivery"):
        assert await LoggingProvider().send(Message("email", "a@example.com", "hello", subject="Hi")) is None
    assert "a@example.com" in caplog.text and "hello" in caplog.text


def test_configure_providers_follows_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "_providers", {})
    base = get_settings()
    delivery.configure_providers(base.model_copy(update={"environment": "production", "smtp_host": ""}))
    assert delivery._providers == {}  # production with nothing configured sends nothing, visibly
    delivery.configure_providers(base.model_copy(update={"environment": "production", "smtp_host": "smtp.example.com", "smtp_user": "u", "smtp_password": "p"}))
    smtp = delivery.get_provider("email")
    assert isinstance(smtp, SmtpEmailProvider) and smtp.host == "smtp.example.com" and smtp.user == "u"
    assert delivery.get_provider("sms") is None and delivery.get_provider("whatsapp") is None
    delivery.configure_providers(base.model_copy(update={"environment": "development", "smtp_host": ""}))
    assert isinstance(delivery.get_provider("email"), SmtpEmailProvider)  # development fills only the gaps
    assert isinstance(delivery.get_provider("sms"), LoggingProvider) and isinstance(delivery.get_provider("whatsapp"), LoggingProvider)


class FakeSmtp:
    instances: list[FakeSmtp] = []
    fail_with: Exception | None = None

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout, self.calls, self.sent = host, port, timeout, [], []
        FakeSmtp.instances.append(self)
        if FakeSmtp.fail_with:
            raise FakeSmtp.fail_with

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *a: Any) -> None:
        self.calls.append("quit")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login:{user}:{password}")

    def send_message(self, mail: Any) -> None:
        self.sent.append(mail)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSmtp]:
    FakeSmtp.instances, FakeSmtp.fail_with = [], None
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    return FakeSmtp


async def test_the_smtp_provider_sends_a_plain_text_email(smtp: type[FakeSmtp]) -> None:
    provider = SmtpEmailProvider("smtp.example.com", 2525, "Shop <shop@example.com>", user="u", password="p")
    assert await provider.send(Message("email", "aisha@example.com", "Hello there", subject="Hi")) is None
    (conn,) = smtp.instances
    assert (conn.host, conn.port) == ("smtp.example.com", 2525) and conn.calls == ["starttls", "login:u:p", "quit"]
    mail = conn.sent[0]
    assert (mail["To"], mail["From"], mail["Subject"]) == ("aisha@example.com", "Shop <shop@example.com>", "Hi")
    assert mail.get_content().strip() == "Hello there"


async def test_the_smtp_provider_can_skip_tls_and_login(smtp: type[FakeSmtp]) -> None:
    await SmtpEmailProvider("localhost", 25, "a@example.com", starttls=False).send(Message("email", "b@example.com", "x", subject="s"))
    assert smtp.instances[0].calls == ["quit"]


@pytest.mark.parametrize("failure", [smtplib.SMTPRecipientsRefused({}), smtplib.SMTPServerDisconnected("gone"), ConnectionRefusedError("no")])
async def test_smtp_failures_become_delivery_errors(smtp: type[FakeSmtp], failure: Exception) -> None:
    smtp.fail_with = failure
    with pytest.raises(DeliveryError):
        await SmtpEmailProvider("h", 25, "a@example.com").send(Message("email", "b@example.com", "x", subject="s"))


async def test_the_smtp_provider_refuses_header_injection(smtp: type[FakeSmtp]) -> None:
    provider = SmtpEmailProvider("h", 25, "a@example.com")
    for message in (Message("email", "b@example.com\r\nBcc: x@example.com", "x", subject="s"), Message("email", "b@example.com", "x", subject="s\nBcc: x@example.com")):
        with pytest.raises(DeliveryError):
            await provider.send(message)
    assert smtp.instances == []


# --------------------------------------------------------------------- booking messages
async def run_outbox(s: Seed, *, now: dt.datetime | None = None) -> outbox.DispatchResult:
    async with tenant_session(s.tenant_id) as db:
        return await outbox.dispatch_pending(db, NotificationPublisher(), now=now)


async def log_rows(s: Seed, bid: str | None = None) -> list[NotificationLog]:
    async with tenant_session(s.tenant_id) as db:
        q = select(NotificationLog).order_by(NotificationLog.created_at, NotificationLog.id)
        if bid:
            q = q.where(NotificationLog.booking_id == uuid.UUID(bid))
        return list((await db.scalars(q)).all())


async def put_template(s: Seed, event: str, channel: str, **body: Any) -> httpx.Response:
    payload = {"body": "Hi $customer_name", **body}
    if channel == "email":
        payload.setdefault("subject", "Subject")
    return await s.client.put(f"/v1/admin/notification-templates/{event}/{channel}", json=payload, headers=s.admin)


async def test_a_confirmed_booking_sends_one_confirmation_email(seeded: Seed, mail: Recorder) -> None:
    bid = await booked_with_email(seeded, hour=10)
    result = await run_outbox(seeded)
    assert result.failed == 0
    (message,) = mail.sent  # booking.created is not announced separately when the booking starts out confirmed
    assert (message.channel, message.recipient, message.subject) == ("email", EMAIL, "Your booking is confirmed")
    day = at(seeded.day, 10)
    assert "Hi Aisha" in message.body and "Massage" in message.body and day.strftime("%a %d %b %Y") in message.body and "10:00" in message.body
    (row,) = await log_rows(seeded, bid)
    assert (row.event, row.channel, row.status, row.provider, row.attempts) == ("booking.confirmed", "email", "sent", "recorder", 1)
    assert row.recipient == "a***@example.com" and row.provider_message_id == "msg-1" and row.sent_at is not None and row.error is None
    assert not any(hasattr(row, field) for field in ("body", "subject", "message"))  # no text is kept
    again = await run_outbox(seeded)
    assert again.published == 0 and len(mail.sent) == 1  # published events are not delivered again


async def test_a_pending_booking_gets_a_received_message_and_then_the_verdict(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await make_seed(client, default_status="pending")
    bid = await booked_with_email(s, hour=10)
    await run_outbox(s)
    assert [m.subject for m in mail.sent] == ["We received your booking request"]
    assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "confirmed"}, headers=s.admin)).status_code == 200
    bid2 = await booked_with_email(s, "+60122220001", "other@example.com", hour=12)
    assert (await s.client.patch(f"/v1/admin/bookings/{bid2}/status", json={"status": "rejected"}, headers=s.admin)).status_code == 200
    await run_outbox(s)
    assert sorted(m.subject for m in mail.sent[1:]) == sorted(
        ["We received your booking request", "Your booking is confirmed", "We could not confirm your booking"]
    )


async def test_an_unanswered_request_that_times_out_tells_the_customer(client: httpx.AsyncClient, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await make_seed(client, default_status="pending")
    await set_default_policy(s, default_status="pending", pending_timeout=600)
    await booked_with_email(s)
    monkeypatch.setattr(outbox, "_publisher", NotificationPublisher())  # as the worker sets it up
    later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=20)
    report = await sweep_all(tenant_ids=[s.tenant_id], now=later)
    assert report.ran and not report.errors
    assert sorted(m.subject for m in mail.sent) == ["We could not confirm your booking", "We received your booking request"]


async def test_cancel_reschedule_and_completion_each_send_their_message(seeded: Seed, mail: Recorder, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
        at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": PHONE, "email": EMAIL}))
    bid = r.json()["id"]
    me = await login(seeded)
    assert (await move(seeded, me, bid, at(seeded.day, 15))).status_code == 200
    await run_outbox(seeded)
    moved = mail.sent[-1]
    assert moved.subject == "Your booking was moved"
    old, new = at(seeded.day, 10), at(seeded.day, 15)
    assert f"from {old.strftime('%a %d %b %Y')} at 10:00 to {new.strftime('%a %d %b %Y')} at 15:00" in moved.body
    assert (await cancel(seeded, me, bid)).status_code == 200
    await run_outbox(seeded)
    assert mail.sent[-1].subject == "Your booking was cancelled" and "15:00" in mail.sent[-1].body
    bid2 = (await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
        at(seeded.day, 11), mei, customer={"name": "Aisha", "phone": PHONE, "email": EMAIL}))).json()["id"]
    assert (await seeded.client.patch(f"/v1/admin/bookings/{bid2}/status", json={"status": "completed"}, headers=seeded.admin)).status_code == 200
    await run_outbox(seeded)
    assert mail.sent[-1].subject == "Thank you for visiting us"


async def test_a_workshop_tells_the_customer_a_job_is_waiting_for_parts(client: httpx.AsyncClient, mail: Recorder) -> None:
    garage = await make_garage(client)
    r = await gbook(garage, at(garage.day, 10), ["oil"], customer={"name": "Aisha", "phone": PHONE, "email": EMAIL})
    assert r.status_code == 201, r.text
    for status in ("in-progress", "waiting-for-parts"):
        assert (await garage.client.patch(f"/v1/admin/bookings/{r.json()['id']}/status", json={"status": status}, headers=garage.admin)).status_code == 200
    await run_outbox(garage)
    assert "We are waiting for parts for your vehicle" in [m.subject for m in mail.sent]
    assert "Oil change" in mail.sent[-1].body


async def test_a_customer_without_an_email_is_recorded_as_skipped_not_failed(seeded: Seed, mail: Recorder) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    result = await run_outbox(seeded)
    assert mail.sent == [] and result.failed == 0
    (row,) = await log_rows(seeded, r.json()["id"])
    assert (row.status, row.error, row.recipient) == ("skipped", "no email address on file", None)


async def test_a_business_can_add_sms_and_whatsapp_and_rewrite_or_silence_email(seeded: Seed, mail: Recorder) -> None:
    assert (await put_template(seeded, "booking.confirmed", "sms", body="$business_name: $customer_name, $services at $time")).status_code == 200
    assert (await put_template(seeded, "booking.confirmed", "whatsapp", body="x", external_template="booking_confirmed_v2")).status_code == 200
    assert (await put_template(seeded, "booking.confirmed", "email", subject="Hello $customer_name", body="Custom for $date")).status_code == 200
    bid = await booked_with_email(seeded, hour=10)
    await run_outbox(seeded)
    by_channel = {m.channel: m for m in mail.sent}
    assert set(by_channel) == {"email", "sms", "whatsapp"}
    assert by_channel["email"].subject == "Hello Aisha" and by_channel["email"].body.startswith("Custom for ")
    assert by_channel["sms"].recipient == PHONE and by_channel["sms"].body.endswith(": Aisha, Massage at 10:00")
    assert by_channel["whatsapp"].external_template == "booking_confirmed_v2" and by_channel["whatsapp"].params[0] == "Aisha"
    assert sorted(r.channel for r in await log_rows(seeded, bid)) == ["email", "sms", "whatsapp"]
    # switching the email off leaves the other channels
    mail.sent.clear()
    assert (await put_template(seeded, "booking.cancelled", "email", subject="x", body="y", active=False)).status_code == 200
    assert (await put_template(seeded, "booking.cancelled", "sms", body="cancelled $time")).status_code == 200
    me_bid = bid
    async with tenant_session(seeded.tenant_id) as db:
        from app.services.booking import transition_booking
        tenant = await db.get(Tenant, seeded.tenant_id)
        assert tenant is not None
        await transition_booking(db, tenant, uuid.UUID(me_bid), "cancelled", actor="staff")
    await run_outbox(seeded)
    assert [(m.channel, m.body) for m in mail.sent] == [("sms", "cancelled 10:00")]


async def test_templates_belong_to_one_business(client: httpx.AsyncClient, mail: Recorder) -> None:
    a, b = await make_seed(client), await make_seed(client)
    assert (await put_template(a, "booking.confirmed", "sms", body="A says $time")).status_code == 200
    assert (await put_template(a, "booking.confirmed", "email", subject="A", body="A only")).status_code == 200
    await booked_with_email(b)
    await run_outbox(b)
    assert [m.channel for m in mail.sent] == ["email"] and mail.sent[0].subject == "Your booking is confirmed"  # B keeps the default


async def test_a_redelivered_event_sends_nothing_twice(seeded: Seed, mail: Recorder) -> None:
    await put_template(seeded, "booking.confirmed", "sms", body="hello")
    await booked_with_email(seeded)
    await run_outbox(seeded)
    assert len(mail.sent) == 2
    async with tenant_session(seeded.tenant_id) as db:  # the broker "forgets" it published: at-least-once delivery
        await db.execute(text("UPDATE outbox_events SET published_at = NULL"))
    await run_outbox(seeded)
    assert len(mail.sent) == 2 and len(await log_rows(seeded)) == 2


async def test_a_failing_channel_is_retried_alone_with_backoff(seeded: Seed, mail: Recorder) -> None:
    await put_template(seeded, "booking.confirmed", "sms", body="sms body")
    bid = await booked_with_email(seeded)
    mail.failing["email"] = RuntimeError("provider down")
    start = dt.datetime.now(dt.UTC)
    first = await run_outbox(seeded, now=start)
    assert first.failed == 1 and [m.channel for m in mail.sent] == ["sms"]
    rows = {r.channel: r for r in await log_rows(seeded, bid)}
    assert (rows["sms"].status, rows["email"].status, rows["email"].attempts) == ("sent", "failed", 1)
    assert rows["email"].error == "RuntimeError: provider down"
    assert (await run_outbox(seeded, now=start)).failed == 0  # waiting out its back-off
    second = await run_outbox(seeded, now=start + dt.timedelta(minutes=2))
    assert second.failed == 1 and [m.channel for m in mail.sent] == ["sms"]  # still failing; the SMS was not sent again
    mail.failing.clear()
    third = await run_outbox(seeded, now=start + dt.timedelta(minutes=10))
    assert third.published >= 1 and sorted(m.channel for m in mail.sent) == ["email", "sms"]
    rows = {r.channel: r for r in await log_rows(seeded, bid)}
    assert (rows["email"].status, rows["email"].attempts, rows["email"].error) == ("sent", 3, None) and rows["sms"].attempts == 1


async def test_a_message_that_keeps_failing_is_given_up_on_and_stays_failed(seeded: Seed, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "notify_max_attempts", 2)
    bid = await booked_with_email(seeded)
    mail.failing["email"] = RuntimeError("nope")
    start = dt.datetime.now(dt.UTC)
    assert (await run_outbox(seeded, now=start)).failed == 1
    last = await run_outbox(seeded, now=start + dt.timedelta(minutes=5))
    assert last.failed == 0 and last.published >= 1  # no more retries
    (row,) = await log_rows(seeded, bid)
    assert (row.status, row.attempts) == ("failed", 2)
    mail.failing.clear()
    assert (await run_outbox(seeded, now=start + dt.timedelta(hours=2))).published == 0 and mail.sent == []


async def test_a_channel_with_no_provider_fails_visibly(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "_providers", {})
    bid = await booked_with_email(seeded)
    assert (await run_outbox(seeded)).failed == 1
    (row,) = await log_rows(seeded, bid)
    assert row.status == "failed" and row.error is not None and row.error.startswith("NoProviderError")


async def test_an_erased_customer_is_not_contacted(seeded: Seed, mail: Recorder) -> None:
    bid = await booked_with_email(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE customers SET erased_at = now()"))
    await run_outbox(seeded)
    assert mail.sent == []
    (row,) = await log_rows(seeded, bid)
    assert (row.status, row.error) == ("skipped", "customer erased")


async def test_what_a_customer_typed_cannot_break_out_of_a_header_or_a_template(seeded: Seed, mail: Recorder) -> None:
    await put_template(seeded, "booking.confirmed", "email", subject="For $customer_name", body="Dear $customer_name")
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
        at(seeded.day, 10), mei, customer={"name": "Eve\r\nBcc: x@example.com $services ${business_name}", "phone": PHONE, "email": EMAIL}))
    assert r.status_code == 201, r.text
    await run_outbox(seeded)
    (message,) = mail.sent
    assert "\n" not in message.subject and "\r" not in message.subject
    assert message.subject == "For Eve Bcc: x@example.com $services ${business_name}"  # the name is data: its "placeholders" are not expanded


async def test_erasing_a_customer_deletes_their_notification_log(seeded: Seed, mail: Recorder) -> None:
    bid = await booked_with_email(seeded)
    await run_outbox(seeded)
    assert len(await log_rows(seeded, bid)) == 1
    async with tenant_session(seeded.tenant_id) as db:
        tenant = await db.get(Tenant, seeded.tenant_id)
        customer = await db.scalar(select(Customer))
        assert tenant is not None and customer is not None
        await db.execute(text("UPDATE bookings SET status = 'completed'"))  # nothing open: erasure is allowed
        await compliance.erase_customer(db, tenant, customer.id, dt.datetime.now(dt.UTC))
    assert await log_rows(seeded) == []


async def test_the_worker_connects_the_notifications_to_the_outbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_publisher", None)
    monkeypatch.setattr(delivery, "_providers", {})
    ran: list[bool] = []

    async def fake_run(*a: Any, **k: Any) -> None:
        found = outbox.get_event_publisher()
        ran.append(isinstance(found, outbox.CompositePublisher) and any(isinstance(p, NotificationPublisher) for p in found.publishers))

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(worker, "dispose_engine", lambda: _noop())
    monkeypatch.setattr(get_settings(), "environment", "development")
    await worker.amain(["--once"])
    assert ran == [True]
    assert isinstance(delivery.get_provider("email"), LoggingProvider)  # and its providers are registered


async def _noop() -> None:
    return None


async def test_the_sweep_delivers_notifications_when_the_worker_has_set_them_up(seeded: Seed, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox, "_publisher", NotificationPublisher())
    await booked_with_email(seeded)
    report = await sweep_all(tenant_ids=[seeded.tenant_id])
    assert report.ran and not report.errors and report.tenants[0].events_published >= 1
    assert [m.subject for m in mail.sent] == ["Your booking is confirmed"]


# --------------------------------------------------------------------- admin
async def test_the_template_list_shows_the_defaults_and_what_the_business_changed(seeded: Seed) -> None:
    listing = (await seeded.client.get("/v1/admin/notification-templates", headers=seeded.admin)).json()
    assert {(t["event"], t["channel"], t["source"]) for t in listing} == {(e, "email", "default") for e in notes.DEFAULTS}
    await put_template(seeded, "booking.cancelled", "sms", body="Cancelled")
    await put_template(seeded, "booking.confirmed", "email", subject="S", body="B", active=False)
    listing = {(t["event"], t["channel"]): t for t in (await seeded.client.get("/v1/admin/notification-templates", headers=seeded.admin)).json()}
    assert listing[("booking.cancelled", "sms")]["source"] == "custom" and listing[("booking.cancelled", "email")]["source"] == "default"
    assert listing[("booking.confirmed", "email")]["active"] is False and listing[("booking.confirmed", "email")]["body"] == "B"


async def test_templates_are_validated(seeded: Seed) -> None:
    ok = {"subject": "Hi", "body": "Hello $customer_name"}
    put = lambda event, channel, body: seeded.client.put(f"/v1/admin/notification-templates/{event}/{channel}", json=body, headers=seeded.admin)  # noqa: E731
    for event, channel, body in [
        ("booking.confirmed", "email", {**ok, "body": "Your $password"}),  # unknown placeholder
        ("booking.confirmed", "email", {**ok, "body": "Costs $5"}),  # stray dollar
        ("booking.confirmed", "email", {"body": "no subject"}),  # an email needs a subject
        ("booking.confirmed", "sms", {**ok}),  # only emails have one
        ("booking.confirmed", "sms", {"body": "x", "external_template": "t"}),  # WhatsApp only
        ("booking.confirmed", "email", {**ok, "body": ""}),
        ("booking.confirmed", "email", {**ok, "body": "x" * 2001}),
        ("booking.confirmed", "email", {**ok, "subject": "x" * 201}),
        ("booking.confirmed", "fax", ok),
        ("booking.exploded", "email", ok),
        ("auth.otp", "sms", {"body": "x"}),  # sign-in messages are not editable
    ]:
        assert (await put(event, channel, body)).status_code == 422, (event, channel, body)
    bad = await put("booking.confirmed", "email", {**ok, "body": "Your $password"})
    assert "password" in bad.json()["detail"] and "customer_name" in bad.json()["allowed"]
    assert (await put("booking.confirmed", "email", ok)).status_code == 200


async def test_a_template_can_be_replaced_and_deleted(seeded: Seed) -> None:
    await put_template(seeded, "booking.confirmed", "sms", body="one")
    again = await put_template(seeded, "booking.confirmed", "sms", body="two", active=False)
    assert again.json()["body"] == "two" and again.json()["active"] is False
    async with tenant_session(seeded.tenant_id) as db:
        assert await db.scalar(select(func.count()).select_from(NotificationLog.metadata.tables["notification_templates"])) == 1
    path = "/v1/admin/notification-templates/booking.confirmed/sms"
    assert (await seeded.client.delete(path, headers=seeded.admin)).status_code == 204
    assert (await seeded.client.delete(path, headers=seeded.admin)).status_code == 404
    await put_template(seeded, "booking.confirmed", "email", subject="S", body="B")
    assert (await seeded.client.delete("/v1/admin/notification-templates/booking.confirmed/email", headers=seeded.admin)).status_code == 204
    listing = (await seeded.client.get("/v1/admin/notification-templates", headers=seeded.admin)).json()
    assert [t["source"] for t in listing if (t["event"], t["channel"]) == ("booking.confirmed", "email")] == ["default"]


async def test_notification_admin_needs_a_manager(seeded: Seed) -> None:
    staff = await seeded.token("staff")
    for method, path in [("GET", "/v1/admin/notification-templates"), ("GET", "/v1/admin/notifications"), ("GET", "/v1/admin/notification-placeholders"),
                         ("PUT", "/v1/admin/notification-templates/booking.confirmed/sms"), ("DELETE", "/v1/admin/notification-templates/booking.confirmed/sms")]:
        r = await seeded.client.request(method, path, headers=staff, json={"body": "x"} if method == "PUT" else None)
        assert r.status_code == 403, (method, path)
        assert (await seeded.client.request(method, path, headers=seeded.pk, json={"body": "x"} if method == "PUT" else None)).status_code in (401, 403)
    assert (await seeded.client.get("/v1/admin/notification-placeholders", headers=await seeded.token("manager"))).status_code == 200


async def test_the_notification_log_can_be_filtered_and_is_private_to_the_business(client: httpx.AsyncClient, mail: Recorder) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await put_template(a, "booking.confirmed", "sms", body="x")
    first = await booked_with_email(a, "+60111110001", "one@example.com", hour=10)
    await booked_with_email(a, "+60111110002", "two@example.com", hour=12)
    mail.failing["sms"] = RuntimeError("sms gateway down")
    await run_outbox(a)
    await booked_with_email(b)
    await run_outbox(b)
    get = lambda s, **p: s.client.get("/v1/admin/notifications", params=p, headers=s.admin)  # noqa: E731
    everything = (await get(a)).json()
    assert len(everything) == 4 and {e["status"] for e in everything} == {"sent", "failed"}
    assert {e["status"] for e in (await get(a, status="failed")).json()} == {"failed"}
    assert len((await get(a, status="sent")).json()) == 2
    for_one = (await get(a, booking_id=first)).json()
    assert len(for_one) == 2 and {e["booking_id"] for e in for_one} == {first}
    assert len((await get(a, event="booking.confirmed", limit=1)).json()) == 1
    assert (await get(a, event="booking.cancelled")).json() == []
    assert {e["event"] for e in (await get(a, event="booking.confirmed")).json()} == {"booking.confirmed"}
    assert len((await get(b)).json()) == 1  # business B sees only its own
    assert (await get(a, status="exploded")).status_code == 422
    sample = everything[0]
    assert set(sample) == {"id", "event", "channel", "booking_id", "customer_id", "recipient", "status", "provider", "error", "attempts", "created_at", "sent_at"}


# --------------------------------------------------------------------- sign-in messages
async def test_the_otp_code_is_delivered_and_works(seeded: Seed, otp: Any, mail: Recorder) -> None:
    assert (await request_code(seeded, "012-345 0001")).status_code == 202
    (message,) = mail.sent
    assert (message.channel, message.recipient) == ("whatsapp", "+60123450001")  # the normalised number
    assert CODE in message.body and "5 minutes" in message.body
    assert (await verify(seeded, "+60123450001", code=[w for w in message.body.split() if w.rstrip(".").isdigit()][0].rstrip("."))).status_code == 200
    (row,) = await log_rows(seeded)
    assert (row.event, row.channel, row.status, row.recipient, row.customer_id) == ("auth.otp", "whatsapp", "sent", "+60****0001", None)
    assert CODE not in " ".join(str(v) for v in (row.error, row.recipient, row.provider_message_id))


async def test_the_otp_channel_is_configurable(seeded: Seed, otp: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_channel", "sms")
    await request_code(seeded)
    assert [m.channel for m in mail.sent] == ["sms"]


async def test_a_code_that_could_not_be_sent_is_discarded_and_can_be_requested_again(seeded: Seed, otp: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 60)
    mail.failing["whatsapp"] = RuntimeError("gateway down")
    r = await request_code(seeded)
    assert r.status_code == 503 and r.json()["error"] == "delivery_failed"
    assert await otp.keys("otp:*") == []  # no usable code, no cool-down held against the customer
    (row,) = await log_rows(seeded)  # and the failure is on record although the request itself failed
    assert (row.event, row.status, row.error) == ("auth.otp", "failed", "RuntimeError: gateway down")
    mail.failing.clear()
    assert (await request_code(seeded)).status_code == 202


async def test_with_no_provider_no_code_is_issued(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery, "_providers", {})
    r = await request_code(seeded)
    assert r.status_code == 503 and await otp.keys("otp:*") == []


async def test_the_magic_link_is_emailed_and_the_emailed_token_signs_in(seeded: Seed, otp: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "magic_link_url", "https://shop.example.com/login?t={token}")
    await booked_with_email(seeded)
    assert (await ask(seeded)).status_code == 202
    (message,) = mail.sent
    assert (message.channel, message.recipient) == ("email", EMAIL) and "Use this link to sign in" in message.body
    token = message.body.split("https://shop.example.com/login?t=")[1].split()[0]
    assert (await redeem(seeded, token)).status_code == 200
    rows = [r for r in await log_rows(seeded) if r.event == "auth.magic_link"]
    assert [(r.status, r.recipient) for r in rows] == [("sent", "a***@example.com")] and rows[0].customer_id is not None
    assert token not in " ".join(str(v) for v in (rows[0].error, rows[0].recipient))


async def test_without_a_link_template_the_email_carries_the_bare_token(seeded: Seed, otp: Any, mail: Recorder) -> None:
    await booked_with_email(seeded)
    await ask(seeded)
    token = mail.sent[0].body.split("Your sign-in token: ")[1].split()[0]
    assert (await redeem(seeded, token)).status_code == 200


async def test_a_failed_magic_link_looks_the_same_to_the_caller_and_leaves_no_valid_token(seeded: Seed, otp: Any, mail: Recorder, links: list[str]) -> None:
    await booked_with_email(seeded)
    mail.failing["email"] = RuntimeError("smtp down")
    known = await ask(seeded)
    unknown = await ask(seeded, "nobody@example.com")
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json()) == (202, {"status": "sent"})
    assert (await redeem(seeded, links[0])).status_code == 401  # nobody got that token, so nobody may use it
    assert [r.status for r in await log_rows(seeded) if r.event == "auth.magic_link"] == ["failed"]
    mail.failing.clear()
    assert (await ask(seeded)).status_code == 202 and (await redeem(seeded, links[-1])).status_code == 200


async def test_the_magic_link_subject_names_the_business(seeded: Seed, otp: Any, mail: Recorder) -> None:
    await booked_with_email(seeded)
    await ask(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        tenant = await db.get(Tenant, seeded.tenant_id)
        assert tenant is not None
    assert mail.sent[0].subject == f"Your sign-in link for {tenant.name}"


async def test_the_api_registers_its_providers_when_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    monkeypatch.setattr(delivery, "_providers", {})
    monkeypatch.setattr(get_settings(), "environment", "development")
    async with app.router.lifespan_context(app):
        assert isinstance(delivery.get_provider("whatsapp"), LoggingProvider)  # so a developer can read the OTP off the console


async def test_the_business_name_cannot_inject_lines_either(client: httpx.AsyncClient, mail: Recorder) -> None:
    r = await client.post("/v1/auth/tenants", headers={"X-Platform-Token": get_settings().platform_admin_token},
                          json={"name": "Shop\r\nBcc: x@example.com", "business_type": "spa"})
    s = Seed(client, r.json())
    s.admin = await s.token("owner")
    assert (await client.post("/v1/admin/policies", headers=s.admin, json={"name": "D", "default_status": "confirmed", "min_notice_booking": 60, "min_notice_cancel": 3600, "booking_window": 86400 * 60, "pending_timeout": 1800, "make_default": True})).status_code == 201
    s.service_id = uuid.UUID((await client.post("/v1/admin/services", headers=s.admin, json={"name": "Massage", "duration_minutes": 60})).json()["id"])
    await put_template(s, "booking.confirmed", "email", subject="From $business_name", body="Dear $customer_name")
    await booked_with_email(s)
    await run_outbox(s)
    assert mail.sent[0].subject == "From Shop Bcc: x@example.com"
