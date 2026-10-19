"""Booking reminders: due at the right moment, once, only for bookings that still stand, and never stale."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from app import worker
from app.core.config import get_settings
from app.core.database import tenant_session
from app.models import BookingReminder, NotificationLog
from app.models.events import OutboxEvent
from app.services import notifications as notes
from app.services import outbox, reminders
from app.services.notifications import NotificationPublisher
from app.services.sweep import TenantSweep, sweep_all, sweep_tenant
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_customer_cancel import PHONE, cancel, login, otp, set_default_policy  # noqa: F401
from tests.test_customer_identity import EMAIL, booked_with_email
from tests.test_customer_reschedule import move
from tests.test_notifications import log_rows, put_template, run_outbox
from tests.test_restaurant import book as rbook
from tests.test_restaurant import make_restaurant

H = dt.timedelta(hours=1)


@pytest.fixture
def notify(monkeypatch: pytest.MonkeyPatch, mail: Recorder) -> Recorder:
    """The worker's set-up: booking events become messages (recorded)."""
    monkeypatch.setattr(outbox, "_publisher", NotificationPublisher())
    return mail


async def tick(s: Seed, now: dt.datetime) -> TenantSweep:
    report = await sweep_all(tenant_ids=[s.tenant_id], now=now)
    assert report.ran and not report.errors, report
    return report.tenants[0]


async def reminder_rows(s: Seed, bid: str | None = None) -> list[tuple[str, str]]:
    async with tenant_session(s.tenant_id) as db:
        q = select(BookingReminder.booking_id, BookingReminder.kind).order_by(BookingReminder.kind)
        if bid:
            q = q.where(BookingReminder.booking_id == uuid.UUID(bid))
        return [(str(b), k) for b, k in (await db.execute(q)).all()]


def reminders_sent(rec: Recorder) -> list[Any]:
    return [m for m in rec.sent if (m.subject or "").startswith("Reminder")]


# --------------------------------------------------------------------- when
async def test_a_reminder_goes_out_once_its_moment_has_come_and_not_before(seeded: Seed, notify: Recorder) -> None:
    bid = await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    assert (await tick(seeded, start - 25 * H)).reminders_queued == 0
    assert reminders_sent(notify) == [] and await reminder_rows(seeded) == []
    first = await tick(seeded, start - 24 * H + dt.timedelta(minutes=5))
    assert first.reminders_queued == 1 and first.events_failed == 0
    (message,) = reminders_sent(notify)
    assert (message.channel, message.recipient) == ("email", EMAIL)
    assert message.subject == f"Reminder: your booking on {start.strftime('%a %d %b %Y')}"
    assert "Hi Aisha" in message.body and "Massage" in message.body and "10:00" in message.body
    assert await reminder_rows(seeded, bid) == [(bid, "24h")]
    rows = [r for r in await log_rows(seeded, bid) if r.event == "booking.reminder"]
    assert [(r.status, r.channel, r.recipient) for r in rows] == [("sent", "email", "a***@example.com")]


async def test_the_same_reminder_is_never_sent_twice(seeded: Seed, notify: Recorder) -> None:
    await booked_with_email(seeded, hour=10)
    due = at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5)
    for minutes in (0, 1, 2, 30):
        await tick(seeded, due + dt.timedelta(minutes=minutes))
    assert len(reminders_sent(notify)) == 1
    async with tenant_session(seeded.tenant_id) as db:  # even if the outbox "forgets" it published
        await db.execute(text("UPDATE outbox_events SET published_at = NULL"))
    await run_outbox(seeded)
    assert len(reminders_sent(notify)) == 1


async def test_the_two_hour_reminder_is_separate_from_the_day_before_one(seeded: Seed, notify: Recorder) -> None:
    bid = await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    await tick(seeded, start - 24 * H + dt.timedelta(minutes=1))
    await tick(seeded, start - 3 * H)
    assert len(reminders_sent(notify)) == 1
    await tick(seeded, start - 2 * H + dt.timedelta(minutes=1))
    assert len(reminders_sent(notify)) == 2
    assert await reminder_rows(seeded, bid) == [(bid, "24h"), (bid, "2h")]
    payloads = sorted(
        (e["hours_before"] for e in await _event_payloads(seeded, "booking.reminder")),
    )
    assert payloads == [2, 24]


async def _event_payloads(s: Seed, event_type: str) -> list[dict[str, Any]]:
    async with tenant_session(s.tenant_id) as db:
        return [e.payload for e in (await db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == event_type))).all()]


async def test_a_reminder_that_is_too_late_is_skipped_not_sent_stale(seeded: Seed, notify: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    late = start - 24 * H + dt.timedelta(minutes=61)  # the worker was down for an hour and a bit
    assert (await tick(seeded, late)).reminders_queued == 0
    assert reminders_sent(notify) == [] and await reminder_rows(seeded) == []
    just_in_time = start - 24 * H + dt.timedelta(minutes=59)
    assert (await tick(seeded, just_in_time)).reminders_queued == 1
    monkeypatch.setattr(get_settings(), "reminder_lateness_minutes", 5)  # the allowance is a setting
    bid2 = await booked_with_email(seeded, "+60122220001", "two@example.com", hour=12)
    start2 = at(seeded.day, 12)
    assert (await tick(seeded, start2 - 2 * H + dt.timedelta(minutes=6))).reminders_queued == 0
    assert await reminder_rows(seeded, bid2) == []
    assert (await tick(seeded, start2 - 2 * H + dt.timedelta(minutes=4))).reminders_queued == 1


async def test_a_booking_made_after_the_reminder_moment_gets_no_such_reminder(seeded: Seed, notify: Recorder) -> None:
    bid = await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    async with tenant_session(seeded.tenant_id) as db:  # it was booked 20 hours ahead: "24 hours before" never existed
        await db.execute(text("UPDATE bookings SET created_at = :c WHERE id = :i"), {"c": start - 20 * H, "i": uuid.UUID(bid)})
    assert (await tick(seeded, start - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 0
    assert (await tick(seeded, start - 23 * H)).reminders_queued == 0
    assert (await tick(seeded, start - 2 * H + dt.timedelta(minutes=5))).reminders_queued == 1  # but the 2-hour one still applies
    assert await reminder_rows(seeded, bid) == [(bid, "2h")]


@pytest.mark.parametrize("status", ["pending", "cancelled", "rejected", "in-progress", "waiting-for-parts", "completed", "no-show"])
async def test_only_confirmed_bookings_are_reminded(seeded: Seed, notify: Recorder, status: str) -> None:
    bid = await booked_with_email(seeded, hour=10)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE bookings SET status = :s WHERE id = :i"), {"s": status, "i": uuid.UUID(bid)})
    assert (await tick(seeded, at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 0
    assert reminders_sent(notify) == []


async def test_a_pending_booking_is_reminded_once_it_is_approved_in_time(client: httpx.AsyncClient, notify: Recorder) -> None:
    s = await make_seed(client, default_status="pending")
    await set_default_policy(s, default_status="pending", pending_timeout=10 * 86400)  # the request waits long enough to be answered "later"
    bid = await booked_with_email(s, hour=10)
    start = at(s.day, 10)
    assert (await tick(s, start - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 0
    assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "confirmed"}, headers=s.admin)).status_code == 200
    # the 24-hour moment has passed by now (the approval came after the allowance); the 2-hour one has not
    assert (await tick(s, start - 24 * H + dt.timedelta(hours=2))).reminders_queued == 0
    assert (await tick(s, start - 2 * H + dt.timedelta(minutes=1))).reminders_queued == 1


async def test_nothing_is_sent_for_an_appointment_that_has_started(seeded: Seed, notify: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    await booked_with_email(seeded, hour=10)
    monkeypatch.setattr(get_settings(), "reminder_hours", [0])  # a "reminder" at the very start...
    start = at(seeded.day, 10)
    assert (await tick(seeded, start + dt.timedelta(minutes=1))).reminders_queued == 0  # ...is not sent once it has begun
    assert (await tick(seeded, start)).reminders_queued == 0  # (a booking is "already under way" at its start instant)


# --------------------------------------------------------------------- settings
async def test_reminders_can_be_switched_off_or_set_to_other_lead_times(seeded: Seed, notify: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    bid = await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    monkeypatch.setattr(get_settings(), "reminder_hours", [])
    assert (await tick(seeded, start - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 0
    monkeypatch.setattr(get_settings(), "reminder_hours", [48, 48, 1])  # duplicates collapse
    assert (await tick(seeded, start - 48 * H + dt.timedelta(minutes=5))).reminders_queued == 1
    assert (await tick(seeded, start - 1 * H + dt.timedelta(minutes=5))).reminders_queued == 1
    assert await reminder_rows(seeded, bid) == [(bid, "1h"), (bid, "48h")]
    assert sorted(e["hours_before"] for e in await _event_payloads(seeded, "booking.reminder")) == [1, 48]


async def test_a_backlog_is_worked_off_over_several_ticks(seeded: Seed, notify: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reminders, "BATCH", 2)
    for n in range(5):
        await booked_with_email(seeded, f"+6011111{n:04d}", f"c{n}@example.com", hour=10)
    now = at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5)
    assert [(await tick(seeded, now)).reminders_queued for _ in range(4)] == [2, 2, 1, 0]
    assert len(reminders_sent(notify)) == 5


# --------------------------------------------------------------------- the wording
async def test_reminder_wording_and_channels_follow_the_templates(seeded: Seed, notify: Recorder) -> None:
    await put_template(seeded, "booking.reminder", "sms", body="$business_name: see you in $hours_before hours, $customer_name")
    await put_template(seeded, "booking.reminder", "email", subject="Soon, $customer_name", body="In $hours_before h: $services at $time")
    await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    await tick(seeded, start - 24 * H + dt.timedelta(minutes=5))
    await tick(seeded, start - 2 * H + dt.timedelta(minutes=5))
    by = {(m.channel, m.body.split()[0]): m for m in notify.sent}
    ours = [m for m in notify.to("email") if m.body.startswith("In ")]
    assert [m.body for m in ours] == ["In 24 h: Massage at 10:00", "In 2 h: Massage at 10:00"]
    assert ours[0].subject == "Soon, Aisha" and by
    assert [m.body.split(": ", 1)[1] for m in notify.to("sms")] == ["see you in 24 hours, Aisha", "see you in 2 hours, Aisha"]
    assert (await put_template(seeded, "booking.reminder", "email", subject="x", body="y", active=False)).status_code == 200


async def test_switching_the_reminder_email_off_sends_nothing_but_still_counts_it_done(seeded: Seed, notify: Recorder) -> None:
    assert (await put_template(seeded, "booking.reminder", "email", subject="x", body="y", active=False)).status_code == 200
    bid = await booked_with_email(seeded, hour=10)
    await tick(seeded, at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5))
    assert reminders_sent(notify) == [] and await reminder_rows(seeded, bid) == [(bid, "24h")]


async def test_the_reminder_template_is_in_the_admin_lists(seeded: Seed) -> None:
    listing = (await seeded.client.get("/v1/admin/notification-templates", headers=seeded.admin)).json()
    (default,) = [t for t in listing if t["event"] == "booking.reminder"]
    assert default["channel"] == "email" and default["source"] == "default" and "$hours_before" not in default["body"]
    placeholders = (await seeded.client.get("/v1/admin/notification-placeholders", headers=seeded.admin)).json()
    assert "hours_before" in placeholders
    bad = await put_template(seeded, "booking.reminder", "sms", body="$nonsense")
    assert bad.status_code == 422


# --------------------------------------------------------------------- the world changes
async def test_a_booking_moved_after_a_reminder_was_queued_is_not_reminded_about_the_old_time(seeded: Seed, notify: Recorder, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
        at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": PHONE, "email": EMAIL}))
    bid = r.json()["id"]
    start = at(seeded.day, 10)
    async with tenant_session(seeded.tenant_id) as db:  # queued, but not yet delivered
        assert await reminders.enqueue_due(db, seeded.tenant_id, start - 24 * H + dt.timedelta(minutes=5)) == 1
    assert (await move(seeded, await login(seeded), bid, at(seeded.day + dt.timedelta(days=1), 15))).status_code == 200
    await run_outbox(seeded)
    assert reminders_sent(notify) == []  # not told about a time that no longer applies
    skipped = [r for r in await log_rows(seeded, bid) if r.event == "booking.reminder"]
    assert [(r.status, r.error) for r in skipped] == [("skipped", "booking was moved after the reminder was scheduled")]
    assert await reminder_rows(seeded, bid) == []  # and the new time starts afresh
    new_start = at(seeded.day + dt.timedelta(days=1), 15)
    assert (await tick(seeded, new_start - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 1
    (message,) = reminders_sent(notify)
    assert "15:00" in message.body and new_start.strftime("%a %d %b %Y") in message.subject


async def test_a_booking_cancelled_after_a_reminder_was_queued_is_not_reminded(seeded: Seed, notify: Recorder, otp: Any) -> None:
    bid = await booked_with_email(seeded, PHONE, EMAIL, hour=10)
    async with tenant_session(seeded.tenant_id) as db:
        assert await reminders.enqueue_due(db, seeded.tenant_id, at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5)) == 1
    assert (await cancel(seeded, await login(seeded), bid)).status_code == 200
    await run_outbox(seeded)
    assert reminders_sent(notify) == []
    assert [(r.status, r.error) for r in await log_rows(seeded, bid) if r.event == "booking.reminder"] == [("skipped", "booking is cancelled")]


async def test_an_appointment_that_began_before_delivery_is_not_reminded(seeded: Seed, notify: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    bid = await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    async with tenant_session(seeded.tenant_id) as db:
        assert await reminders.enqueue_due(db, seeded.tenant_id, start - 2 * H + dt.timedelta(minutes=5)) == 1  # the 2 h one (the day-before one is long past)
    monkeypatch.setattr(notes, "_utcnow", lambda: start + H)  # delivery happens an hour into the appointment
    await run_outbox(seeded, now=start + H)
    assert reminders_sent(notify) == []
    assert any(r.error == "appointment already started" for r in await log_rows(seeded, bid))


# --------------------------------------------------------------------- safety
async def test_an_existing_reminder_record_blocks_a_second_one(seeded: Seed, notify: Recorder) -> None:
    bid = await booked_with_email(seeded, hour=10)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("INSERT INTO booking_reminders (tenant_id, booking_id, kind) VALUES (:t, :b, '24h')"), {"t": seeded.tenant_id, "b": uuid.UUID(bid)})
    assert (await tick(seeded, at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 0
    assert reminders_sent(notify) == []


async def test_two_sweeps_at_once_queue_one_reminder(seeded: Seed, notify: Recorder) -> None:
    await booked_with_email(seeded, hour=10)
    now = at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5)
    first, second = await asyncio.gather(sweep_tenant(seeded.tenant_id, now=now), sweep_tenant(seeded.tenant_id, now=now))
    assert first.reminders_queued + second.reminders_queued == 1
    await run_outbox(seeded)  # (the sweep that queued it may have dispatched before it committed: the next pass delivers)
    assert len(reminders_sent(notify)) == 1


async def test_reminders_belong_to_their_own_business(client: httpx.AsyncClient, notify: Recorder) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await booked_with_email(a, "+60111110001", "a@example.com", hour=10)
    await booked_with_email(b, "+60111110002", "b@example.com", hour=10)
    now = at(a.day, 10) - 24 * H + dt.timedelta(minutes=5)
    report = await sweep_all(tenant_ids=[a.tenant_id], now=now)
    assert [t.reminders_queued for t in report.tenants] == [1]
    assert [m.recipient for m in reminders_sent(notify)] == ["a@example.com"]  # B was not swept, so B's customer was not reminded
    assert await reminder_rows(b) == []
    async with tenant_session(a.tenant_id) as db:
        assert await db.scalar(select(func.count()).select_from(BookingReminder)) == 1  # and A cannot see B's rows either way


async def test_a_restaurant_booking_is_reminded_too(client: httpx.AsyncClient, notify: Recorder) -> None:
    rest = await make_restaurant(client)
    r = await rbook(rest, at(rest.day, 19), 3, customer={"name": "Aisha", "phone": PHONE, "email": EMAIL})
    assert r.status_code == 201, r.text
    assert (await tick(rest, at(rest.day, 19) - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 1
    (message,) = reminders_sent(notify)
    assert "19:00" in message.body


async def test_the_worker_reports_how_many_reminders_it_queued() -> None:
    from app.services.sweep import SweepReport

    report = SweepReport(ran=True, tenants=[TenantSweep(uuid.uuid4(), reminders_queued=2), TenantSweep(uuid.uuid4(), reminders_queued=3)])
    assert "5 reminders queued" in worker.summarise(report)


async def test_queueing_the_same_reminder_twice_queues_it_once(seeded: Seed, notify: Recorder) -> None:
    """The last line of defence: even if two sweeps picked the same booking, the primary key lets only one through."""
    await booked_with_email(seeded, hour=10)
    async with tenant_session(seeded.tenant_id) as db:
        from app.models import Booking

        booking = await db.scalar(select(Booking))
        assert booking is not None
        assert [await reminders.queue_one(db, seeded.tenant_id, booking, 24) for _ in range(3)] == [1, 0, 0]
        assert await reminders.queue_one(db, seeded.tenant_id, booking, 2) == 1  # another lead time is another reminder
    assert len(await _event_payloads(seeded, "booking.reminder")) == 2


async def test_moving_one_booking_leaves_the_others_reminders_alone(seeded: Seed, notify: Recorder, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    ids = []
    for hour, phone, email in ((10, PHONE, EMAIL), (12, "+60122220001", "two@example.com")):
        r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
            at(seeded.day, hour), mei, customer={"name": "Aisha", "phone": phone, "email": email}))
        ids.append(r.json()["id"])
    await tick(seeded, at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5))
    async with tenant_session(seeded.tenant_id) as db:
        await reminders.enqueue_due(db, seeded.tenant_id, at(seeded.day, 12) - 24 * H + dt.timedelta(minutes=5))
    assert len(await reminder_rows(seeded)) == 2
    assert (await move(seeded, await login(seeded), ids[0], at(seeded.day, 15))).status_code == 200
    assert await reminder_rows(seeded) == [(ids[1], "24h")]
