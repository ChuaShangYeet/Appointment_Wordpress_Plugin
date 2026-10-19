"""Alerting the stylist when an add-on or an overrun runs into their next appointment (blueprint §9.3, §11.3): the event, the message, the contact details."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.models import NotificationLog, OutboxEvent
from app.models.webhooks import WEBHOOK_EVENTS
from tests.conftest import Recorder, Seed, at
from tests.test_field_service import Practice, _actual, _in_progress, pbook, practice  # noqa: F401  (``practice`` is a fixture)
from tests.test_notifications import run_outbox
from tests.test_restaurant import book as rbook
from tests.test_restaurant import make_restaurant
from tests.test_salon import Salon, detail, salon, sbook, status, upsell  # noqa: F401  (``salon`` is a fixture)
from tests.test_webhooks import add_hook, net, tick, worker_publishers  # noqa: F401  (fixtures)

EVENT = "booking.schedule_conflict"


async def set_contact(s: Seed, resource_id: uuid.UUID, **fields: Any) -> httpx.Response:
    return await s.client.patch(f"/v1/admin/resources/{resource_id}", json=fields, headers=s.admin)


async def collide(salon: Salon, stylist: str = "Mei") -> tuple[str, str]:
    """A haircut (10:00-10:45) with a colour right behind it (10:45-12:15); a 30-minute add-on on the first runs into the second."""
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist=stylist)).json()["id"]
    nxt = (await sbook(salon, at(salon.day, 10, 45), ["color"], stylist=stylist, customer={"name": "Next Customer", "phone": "+60111222333"})).json()["id"]
    assert (await status(salon, first, "in-progress")).status_code == 200
    r = await upsell(salon, first, ["gel"])
    assert r.status_code == 200 and r.json()["extended"] is False and len(r.json()["conflicts"]) == 1
    return first, nxt


async def events_named(s: Seed, name: str = EVENT) -> list[OutboxEvent]:
    async with tenant_session(s.tenant_id) as db:
        return list((await db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == name).order_by(OutboxEvent.seq))).all())


async def log_rows(s: Seed) -> list[NotificationLog]:
    async with tenant_session(s.tenant_id) as db:
        return list((await db.scalars(select(NotificationLog).where(NotificationLog.event == EVENT).order_by(NotificationLog.channel))).all())


async def business(s: Seed) -> str:
    return str((await s.client.get("/v1/admin/business-profile", headers=s.admin)).json()["business_name"])


# --------------------------------------------------------------------- contact details on a staff member
async def test_a_manager_saves_contact_details_on_a_staff_member(salon: Salon) -> None:
    mei = salon.stylists["Mei"]
    r = await set_contact(salon, mei, contact_email="mei@example.com", contact_phone="012-345 0001")
    assert r.status_code == 200, r.text
    assert (r.json()["contact_email"], r.json()["contact_phone"]) == ("mei@example.com", "+60123450001")  # the phone written the one way every phone is
    listed = {x["name"]: x for x in (await salon.client.get("/v1/admin/resources", headers=await salon.token("manager"))).json()}
    assert (listed["Mei"]["contact_email"], listed["Mei"]["contact_phone"]) == ("mei@example.com", "+60123450001")
    assert listed["Lin"]["contact_email"] is None and listed["Lin"]["contact_phone"] is None
    changed = await set_contact(salon, mei, contact_phone="+65 9123 4567")
    assert changed.json()["contact_phone"] == "+6591234567" and changed.json()["contact_email"] == "mei@example.com"  # only what was sent changed
    gone = await set_contact(salon, mei, contact_email=None, contact_phone=None)
    assert gone.status_code == 200 and gone.json()["contact_email"] is None and gone.json()["contact_phone"] is None


async def test_colleagues_and_customers_never_see_contact_details(salon: Salon) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com", contact_phone="0123450001")
    staff = (await salon.client.get("/v1/admin/resources", headers=await salon.token("staff"))).json()
    assert all(x["contact_email"] is None and x["contact_phone"] is None for x in staff)  # a colleague is not told
    public = (await salon.client.get("/v1/public/resources", headers=salon.pk)).text
    assert "contact_email" not in public and "contact_phone" not in public and "mei@example.com" not in public and "123450001" not in public
    assert (await set_contact(salon, salon.stylists["Mei"], contact_email="x@example.com")).status_code == 200
    assert (await salon.client.patch(f"/v1/admin/resources/{salon.stylists['Mei']}", json={"contact_email": "x@example.com"}, headers=await salon.token("staff"))).status_code == 403


async def test_contact_details_must_be_valid_and_belong_to_staff(salon: Salon) -> None:
    mei = salon.stylists["Mei"]
    for bad in ({"contact_email": "not-an-email"}, {"contact_phone": "12"}, {"contact_phone": "x" * 33}, {"contact_phone": "abcdefgh"}, {"name": None}, {"contact_email": 5}):
        assert (await set_contact(salon, mei, **bad)).status_code == 422, bad
    created = await salon.client.post("/v1/admin/resources", json={"type": "staff", "name": "Ann", "contact_email": "ann@example.com", "contact_phone": "019-876 5432"}, headers=salon.admin)
    assert created.status_code == 201 and created.json()["contact_phone"] == "+60198765432" and created.json()["contact_email"] == "ann@example.com"
    for kind, body in (("table", {"capacity": 4}), ("bay", {})):
        refused = await salon.client.post("/v1/admin/resources", json={"type": kind, "name": "X", "contact_email": "x@example.com", **body}, headers=salon.admin)
        assert refused.status_code == 422, kind
    table = await salon.client.post("/v1/admin/resources", json={"type": "bay", "name": "Bay 1"}, headers=salon.admin)
    r = await set_contact(salon, uuid.UUID(table.json()["id"]), contact_phone="0123450001")
    assert r.status_code == 422 and "only be saved for staff" in r.json()["detail"]
    assert (await set_contact(salon, uuid.UUID(table.json()["id"]), contact_phone=None)).status_code == 200  # clearing is always fine
    assert (await set_contact(salon, uuid.uuid4(), contact_phone="0123450001")).status_code == 404


# --------------------------------------------------------------------- the event
async def test_a_collision_after_an_upsell_queues_the_event_with_ids_and_times_only(salon: Salon) -> None:
    first, nxt = await collide(salon)
    (event,) = await events_named(salon)
    assert event.aggregate_type == "booking" and str(event.aggregate_id) == first
    p = event.payload
    assert p["booking_id"] == first and p["conflicting_booking_ids"] == [nxt] and p["cause"] == "upsell"
    assert [dt.datetime.fromisoformat(p[k]) for k in ("start_at", "end_at", "wanted_end_at")] == [at(salon.day, 10), at(salon.day, 10, 45), at(salon.day, 11, 15)]
    assert set(p) == {"booking_id", "conflicting_booking_ids", "cause", "start_at", "end_at", "wanted_end_at"}  # ids and times: no names, no services


async def test_no_event_when_the_add_on_fits(salon: Salon) -> None:
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    await sbook(salon, at(salon.day, 11, 15), ["cut"], stylist="Mei")  # starts exactly where the add-on would end
    assert (await status(salon, first, "in-progress")).status_code == 200
    r = await upsell(salon, first, ["gel"])
    assert r.json()["extended"] is True and r.json()["conflicts"] == []
    assert await events_named(salon) == []
    assert (await upsell(salon, first, products=[("shampoo", 1)])).status_code == 200  # a product changes no time
    assert await events_named(salon) == []


async def test_each_add_on_that_collides_is_its_own_event(salon: Salon) -> None:
    first, _ = await collide(salon)
    assert (await upsell(salon, first, ["treat"])).json()["extended"] is False
    events = await events_named(salon)
    assert [e.payload["cause"] for e in events] == ["upsell", "upsell"] and events[0].payload["wanted_end_at"] != events[1].payload["wanted_end_at"]


async def test_an_overrun_into_the_next_job_queues_the_event(practice: Practice) -> None:
    first = await _in_progress(practice, "office", 10)
    nxt = (await pbook(practice, at(practice.day, 11), "office")).json()["id"]
    assert (await _actual(practice, first, 90)).json()["conflicting_booking_ids"] == [nxt]
    (event,) = await events_named(practice)
    assert event.payload["cause"] == "overrun" and event.payload["conflicting_booking_ids"] == [nxt]
    assert dt.datetime.fromisoformat(event.payload["wanted_end_at"]) == at(practice.day, 11, 30)
    free = await _in_progress(practice, "office", 15)
    assert (await _actual(practice, free, 90)).json()["conflicting_booking_ids"] == []
    assert len(await events_named(practice)) == 1  # a free overrun is not a collision


async def test_the_event_is_one_a_webhook_can_subscribe_to() -> None:
    assert EVENT in WEBHOOK_EVENTS


async def test_a_refused_upsell_leaves_no_event(salon: Salon) -> None:
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]  # not in progress yet
    assert (await upsell(salon, first, ["gel"])).status_code == 409
    assert await events_named(salon) == []


# --------------------------------------------------------------------- the message to the stylist
async def test_the_stylist_is_told_by_email_and_sms_with_the_next_appointments_details(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com", contact_phone="012-345 0001")
    await collide(salon)
    result = await run_outbox(salon)
    assert result.failed == 0
    by_channel = {m.channel: m for m in mail.sent}
    assert set(by_channel) == {"email", "sms"} and len(mail.sent) == 2
    assert by_channel["email"].recipient == "mei@example.com" and by_channel["sms"].recipient == "+60123450001"
    name = await business(salon)
    day = at(salon.day, 10).strftime("%a %d %b")
    assert by_channel["email"].subject == f"{name}: your next appointment is in the way" and by_channel["sms"].subject is None
    body = by_channel["email"].body
    assert body == by_channel["sms"].body
    assert body == (
        f"{name}: your {day} 10:00-10:45 appointment (Haircut, Gel finish) needs to go on until 11:15 because an extra service was added, "
        f"and that runs into your next appointment:\n- {day} 10:45-12:15: Colour for Next\n"
        "Nothing was moved. Please speak to your manager; both appointments are marked for review."
    )
    assert "Customer" not in body and "60111222333" not in body  # the next customer's first name only, never a surname or a number


async def test_the_log_shows_who_was_told_and_how_without_the_text(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com", contact_phone="0123450001")
    first, _ = await collide(salon)
    await run_outbox(salon)
    rows = await log_rows(salon)
    assert [(r.channel, r.status, r.recipient, str(r.booking_id), r.customer_id, r.attempts) for r in rows] == [
        ("email", "sent", "m***@example.com", first, None, 1), ("sms", "sent", "+60****0001", first, None, 1)]
    listed = (await salon.client.get("/v1/admin/notifications", params={"event": EVENT}, headers=await salon.token("manager"))).json()
    assert [(x["channel"], x["status"]) for x in listed] == [("email", "sent"), ("sms", "sent")] or {x["channel"] for x in listed} == {"email", "sms"}
    assert not any("Next" in str(x) or "Colour" in str(x) for x in listed)


async def test_an_overrun_is_worded_as_one(practice: Practice, mail: Recorder) -> None:
    pro = practice.pros["Dr Lim"]
    await set_contact(practice, pro, contact_email="lim@example.com")
    first = await _in_progress(practice, "office", 10)
    await pbook(practice, at(practice.day, 11), "office", customer={"name": "Aisha Karim", "phone": "+60123450009"})
    await _actual(practice, first, 90)
    await run_outbox(practice)
    (message,) = mail.sent
    assert message.channel == "email" and message.recipient == "lim@example.com"
    assert "needs to go on until 11:30 because the visit is running over," in message.body and "Office consult for Aisha" in message.body and "Karim" not in message.body


async def test_only_one_channel_is_used_when_only_one_contact_is_saved(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_phone="0123450001")
    await collide(salon)
    await run_outbox(salon)
    assert [(m.channel, m.recipient) for m in mail.sent] == [("sms", "+60123450001")]
    assert [(r.channel, r.status) for r in await log_rows(salon)] == [("sms", "sent")]  # no "skipped" noise for the channel that was never wanted


async def test_a_stylist_with_no_contact_details_is_logged_as_not_told(salon: Salon, mail: Recorder) -> None:
    await collide(salon)
    await run_outbox(salon)
    assert mail.sent == []
    ((row,),) = [[r] for r in await log_rows(salon)]
    assert (row.channel, row.status, row.error, row.recipient) == ("email", "skipped", "no contact details saved for the staff member", None)


async def test_only_the_stylist_of_the_running_appointment_is_told(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com")
    await set_contact(salon, salon.stylists["Lin"], contact_email="lin@example.com")
    await collide(salon, stylist="Lin")
    await run_outbox(salon)
    assert [m.recipient for m in mail.sent] == ["lin@example.com"]


async def test_the_next_customer_gets_nothing(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com")
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei", customer={"name": "A B", "phone": "+60111000001", "email": "a@example.com"})).json()["id"]
    await sbook(salon, at(salon.day, 10, 45), ["color"], stylist="Mei", customer={"name": "Next Customer", "phone": "+60111222333", "email": "next@example.com"})
    await status(salon, first, "in-progress")
    await upsell(salon, first, ["gel"])
    mail.sent.clear()
    await run_outbox(salon)
    assert [m.recipient for m in mail.sent if m.subject and "in the way" in m.subject] == ["mei@example.com"]
    assert not any(m.recipient in ("a@example.com", "next@example.com") and "in the way" in (m.subject or "") for m in mail.sent)


async def test_nothing_is_sent_once_a_manager_has_cleared_the_flag(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com")
    first, _ = await collide(salon)
    assert (await salon.client.post(f"/v1/admin/bookings/{first}/resolve-conflict", headers=salon.admin)).status_code == 200
    mail.sent.clear()
    await run_outbox(salon)
    assert not [m for m in mail.sent if "in the way" in (m.subject or "")]
    ((row,),) = [[r] for r in await log_rows(salon)]
    assert (row.status, row.error) == ("skipped", "the conflict was already resolved")


async def test_a_redelivered_event_tells_nobody_twice_and_a_failing_channel_is_retried_alone(salon: Salon, mail: Recorder) -> None:
    from app.services.notifications import NotificationFailed

    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com", contact_phone="0123450001")
    await collide(salon)
    mail.sent.clear()
    mail.failing["sms"] = RuntimeError("sms gateway down")
    first_pass = await run_outbox(salon)
    assert first_pass.failed == 1
    assert [m.channel for m in mail.sent if "in the way" in (m.subject or "")] == ["email"]  # the email went; the text did not
    assert {(r.channel, r.status) for r in await log_rows(salon)} == {("email", "sent"), ("sms", "failed")}
    sms_row = next(r for r in await log_rows(salon) if r.channel == "sms")
    assert "sms gateway down" in (sms_row.error or "")
    mail.failing.clear()
    mail.sent.clear()
    second_pass = await run_outbox(salon, now=dt.datetime.now(dt.UTC) + dt.timedelta(hours=2))  # after the retry back-off
    assert second_pass.failed == 0
    assert [m.channel for m in mail.sent] == ["sms"]  # only the one that had not gone
    assert {(r.channel, r.status, r.attempts) for r in await log_rows(salon)} == {("email", "sent", 1), ("sms", "sent", 2)}
    assert NotificationFailed  # (the failure that made the outbox retry)


async def test_a_stylist_alert_is_not_a_customer_message_and_has_no_template(salon: Salon) -> None:
    from app.models.notifications import NOTIFIABLE_EVENTS

    assert EVENT not in NOTIFIABLE_EVENTS
    r = await salon.client.put(f"/v1/admin/notification-templates/{EVENT}/email", json={"subject": "x", "body": "y"}, headers=salon.admin)
    assert r.status_code == 422  # the wording is built in


# --------------------------------------------------------------------- webhooks
async def test_a_subscribed_receiver_is_told_through_a_signed_webhook(salon: Salon, net: Any, worker_publishers: Recorder) -> None:
    await add_hook(salon, events=[EVENT])
    first, nxt = await collide(salon)
    await tick(salon)
    (body,) = net.bodies()
    assert body["type"] == EVENT and body["data"]["booking_id"] == first and body["data"]["conflicting_booking_ids"] == [nxt]
    assert body["data"]["cause"] == "upsell" and "customer" not in str(body).lower() and "Colour" not in str(body)
    listed = (await salon.client.get("/v1/admin/webhook-events", headers=salon.admin)).json()
    assert EVENT in listed


async def test_other_subscriptions_are_not_told_unless_they_asked_for_everything(salon: Salon, net: Any, worker_publishers: Recorder) -> None:
    await add_hook(salon, url="https://hooks.example.com/a", events=["booking.created"])
    await add_hook(salon, url="https://hooks.example.com/b", events=["*"])
    await collide(salon)
    await tick(salon)
    by_path: dict[str, list[str]] = {}
    for request in net.requests:  # (the connection goes to the checked address, so the receiver is told apart by its path)
        by_path.setdefault(request.url.path, []).append(request.headers["x-webhook-event"])
    assert set(by_path["/a"]) == {"booking.created"}  # it only asked for new bookings
    assert EVENT in by_path["/b"]


async def test_every_appointment_in_the_way_is_listed_in_time_order(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com")
    r = await salon.client.post("/v1/admin/services", json={"name": "Trim", "duration_minutes": 10, "buffer_minutes": 0}, headers=salon.admin)
    trim = r.json()["id"]
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]  # 10:00-10:45
    for hour, minute, who in ((11, 0, "Bea Later"), (10, 45, "Abe Earlier")):  # booked in the wrong order on purpose
        body = {"service_ids": [trim], "start_at": at(salon.day, hour, minute).isoformat(), "resource_id": str(salon.stylists["Mei"]),
                "customer": {"name": who, "phone": f"+6012{uuid.uuid4().int % 10**7:07d}"}}
        assert (await salon.client.post("/v1/public/bookings", json=body, headers=salon.pk)).status_code == 201
    assert (await status(salon, first, "in-progress")).status_code == 200
    assert len((await upsell(salon, first, ["gel"])).json()["conflicts"]) == 2  # both 10-minute bookings sit inside the 30 minutes the add-on needs
    mail.sent.clear()
    await run_outbox(salon)
    (message,) = [m for m in mail.sent if "in the way" in (m.subject or "")]
    lines = [line for line in message.body.splitlines() if line.startswith("- ")]
    assert [line.split(": ", 1)[1] for line in lines] == ["Trim for Abe", "Trim for Bea"]
    assert lines[0].startswith(f"- {at(salon.day, 10, 45).strftime('%a %d %b')} 10:45-10:55") and lines[1].startswith(f"- {at(salon.day, 11).strftime('%a %d %b')} 11:00-11:10")


async def test_a_collision_on_a_booking_with_no_staff_member_is_logged_as_such(client: httpx.AsyncClient, mail: Recorder) -> None:
    from app.services import events

    rest = await make_restaurant(client)
    bid = (await rbook(rest, at(rest.day, 19), 2, customer={"name": "Aisha", "phone": "+60123450001"})).json()["id"]  # a table, no staff
    async with tenant_session(rest.tenant_id) as db:
        from app.models import Booking

        booking = await db.get(Booking, uuid.UUID(bid))
        assert booking is not None
        booking.schedule_conflict = True
        events.record_schedule_conflict(db, booking, [], booking.end_at, "upsell")
    await run_outbox(rest)
    assert mail.sent == []
    ((row,),) = [[r] for r in await log_rows(rest)]
    assert (row.status, row.error) == ("skipped", "no staff member on the booking")


async def test_staff_are_created_with_valid_contact_details_only(salon: Salon) -> None:
    for bad in ({"contact_email": "not-an-email"}, {"contact_phone": "12"}, {"contact_phone": "x" * 33}):
        r = await salon.client.post("/v1/admin/resources", json={"type": "staff", "name": "Ann", **bad}, headers=salon.admin)
        assert r.status_code == 422, bad
