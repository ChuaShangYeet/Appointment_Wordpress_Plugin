"""Two messages racing for the last place in a month's WhatsApp / SMS allowance: only one may go out.

The count is the business's ``notification_log`` rows with a ``sent_at`` this month. Before this was fixed, a sender counted, then sent, and only then logged, so
two senders could both count "19 of 20", both send, and both log: 21 of 20. Now a sign-in code is logged as ``sending`` (and counted) *before* it is handed to the
gateway and settled afterwards, and every sender takes a per-business lock around its count; the worker keeps the lock until its own log row is committed.
"""
from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models import NotificationLog, Tenant
from app.services import notifications, plans
from tests.conftest import Recorder, Seed
from tests.test_customer_cancel import _Together, otp, request_code  # noqa: F401  (``otp`` is a fixture)
from tests.test_notifications import log_rows, put_template, run_outbox
from tests.test_plans import book_for_messages, on, refused, usage_of


@pytest.fixture
def sms_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_channel", "sms")


def sms_rows(rows: list[NotificationLog]) -> dict[str, NotificationLog]:
    return {r.event: r for r in rows if r.channel == "sms"}


async def codes_logged(s: Seed) -> list[NotificationLog]:
    return [r for r in await log_rows(s) if r.event == "auth.otp"]


# --------------------------------------------------------------------- the races
async def test_two_sign_in_codes_for_the_last_text_cannot_both_go_out(client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await on(client, sms_per_month=1)
    together = _Together(2)
    with monkeypatch.context() as m:
        m.setattr(plans, "message_allowed", together.wrap(plans.message_allowed))  # both arrive before either has counted
        results = await asyncio.gather(request_code(s, "012-345 0001"), request_code(s, "012-345 0002"))
    assert sorted(r.status_code for r in results) == [202, 402], [r.text for r in results]
    assert len(mail.to("sms")) == 1
    assert (await usage_of(s))["usage"]["sms_per_month"] == 1
    assert [r.status for r in await codes_logged(s)] == ["sent"]


async def test_several_sign_in_codes_at_once_never_pass_the_allowance(client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await on(client, sms_per_month=3)
    together = _Together(8)
    with monkeypatch.context() as m:
        m.setattr(plans, "message_allowed", together.wrap(plans.message_allowed))
        results = await asyncio.gather(*(request_code(s, f"012-345 00{i:02d}") for i in range(8)))
    assert sorted(r.status_code for r in results) == [202] * 3 + [402] * 5, [r.text for r in results]
    assert len(mail.to("sms")) == 3 and (await usage_of(s))["usage"]["sms_per_month"] == 3


async def test_a_text_the_worker_is_sending_is_counted_before_a_sign_in_code_can_take_the_last_one(
    client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await on(client, sms_per_month=1)
    assert (await put_template(s, "booking.confirmed", "sms", body="Booked $time")).status_code == 200
    await book_for_messages(s, 10)
    real_deliver = notifications.deliver
    racers: list[asyncio.Task[httpx.Response]] = []

    async def deliver_while_a_code_is_requested(message: Any) -> Any:
        if message.channel == "sms" and not racers:
            racers.append(asyncio.create_task(request_code(s)))  # a customer asks for a code while the booking's text is on its way
            await asyncio.sleep(0.5)
        return await real_deliver(message)

    with monkeypatch.context() as m:
        m.setattr(notifications, "deliver", deliver_while_a_code_is_requested)
        await run_outbox(s)
        code = await asyncio.wait_for(racers[0], timeout=20)
    refused(code, "max_sms_per_month", 1, 1)
    assert len(mail.to("sms")) == 1 and mail.to("sms")[0].body.startswith("Booked")  # the booking's text went out, the code did not
    assert (await usage_of(s))["usage"]["sms_per_month"] == 1


async def test_a_sign_in_code_that_is_on_its_way_is_counted_before_the_worker_sends_the_last_text(
    client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await on(client, sms_per_month=1)
    assert (await put_template(s, "booking.confirmed", "sms", body="Booked $time")).status_code == 200
    bid = await book_for_messages(s, 10)
    real_deliver = notifications.deliver
    passes: list[Any] = []

    async def deliver_while_the_worker_runs(message: Any) -> Any:
        if "sign-in code" in message.body and not passes:
            (inflight,) = await codes_logged(s)  # the code is already in the log and already counted, though it has not been handed over yet
            assert (inflight.status, inflight.attempts) == ("sending", 1) and inflight.sent_at is not None and inflight.provider is None
            passes.append(await run_outbox(s))  # the worker's pass while the code is still with the gateway
        return await real_deliver(message)

    with monkeypatch.context() as m:
        m.setattr(notifications, "deliver", deliver_while_the_worker_runs)
        code = await request_code(s)
    assert code.status_code == 202, code.text
    assert len(mail.to("sms")) == 1 and "sign-in code" in mail.to("sms")[0].body  # the code, and no booking text
    (booking_text,) = [r for r in await log_rows(s, bid) if r.channel == "sms"]
    assert booking_text.status == "skipped" and "plan limit" in (booking_text.error or "")
    assert (await usage_of(s))["usage"]["sms_per_month"] == 1


async def test_the_message_lock_is_held_until_the_senders_log_row_is_committed(client: httpx.AsyncClient) -> None:
    s = await on(client, sms_per_month=2)
    async with tenant_session(s.tenant_id) as one:
        tenant = await one.get(Tenant, s.tenant_id)
        assert tenant is not None
        assert await plans.message_allowed(one, tenant, "sms")
        async with tenant_session(s.tenant_id) as two:  # another sender, meanwhile
            free = await two.scalar(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"messages:{s.tenant_id}"})
        assert free is False
    async with tenant_session(s.tenant_id) as three:
        assert await three.scalar(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"messages:{s.tenant_id}"}) is True  # released when the first finished


async def test_a_message_with_no_limit_and_email_take_no_lock(client: httpx.AsyncClient) -> None:
    s = await on(client, sms_per_month=None, whatsapp_per_month=1)
    async with tenant_session(s.tenant_id) as one:
        tenant = await one.get(Tenant, s.tenant_id)
        assert tenant is not None
        assert await plans.message_allowed(one, tenant, "sms") and await plans.message_allowed(one, tenant, "email")
        async with tenant_session(s.tenant_id) as two:
            assert await two.scalar(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"messages:{s.tenant_id}"}) is True


# --------------------------------------------------------------------- the sign-in code's own log row
async def test_a_code_is_logged_as_sent_with_the_time_it_was_counted(client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None) -> None:
    s = await on(client, sms_per_month=5)
    assert (await request_code(s, "012-345 0001")).status_code == 202
    (row,) = await codes_logged(s)
    assert (row.status, row.channel, row.error, row.attempts) == ("sent", "sms", None, 1)
    assert row.sent_at is not None and row.provider == "recorder" and row.provider_message_id == "msg-1" and row.recipient == "+60****0001"
    listed = (await s.client.get("/v1/admin/notifications?status=sent&event=auth.otp", headers=s.admin)).json()
    assert [x["id"] for x in listed] == [str(row.id)]
    assert (await s.client.get("/v1/admin/notifications?status=sending", headers=s.admin)).json() == []


async def test_a_code_that_could_not_be_sent_gives_its_place_back(client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None) -> None:
    s = await on(client, sms_per_month=1)
    mail.failing["sms"] = RuntimeError("gateway down")
    failed = await request_code(s, "012-345 0001")
    assert failed.status_code == 503, failed.text
    (row,) = await codes_logged(s)
    assert row.status == "failed" and row.sent_at is None and "gateway down" in (row.error or "")
    assert (await usage_of(s))["usage"]["sms_per_month"] == 0  # nothing went out, nothing is counted
    mail.failing.clear()
    assert (await request_code(s, "012-345 0002")).status_code == 202  # the place was free again
    assert sorted(r.status for r in await codes_logged(s)) == ["failed", "sent"]
    assert (await usage_of(s))["usage"]["sms_per_month"] == 1


async def test_a_code_over_the_allowance_is_refused_and_leaves_no_trace_in_the_log(client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None) -> None:
    s = await on(client, sms_per_month=1)
    assert (await request_code(s, "012-345 0001")).status_code == 202
    refused(await request_code(s, "012-345 0002"), "max_sms_per_month", 1, 1)
    assert [r.status for r in await codes_logged(s)] == ["sent"] and len(mail.to("sms")) == 1


async def test_a_code_whose_result_is_not_known_still_counts(client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None) -> None:
    """A worker killed while a code was with the gateway leaves a ``sending`` row: the message may have gone out, so it keeps its place."""
    s = await on(client, sms_per_month=1)
    async with tenant_session(s.tenant_id) as db:
        db.add(NotificationLog(tenant_id=s.tenant_id, event="auth.otp", channel="sms", status="sending", attempts=1, sent_at=dt.datetime.now(dt.UTC)))
    assert (await usage_of(s))["usage"]["sms_per_month"] == 1
    refused(await request_code(s, "012-345 0001"), "max_sms_per_month", 1, 1)
    listed = (await s.client.get("/v1/admin/notifications?status=sending", headers=s.admin)).json()
    assert [(x["status"], x["event"]) for x in listed] == [("sending", "auth.otp")]


async def test_a_paid_business_logs_its_codes_the_same_way(client: httpx.AsyncClient, otp: Any, mail: Recorder, sms_codes: None) -> None:
    from tests.test_country_setup import business

    s = await business(client)
    assert (await request_code(s)).status_code == 202
    (row,) = await codes_logged(s)
    assert row.status == "sent" and row.sent_at is not None and len(mail.to("sms")) == 1


async def test_the_magic_link_email_is_logged_as_before(client: httpx.AsyncClient, otp: Any, mail: Recorder) -> None:
    from tests.test_customer_identity import EMAIL, booked_with_email

    s = await on(client, sms_per_month=0, whatsapp_per_month=0)
    await booked_with_email(s)
    assert (await s.client.post("/v1/public/auth/magic-link", json={"email": EMAIL}, headers=s.pk)).status_code == 202
    rows = [r for r in await log_rows(s) if r.event == "auth.magic_link"]
    assert [(r.channel, r.status, r.attempts) for r in rows] == [("email", "sent", 1)] and rows[0].sent_at is not None


async def test_the_log_knows_five_statuses_and_the_database_refuses_any_other(client: httpx.AsyncClient) -> None:
    from app.models.notifications import LOG_STATUSES

    assert set(LOG_STATUSES) == {"sending", "sent", "failed", "skipped", "refused"}
    s = await on(client, sms_per_month=1)
    for status in LOG_STATUSES:
        async with tenant_session(s.tenant_id) as db:
            db.add(NotificationLog(tenant_id=s.tenant_id, event="x", channel="sms", status=status, attempts=0))
    with pytest.raises(IntegrityError):
        async with tenant_session(s.tenant_id) as db:
            db.add(NotificationLog(tenant_id=s.tenant_id, event="x", channel="sms", status="queued", attempts=0))
    async with tenant_session(s.tenant_id) as db:
        assert len((await db.scalars(select(NotificationLog))).all()) == 5


async def test_a_whatsapp_template_code_takes_its_place_first_and_settles_the_same_row(client: httpx.AsyncClient, otp: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_channel", "whatsapp")
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "sign_in_code")
    s = await on(client, whatsapp_per_month=1)
    assert (await request_code(s, "012-345 0001")).status_code == 202
    (sent,) = mail.to("whatsapp")
    assert sent.external_template == "sign_in_code"
    (row,) = await codes_logged(s)
    assert (row.channel, row.status, row.attempts) == ("whatsapp", "sent", 1) and row.sent_at is not None and row.provider_message_id == "msg-1"
    refused(await request_code(s, "012-345 0002"), "max_whatsapp_per_month", 1, 1)
    assert len(await codes_logged(s)) == 1


async def test_a_whatsapp_template_code_that_fails_gives_its_place_back(client: httpx.AsyncClient, otp: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_channel", "whatsapp")
    monkeypatch.setattr(get_settings(), "whatsapp_otp_template", "sign_in_code")
    s = await on(client, whatsapp_per_month=1)
    mail.failing["whatsapp"] = RuntimeError("template not approved")
    assert (await request_code(s, "012-345 0001")).status_code == 503
    (row,) = await codes_logged(s)
    assert row.status == "failed" and row.sent_at is None and (await usage_of(s))["usage"]["whatsapp_per_month"] == 0
