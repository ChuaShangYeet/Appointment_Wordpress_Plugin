"""Online consultations with the business's own meeting service (2026-10-19, migration 0032): a consultant saves their video room once, their online appointments
get it on confirmation, a link set by hand wins, and the client is told. Nothing here makes video rooms."""
from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from app.core.database import tenant_session
from app.models import Service
from app.services import outbox
from app.services.calendar import CalendarPublisher
from app.services.notifications import DEFAULTS, NotificationPublisher, render
from app.services.sweep import sweep_all
from tests.conftest import Recorder, at, make_seed
from tests.test_calendar import Google, connect, google  # noqa: F401  (fixture)
from tests.test_field_service import Practice, make_practice, pbody, status
from tests.test_ratelimit import redis  # noqa: F401  (fixture)

CLIENT = "client@example.com"
ROOM = "https://meet.google.com/abc-defg-hij"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch, mail: Recorder) -> Recorder:
    """Booking messages go out through the outbox, as the worker sets it up; the ``mail`` recorder takes them."""
    monkeypatch.setattr(outbox, "_publisher", outbox.CompositePublisher(NotificationPublisher(), CalendarPublisher()))
    return mail


async def practice_with_room(client: httpx.AsyncClient, link: str | None = ROOM, **kw: Any) -> Practice:
    p = await make_practice(client, **kw)
    if link is not None:
        r = await client.patch(f"/v1/admin/resources/{p.pros['Dr Lim']}", json={"meeting_link": link}, headers=p.admin)
        assert r.status_code == 200, r.text
    return p


async def consult(p: Practice, service: str = "video", hour: int = 10) -> dict[str, Any]:
    body = pbody(p, at(p.day, hour), service, resource_id=str(p.pros["Dr Lim"]))
    body["customer"]["email"] = CLIENT
    r = await p.client.post("/v1/public/bookings", json=body, headers=p.pk)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def tick(p: Any) -> None:
    report = await sweep_all(tenant_ids=[p.tenant_id])
    assert report.ran and not report.errors, report


def mails(rec: Recorder, subject: str) -> list[Any]:
    return [m for m in rec.sent if m.channel == "email" and m.subject == subject]


async def set_link(p: Practice, bid: str, url: str) -> httpx.Response:
    return await p.client.patch(f"/v1/admin/bookings/{bid}/meeting-link", json={"url": url}, headers=p.admin)


# --------------------------------------------------------------------- the consultant's saved room
async def test_an_online_consultation_gets_the_consultants_room_and_the_confirmation_carries_it(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client)
    booked = await consult(p)
    assert booked["status"] == "confirmed" and booked["meeting_link"] == ROOM
    await tick(p)
    await tick(p)
    (confirmed,) = mails(sent, "Your booking is confirmed")
    assert confirmed.recipient == CLIENT and f"Join the online meeting here: {ROOM}" in confirmed.body
    assert mails(sent, "Your online meeting link") == []  # the confirmation already said it
    seen = (await client.get(f"/v1/admin/bookings/{booked['id']}", headers=p.admin)).json()
    assert seen["meeting_link"] == ROOM


async def test_a_visit_in_person_gets_no_link(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client)
    booked = await consult(p, "office")
    assert booked["meeting_link"] is None
    await tick(p)
    (confirmed,) = mails(sent, "Your booking is confirmed")
    assert "Join" not in confirmed.body


async def test_without_a_saved_room_there_is_no_link_until_one_is_set(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client, link=None)
    booked = await consult(p)
    assert booked["meeting_link"] is None


async def test_a_booking_awaiting_an_answer_gets_the_room_when_confirmed(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client, default_status="pending")
    booked = await consult(p)
    assert booked["status"] == "pending" and booked["meeting_link"] is None
    approved = await status(p, booked["id"], "confirmed")
    assert approved.status_code == 200 and approved.json()["meeting_link"] == ROOM
    await tick(p)
    (confirmed,) = mails(sent, "Your booking is confirmed")
    assert ROOM in confirmed.body


async def test_a_consultation_handed_to_another_consultant_before_confirming_gets_the_new_ones_room(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client, pros=("Dr Lim", "Dr Tan"), default_status="pending")
    tan_room = "https://zoom.us/j/tan"
    assert (await client.patch(f"/v1/admin/resources/{p.pros['Dr Tan']}", json={"meeting_link": tan_room}, headers=p.admin)).status_code == 200
    booked = await consult(p)
    r = await client.post(f"/v1/admin/bookings/{booked['id']}/reschedule", json={"start_at": at(p.day, 11).isoformat(), "resource_id": str(p.pros["Dr Tan"])}, headers=p.admin)
    assert r.status_code == 200, r.text
    assert (await status(p, booked["id"], "confirmed")).json()["meeting_link"] == tan_room  # not Dr Lim's, who no longer has it


async def test_only_professional_businesses_hand_out_rooms(client: httpx.AsyncClient, sent: Recorder) -> None:
    spa = await make_seed(client)
    mei = await spa.add_staff("Mei")
    assert (await client.patch(f"/v1/admin/resources/{mei}", json={"meeting_link": ROOM}, headers=spa.admin)).status_code == 200
    async with tenant_session(spa.tenant_id) as db:  # (a spa cannot make a virtual service through the API)
        service = await db.get(Service, spa.service_id)
        assert service is not None
        service.location_type = "virtual"
    r = await client.post("/v1/public/bookings", headers=spa.pk, json=spa.booking_body(at(spa.day, 10), mei))
    assert r.status_code == 201 and r.json()["meeting_link"] is None


# --------------------------------------------------------------------- a link set by hand
async def test_a_link_set_by_hand_wins_and_is_sent_once(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client)
    booked = await consult(p)
    await tick(p)
    r = await set_link(p, booked["id"], "https://zoom.us/j/7")
    assert r.status_code == 200 and r.json()["meeting_link"] == "https://zoom.us/j/7"
    assert (await set_link(p, booked["id"], "https://zoom.us/j/7")).status_code == 200  # the same again: nothing new to tell
    await tick(p)
    (told,) = mails(sent, "Your online meeting link")
    assert told.recipient == CLIENT and "https://zoom.us/j/7" in told.body and "Video call" in told.body


async def test_a_link_set_while_pending_goes_out_with_the_confirmation(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client, default_status="pending")
    booked = await consult(p)
    assert (await set_link(p, booked["id"], "https://zoom.us/j/9")).status_code == 200
    assert (await status(p, booked["id"], "confirmed")).json()["meeting_link"] == "https://zoom.us/j/9"  # the consultant's saved room does not replace it
    await tick(p)
    (confirmed,) = mails(sent, "Your booking is confirmed")
    assert "Join the online meeting here: https://zoom.us/j/9" in confirmed.body and mails(sent, "Your online meeting link") == []


async def test_a_queued_link_is_not_sent_once_the_booking_is_cancelled(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client)
    booked = await consult(p)
    assert (await set_link(p, booked["id"], "https://zoom.us/j/5")).status_code == 200
    assert (await status(p, booked["id"], "cancelled")).status_code == 200
    await tick(p)
    assert mails(sent, "Your online meeting link") == []


async def test_a_moved_consultation_says_where_to_join(client: httpx.AsyncClient, sent: Recorder) -> None:
    p = await practice_with_room(client)
    booked = await consult(p)
    r = await client.post(f"/v1/admin/bookings/{booked['id']}/reschedule", json={"start_at": at(p.day, 15).isoformat()}, headers=p.admin)
    assert r.status_code == 200 and r.json()["meeting_link"] == ROOM
    await tick(p)
    (moved,) = mails(sent, "Your booking was moved")
    assert f"Join the online meeting here: {ROOM}" in moved.body


def test_the_reminder_carries_the_link_and_says_nothing_without_one() -> None:
    _, body = DEFAULTS["booking.reminder"]
    base = {"customer_name": "A", "services": "Video call", "date": "d", "time": "t", "business_name": "B"}
    assert "Join the online meeting here: X" in render(body, {**base, "meeting_line": " Join the online meeting here: X"})
    assert "Join" not in render(body, {**base, "meeting_line": ""})


# --------------------------------------------------------------------- saving the room
async def test_the_room_is_for_staff_and_must_be_a_web_link(client: httpx.AsyncClient) -> None:
    p = await make_practice(client)
    lim = p.pros["Dr Lim"]
    for bad in ("javascript:alert(1)", "meet.google.com/abc", "ftp://x.example/room"):
        assert (await client.patch(f"/v1/admin/resources/{lim}", json={"meeting_link": bad}, headers=p.admin)).status_code == 422, bad
    bay = await client.post("/v1/admin/resources", json={"type": "bay", "name": "Bay 1", "meeting_link": ROOM}, headers=p.admin)
    assert bay.status_code == 422
    bay = await client.post("/v1/admin/resources", json={"type": "bay", "name": "Bay 1"}, headers=p.admin)
    assert (await client.patch(f"/v1/admin/resources/{bay.json()['id']}", json={"meeting_link": ROOM}, headers=p.admin)).status_code == 422
    assert (await client.post("/v1/admin/resources", json={"type": "staff", "name": "Dr Tan", "meeting_link": "zoom.us/j/1"}, headers=p.admin)).status_code == 422
    made = await client.post("/v1/admin/resources", json={"type": "staff", "name": "Dr Tan", "meeting_link": ROOM}, headers=p.admin)
    assert made.status_code == 201 and made.json()["meeting_link"] == ROOM


async def test_the_room_shows_in_the_admin_not_on_the_public_page_and_can_be_removed(client: httpx.AsyncClient) -> None:
    p = await practice_with_room(client)
    lim = str(p.pros["Dr Lim"])
    listed = {r["id"]: r for r in (await client.get("/v1/admin/resources", headers=p.admin)).json()}
    assert listed[lim]["meeting_link"] == ROOM
    public = (await client.get("/v1/public/resources", headers=p.pk)).json()
    assert all("meeting_link" not in r for r in public)
    r = await client.patch(f"/v1/admin/resources/{lim}", json={"meeting_link": None}, headers=p.admin)
    assert r.status_code == 200 and r.json()["meeting_link"] is None
    assert (await consult(p))["meeting_link"] is None


# --------------------------------------------------------------------- the calendar shows it
async def test_the_calendar_event_shows_the_link_and_follows_a_change(client: httpx.AsyncClient, google: Google) -> None:  # noqa: F811
    p = await practice_with_room(client)
    google.calendars["lim@example.test"] = "Dr Lim"
    await connect(p, google, resource_id=str(p.pros["Dr Lim"]), calendar_id="lim@example.test")
    booked = await consult(p)
    await tick(p)
    (event,) = google.stored("lim@example.test").values()
    assert f"Join online: {ROOM}" in event["description"] and "conferenceData" not in event
    assert (await set_link(p, booked["id"], "https://zoom.us/j/3")).status_code == 200
    await tick(p)
    (event,) = google.stored("lim@example.test").values()
    assert "Join online: https://zoom.us/j/3" in event["description"]
    assert dt.datetime.fromisoformat(event["start"]["dateTime"].replace("Z", "+00:00")) == at(p.day, 10)
