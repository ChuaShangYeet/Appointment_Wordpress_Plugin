"""A recipient the provider will never accept (a number not on WhatsApp, a mailbox that does not exist) is ``refused``: logged once, not retried, and it does not hold
back the booking's other messages. Before, it was tried six times over a quarter of an hour and everything after it waited."""
from __future__ import annotations

import datetime as dt
import smtplib
from collections import Counter
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.models import NotificationLog, Tenant
from app.services import delivery, outbox, plans
from app.services.delivery import (
    TWILIO_REFUSED,
    WHATSAPP_REFUSED,
    DeliveryError,
    Message,
    PermanentDeliveryError,
    SmtpEmailProvider,
)
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_customer_cancel import CODE, PHONE, otp, request_code  # noqa: F401
from tests.test_customer_identity import booked_with_email
from tests.test_gateways import Gateway, twilio, whatsapp
from tests.test_notifications import log_rows, put_template, run_outbox
from tests.test_staff_alerts import collide, set_contact
from tests.test_salon import Salon, salon  # noqa: F401  (``salon`` is a fixture)
from tests.test_webhook_alerts import break_the_receiver, set_contact as set_business_contact  # noqa: F401
from tests.test_calendar import google, redis  # noqa: F401  (fixtures)
from tests.test_webhooks import net, tick, worker_publishers  # noqa: F401  (fixtures)

REFUSAL = PermanentDeliveryError("WhatsApp API 400: 131026 Message undeliverable")


class Counting(Recorder):
    """The recorder, which also counts every attempt per channel (a refused send raises before it is recorded as sent)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: Counter[str] = Counter()

    async def send(self, message: Any) -> str | None:
        self.calls[message.channel] += 1
        return await super().send(message)


@pytest.fixture
def counting(monkeypatch: pytest.MonkeyPatch) -> Counting:
    rec = Counting()
    monkeypatch.setattr(delivery, "_providers", {"email": rec, "sms": rec, "whatsapp": rec})
    return rec


def subjects(rec: Recorder) -> list[str]:
    return [m.subject for m in rec.sent if m.channel == "email" and m.subject]


# --------------------------------------------------------------------- which refusals are permanent
class RefusingSmtp:
    def __init__(self, recipients: dict[str, tuple[int, bytes]]) -> None:
        self.recipients = recipients

    def __call__(self, host: str, port: int, timeout: float) -> Any:
        raise smtplib.SMTPRecipientsRefused(self.recipients)


@pytest.mark.parametrize(("recipients", "permanent"), [
    ({"b@example.com": (550, b"no such user")}, True),
    ({"b@example.com": (553, b"bad mailbox name")}, True),
    ({"b@example.com": (550, b"no"), "c@example.com": (551, b"no")}, True),
    ({"b@example.com": (452, b"mailbox full")}, False),  # 4xx: try again
    ({"b@example.com": (550, b"no"), "c@example.com": (450, b"later")}, False),  # one may still come good
    ({}, False),  # nothing to go on
], ids=["550", "553", "all 5xx", "452 mailbox full", "mixed", "empty"])
async def test_an_smtp_refusal_of_the_recipient_is_permanent_only_when_every_code_is_5xx(
    monkeypatch: pytest.MonkeyPatch, recipients: dict[str, tuple[int, bytes]], permanent: bool
) -> None:
    monkeypatch.setattr(smtplib, "SMTP", RefusingSmtp(recipients))
    with pytest.raises(DeliveryError) as raised:
        await SmtpEmailProvider("h", 25, "a@example.com").send(Message("email", "b@example.com", "x", subject="s"))
    assert isinstance(raised.value, PermanentDeliveryError) is permanent


async def test_other_smtp_trouble_is_never_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    for failure in (smtplib.SMTPAuthenticationError(535, b"bad credentials"), smtplib.SMTPServerDisconnected("gone"), ConnectionRefusedError("no"), smtplib.SMTPDataError(554, b"rejected")):
        def boom(host: str, port: int, timeout: float, failure: Exception = failure) -> Any:
            raise failure

        monkeypatch.setattr(smtplib, "SMTP", boom)
        with pytest.raises(DeliveryError) as raised:
            await SmtpEmailProvider("h", 25, "a@example.com").send(Message("email", "b@example.com", "x", subject="s"))
        assert not isinstance(raised.value, PermanentDeliveryError), failure


def meta(code: int, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"error": {"message": "nope", "code": code}})


def tw(code: int, status: int = 400) -> httpx.Response:
    return httpx.Response(status, json={"code": code, "message": "nope", "status": status})


@pytest.mark.parametrize("code", sorted(WHATSAPP_REFUSED))
async def test_whatsapp_codes_for_a_recipient_are_permanent(code: int) -> None:
    gw = Gateway()
    gw.answers = [meta(code)]
    with pytest.raises(PermanentDeliveryError) as raised:
        await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x"))
    assert str(code) in str(raised.value)


@pytest.mark.parametrize(("response", "why"), [
    (meta(131030), "a recipient outside the test list is the business's set-up to fix"),
    (meta(131056), "pair rate limit: wait and retry"),
    (meta(130429), "rate limit"),
    (meta(132000), "template parameters: the business's set-up"),
    (meta(190), "access token expired"),
    (meta(131026, status=500), "a server error is not an answer about the recipient"),
    (httpx.Response(400, text="not json"), "no code to go on"),
    (httpx.Response(400, json={"error": {"code": "131026"}}), "a code that is not a number"),
    (httpx.Response(400, json=[1, 2]), "an odd body"),
    (httpx.Response(400, json={"error": {"code": [131026]}}), "a code that cannot even be looked up"),
], ids=["131030", "131056", "130429", "132000", "190", "5xx", "text", "string code", "list", "list code"])
async def test_other_whatsapp_trouble_is_retried(response: httpx.Response, why: str) -> None:
    gw = Gateway()
    gw.answers = [response]
    with pytest.raises(DeliveryError) as raised:
        await whatsapp(gw).send(Message("whatsapp", "+60123450001", "x"))
    assert not isinstance(raised.value, PermanentDeliveryError), why


@pytest.mark.parametrize("code", sorted(TWILIO_REFUSED))
async def test_twilio_codes_for_a_recipient_are_permanent(code: int) -> None:
    gw = Gateway()
    gw.answers = [tw(code)]
    with pytest.raises(PermanentDeliveryError) as raised:
        await twilio(gw).send(Message("sms", "+60123450001", "x"))
    assert str(code) in str(raised.value)


@pytest.mark.parametrize("response", [tw(21408), tw(20003, 401), tw(20429, 429), tw(21211, 503), httpx.Response(400, text="x")], ids=["region", "auth", "rate", "5xx", "text"])
async def test_other_twilio_trouble_is_retried(response: httpx.Response) -> None:
    gw = Gateway()
    gw.answers = [response]
    with pytest.raises(DeliveryError) as raised:
        await twilio(gw).send(Message("sms", "+60123450001", "x"))
    assert not isinstance(raised.value, PermanentDeliveryError)


# --------------------------------------------------------------------- what the outbox does with it
async def pending_booking(client: httpx.AsyncClient, *templates: tuple[str, str]) -> tuple[Seed, str]:
    """A business whose bookings start pending, with WhatsApp / SMS templates for 'we received your request', and one booking made (not yet approved)."""
    s = await make_seed(client, default_status="pending")
    for channel, template in templates:
        assert (await put_template(s, "booking.created", channel, body="x", **({"external_template": template} if template else {}))).status_code == 200
    return s, await booked_with_email(s, hour=10)


async def approve(s: Seed, bid: str) -> None:
    assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "confirmed"}, headers=s.admin)).status_code == 200


async def test_a_refused_channel_is_logged_once_and_holds_nothing_back(client: httpx.AsyncClient, counting: Counting) -> None:
    s, bid = await pending_booking(client, ("whatsapp", "t"))
    await approve(s, bid)
    counting.failing["whatsapp"] = REFUSAL
    result = await run_outbox(s)
    assert result.failed == 0 and result.published >= 2  # both events went out in this one pass
    assert subjects(counting) == ["We received your booking request", "Your booking is confirmed"]
    assert counting.calls["whatsapp"] == 1
    (refused,) = [r for r in await log_rows(s, bid) if r.channel == "whatsapp"]
    assert (refused.status, refused.attempts, refused.sent_at, refused.provider) == ("refused", 1, None, None)
    assert refused.error == "PermanentDeliveryError: WhatsApp API 400: 131026 Message undeliverable" and refused.recipient == "+60****0001"  # (masked, as for every attempt)
    later = await run_outbox(s, now=dt.datetime.now(dt.UTC) + dt.timedelta(hours=2))
    assert later.published == 0 and counting.calls["whatsapp"] == 1  # nothing is tried again


async def test_a_refused_message_is_kept_and_listed_for_the_manager(client: httpx.AsyncClient, counting: Counting) -> None:
    s, bid = await pending_booking(client, ("whatsapp", "t"))
    counting.failing["whatsapp"] = REFUSAL
    await run_outbox(s)
    listed = (await s.client.get("/v1/admin/notifications?status=refused", headers=s.admin)).json()
    assert [(row["channel"], row["status"], row["booking_id"]) for row in listed] == [("whatsapp", "refused", bid)]
    assert (await s.client.get("/v1/admin/notifications?status=nonsense", headers=s.admin)).status_code == 422


async def test_a_refused_message_does_not_use_up_the_months_allowance(client: httpx.AsyncClient, counting: Counting) -> None:
    s, _ = await pending_booking(client, ("whatsapp", "t"))
    counting.failing["whatsapp"] = REFUSAL
    await run_outbox(s)
    now = dt.datetime.now(dt.UTC)
    async with tenant_session(s.tenant_id) as db:
        tenant = await db.get(Tenant, s.tenant_id)
        assert tenant is not None
        assert await plans.messages_this_month(db, tenant, "whatsapp", now) == 0


async def test_a_refused_channel_is_not_tried_again_while_another_channel_is(client: httpx.AsyncClient, counting: Counting) -> None:
    s, bid = await pending_booking(client, ("whatsapp", "t"), ("sms", ""))
    await approve(s, bid)
    counting.failing["whatsapp"] = REFUSAL
    counting.failing["sms"] = RuntimeError("gateway down")
    clock = dt.datetime.now(dt.UTC)
    first = await run_outbox(s, now=clock)
    assert first.failed == 1 and counting.calls["whatsapp"] == 1 and counting.calls["sms"] == 1
    assert subjects(counting) == ["We received your booking request"]  # the sms failure is transient: the confirmation waits its turn
    second = await run_outbox(s, now=clock + dt.timedelta(seconds=31))
    assert second.failed == 1 and counting.calls["sms"] == 2 and counting.calls["whatsapp"] == 1  # the refused one is left alone
    counting.failing.pop("sms")
    await run_outbox(s, now=clock + dt.timedelta(seconds=31 + 61 + 1))
    assert counting.calls["whatsapp"] == 1 and subjects(counting)[-1] == "Your booking is confirmed"
    rows = {r.channel: (r.status, r.attempts) for r in await log_rows(s, bid) if r.event == "booking.created"}
    assert rows == {"email": ("sent", 1), "whatsapp": ("refused", 1), "sms": ("sent", 3)}


async def test_a_transient_failure_is_still_retried_and_still_keeps_the_order(client: httpx.AsyncClient, counting: Counting) -> None:
    """The behaviour that stays: only a *permanent* refusal skips the queue. A gateway that is merely down is retried (six attempts) and the next message waits."""
    s, bid = await pending_booking(client, ("whatsapp", "t"))
    await approve(s, bid)
    counting.failing["whatsapp"] = RuntimeError("gateway down")
    now = dt.datetime.now(dt.UTC)
    for attempt in range(1, 6):
        await run_outbox(s, now=now)
        assert "Your booking is confirmed" not in subjects(counting)
        now += outbox.backoff(attempt) + dt.timedelta(seconds=1)
    await run_outbox(s, now=now)  # the sixth attempt gives up and lets the next one through
    assert counting.calls["whatsapp"] == 6 and "Your booking is confirmed" in subjects(counting)
    (row,) = [r for r in await log_rows(s, bid) if r.channel == "whatsapp"]
    assert (row.status, row.attempts) == ("failed", 6)


async def test_a_refusal_does_not_stop_the_other_channels_of_the_same_event(client: httpx.AsyncClient, counting: Counting) -> None:
    s, bid = await pending_booking(client, ("sms", ""), ("whatsapp", "t"))
    counting.failing["whatsapp"] = REFUSAL
    counting.sent.clear()
    await run_outbox(s)
    channels = sorted(m.channel for m in counting.sent)
    assert channels == ["email", "sms"]


async def test_a_sign_in_code_to_a_refused_number_is_still_a_delivery_failure(seeded: Seed, otp: Any, mail: Recorder) -> None:
    """Sign-in codes are not retried by the outbox: the person is waiting, so they are told it failed whatever the reason."""
    mail.failing["whatsapp"] = REFUSAL
    r = await request_code(seeded)
    assert r.status_code == 503
    (row,) = [x for x in await log_rows(seeded) if x.event == "auth.otp"]
    assert row.status == "failed" and row.sent_at is None


# --------------------------------------------------------------------- the alerts to the business and the staff
async def test_a_refused_alert_to_the_business_is_logged_and_not_retried(seeded: Seed, net: Any, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import webhooks as hooks

    await set_business_contact(seeded)
    await break_the_receiver(seeded, net, monkeypatch)
    worker_publishers.failing["email"] = PermanentDeliveryError("SMTP delivery failed: the server refused the recipient")
    clock = dt.datetime.now(dt.UTC)
    await tick(seeded, clock)
    report = await tick(seeded, clock + dt.timedelta(minutes=1))
    assert report.events_failed == 0
    await tick(seeded, clock + dt.timedelta(minutes=30))
    rows = [r for r in await log_rows(seeded) if r.event == hooks.ENDPOINT_DISABLED_EVENT]
    assert [(r.status, r.attempts) for r in rows] == [("refused", 1)]


async def test_a_refused_alert_to_the_stylist_does_not_stop_the_text_message(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com", contact_phone="012-345 0001")
    await collide(salon)
    mail.failing["email"] = PermanentDeliveryError("SMTP delivery failed: the server refused the recipient")
    result = await run_outbox(salon)
    assert result.failed == 0
    assert [m.channel for m in mail.sent] == ["sms"]
    async with tenant_session(salon.tenant_id) as db:
        rows = {r.channel: (r.status, r.attempts) for r in (await db.scalars(select(NotificationLog).where(NotificationLog.event == "booking.schedule_conflict"))).all()}
    assert rows == {"email": ("refused", 1), "sms": ("sent", 1)}
    again = await run_outbox(salon, now=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    assert again.published == 0 and [m.channel for m in mail.sent] == ["sms"]


def test_the_allowed_log_statuses_and_the_check_constraint_agree() -> None:
    """A guard for the model: the allowed statuses and the check constraint stay in step."""
    from app.models.notifications import LOG_STATUSES, NotificationLog as Log

    check = next(c for c in Log.__table__.constraints if getattr(c, "name", None) and str(c.name).endswith("status")).sqltext.text  # type: ignore[attr-defined]
    assert all(f"'{status}'" in check for status in LOG_STATUSES)


def test_exactly_these_provider_codes_mean_never() -> None:
    assert WHATSAPP_REFUSED == {131026, 131047, 131049, 131021}
    assert TWILIO_REFUSED == {21211, 21214, 21217, 21610, 21612, 21614}


async def test_a_refused_plan_warning_is_logged_and_not_retried(client: httpx.AsyncClient, mail: Recorder) -> None:
    from app.services import plans as plans_module
    from tests.test_plan_overrides import services_up_to
    from tests.test_plan_overrides import set_contact as plan_contact
    from tests.test_plans import on as plan_on

    s = await plan_on(client, services=2)
    await plan_contact(s)
    await services_up_to(s, 2)
    mail.failing["email"] = PermanentDeliveryError("SMTP delivery failed: the server refused the recipient")
    t0 = dt.datetime.now(dt.UTC)
    assert (await run_outbox(s, now=t0)).failed == 0
    await run_outbox(s, now=t0 + dt.timedelta(hours=3))
    rows = [r for r in await log_rows(s) if r.event == plans_module.LIMIT_WARNING_EVENT]
    assert [(r.status, r.attempts) for r in rows] == [("refused", 1)]


async def test_a_refused_calendar_alert_is_logged_and_not_retried(seeded: Seed, google: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy import text

    from app.core.config import get_settings
    from tests import test_calendar as cal

    monkeypatch.setattr(get_settings(), "calendar_pause_after_failures", 1)
    await cal.set_contact(seeded)
    await cal.connect(seeded, google)
    await cal.book(seeded, await seeded.add_staff("Mei"), 10)
    google.fail.append(("/events", 400, None))
    mail.failing["email"] = PermanentDeliveryError("SMTP delivery failed: the server refused the recipient")
    await cal.tick(seeded)
    await cal.tick(seeded)
    await cal.tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(hours=3))
    async with tenant_session(seeded.tenant_id) as db:
        logged = (await db.execute(text("SELECT status, attempts FROM notification_log WHERE event = 'calendar.connection_lost'"))).all()
    assert [tuple(r) for r in logged] == [("refused", 1)]
