"""Booking status history and the transactional outbox: written with every change, PII-free, and delivered at least once."""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select, text

from app.core.database import tenant_session
from app.models import Tenant
from app.models.events import BookingStatusHistory, OutboxEvent
from app.services import booking as booking_module
from app.services import outbox
from tests.conftest import Seed, at, make_seed
from tests.test_automotive import gbook, make_garage
from tests.test_field_service import add_service, make_practice, pbook
from tests.test_flagging import book, get, put_shifts
from tests.test_sweep import soon, sweep


async def history(s: Seed, bid: str) -> list[dict[str, Any]]:
    r = await s.client.get(f"/v1/admin/bookings/{bid}/history", headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def events(s: Seed, *, aggregate: str | None = None) -> list[OutboxEvent]:
    async with tenant_session(s.tenant_id) as db:
        q = select(OutboxEvent).order_by(OutboxEvent.seq)
        if aggregate:
            q = q.where(OutboxEvent.aggregate_id == uuid.UUID(aggregate))
        return list((await db.scalars(q)).all())


def moves(rows: list[dict[str, Any]]) -> list[tuple[str | None, str, str]]:
    return [(r["from_status"], r["to_status"], r["actor_type"]) for r in rows]


async def status(s: Seed, bid: str, new: str) -> httpx.Response:
    return await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": new}, headers=s.admin)


@pytest.fixture
def publisher() -> Iterator[Any]:
    class Recorder:
        def __init__(self) -> None:
            self.seen: list[str] = []
            self.fail_types: set[str] = set()

        async def publish(self, event: OutboxEvent) -> None:
            if event.event_type in self.fail_types:
                raise RuntimeError("broker unavailable")
            self.seen.append(event.event_type)

    rec = Recorder()
    outbox.set_event_publisher(rec)
    yield rec
    outbox.set_event_publisher(None)


# --------------------------------------------------------------------- history and events for each way a booking changes
async def test_a_booking_confirmed_on_creation_is_recorded_with_its_customer_as_the_actor(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    rows = await history(seeded, bid)
    assert moves(rows) == [(None, "confirmed", "customer")]
    assert rows[0]["actor_id"] == (await get(seeded, bid))["customer_id"]
    assert [e.event_type for e in await events(seeded, aggregate=bid)] == ["booking.created", "booking.confirmed"]


async def test_a_pending_booking_gets_created_now_and_confirmed_when_staff_approve_it(client: httpx.AsyncClient) -> None:
    s = await make_seed(client, default_status="pending")
    bid = await book(s, await s.add_staff("Mei"), at(s.day, 10))
    assert [e.event_type for e in await events(s, aggregate=bid)] == ["booking.created"]  # not confirmed yet
    assert (await status(s, bid, "confirmed")).status_code == 200
    rows = await history(s, bid)
    assert moves(rows) == [(None, "pending", "customer"), ("pending", "confirmed", "staff")]
    assert rows[1]["actor_id"] == "wp-1"  # the WordPress user id the admin token was issued for
    assert [e.event_type for e in await events(s, aggregate=bid)] == ["booking.created", "booking.confirmed"]
    assert [r["changed_at"] for r in rows] == sorted(r["changed_at"] for r in rows)


async def test_every_status_gets_its_own_event_name(client: httpx.AsyncClient) -> None:
    from tests.test_salon import make_salon, sbook

    salon = await make_salon(client)
    a = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    b = (await sbook(salon, at(salon.day, 12), ["cut"], stylist="Mei")).json()["id"]
    c = (await sbook(salon, at(salon.day, 15), ["cut"], stylist="Mei")).json()["id"]
    for new in ("in-progress", "completed"):
        assert (await status(salon, a, new)).status_code == 200
    assert (await status(salon, b, "no-show")).status_code == 200
    assert (await status(salon, c, "cancelled")).status_code == 200
    assert [e.event_type for e in await events(salon, aggregate=a)][2:] == ["booking.in_progress", "booking.completed"]
    assert [e.event_type for e in await events(salon, aggregate=b)][-1] == "booking.no_show"
    assert [e.event_type for e in await events(salon, aggregate=c)][-1] == "booking.cancelled"
    assert moves(await history(salon, a)) == [(None, "confirmed", "customer"), ("confirmed", "in-progress", "staff"), ("in-progress", "completed", "staff")]


async def test_a_customer_cancellation_is_attributed_to_the_customer(seeded: Seed) -> None:
    bid = await book(seeded, await seeded.add_staff("Mei"), at(seeded.day, 10))
    customer = uuid.UUID((await get(seeded, bid))["customer_id"])
    async with tenant_session(seeded.tenant_id) as db:
        tenant = await db.get(Tenant, seeded.tenant_id)
        assert tenant is not None
        await booking_module.cancel_as_customer(db, tenant, uuid.UUID(bid), customer)
    rows = await history(seeded, bid)
    assert moves(rows)[-1] == ("confirmed", "cancelled", "customer") and rows[-1]["actor_id"] == str(customer)


async def test_an_expired_hold_is_rejected_by_the_system_and_recorded(client: httpx.AsyncClient) -> None:
    s = await make_seed(client, default_status="pending")
    bid = await book(s, await s.add_staff("Mei"), at(s.day, 10))
    async with tenant_session(s.tenant_id) as db:
        await db.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute'"))
    await sweep(s)
    rows = await history(s, bid)
    assert moves(rows)[-1] == ("pending", "rejected", "system") and rows[-1]["actor_id"] is None
    assert [e.event_type for e in await events(s, aggregate=bid)][-1] == "booking.rejected"


async def test_resuming_a_job_and_booking_a_follow_up_are_recorded(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    bid = (await gbook(g, at(g.day, 10), ["brake"])).json()["id"]
    mechanic = next(r for r in (await get(g, bid))["resource_ids"] if uuid.UUID(r) in g.mechanics.values())
    assert (await status(g, bid, "in-progress")).status_code == 200 and (await status(g, bid, "waiting-for-parts")).status_code == 200
    r = await g.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": at(g.day, 15).isoformat(), "resource_id": mechanic}, headers=g.admin)
    assert r.status_code == 200, r.text
    rows = await history(g, bid)
    assert moves(rows)[-3:] == [("confirmed", "in-progress", "staff"), ("in-progress", "waiting-for-parts", "staff"), ("waiting-for-parts", "confirmed", "staff")]
    assert [e.event_type for e in await events(g, aggregate=bid)][-3:] == ["booking.in_progress", "booking.waiting_for_parts", "booking.confirmed"]

    p = await make_practice(client)
    inspection = (await pbook(p, at(p.day, 10), "office")).json()["id"]
    assert (await status(p, inspection, "in-progress")).status_code == 200
    standard = await add_service(p, "Follow-up visit", 60, category="standard", location="in_office")
    r = await p.client.post(f"/v1/admin/bookings/{inspection}/follow-up", json={"service_id": str(standard), "start_at": at(p.day, 15).isoformat()}, headers=p.admin)
    assert r.status_code == 201, r.text
    created = await history(p, r.json()["id"])
    assert moves(created) == [(None, "confirmed", "staff")] and created[0]["actor_id"] == "wp-1"


async def test_a_change_that_is_rolled_back_leaves_no_history_and_no_event(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    await book(seeded, mei, at(seeded.day, 10))
    refused = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert refused.status_code == 409  # lost the slot
    async with tenant_session(seeded.tenant_id) as db:
        assert await db.scalar(select(func.count()).select_from(BookingStatusHistory)) == 1
        assert await db.scalar(select(func.count()).select_from(OutboxEvent)) == 3  # the shift setup + created + confirmed of the one booking
    bid = str((await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)).json()[0]["id"])
    assert (await status(seeded, bid, "rejected")).status_code == 409  # not allowed from confirmed
    assert len(await history(seeded, bid)) == 1 and len(await events(seeded)) == 3


async def test_a_shift_change_publishes_shift_updated(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei", shifts=False)
    await put_shifts(seeded, mei, start="10:00:00")
    types = [(e.event_type, e.aggregate_type, e.payload) for e in await events(seeded)]
    assert types == [("shift.updated", "resource", {"resource_id": str(mei)})]


async def test_events_carry_ids_and_times_only_never_personal_data(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post(
        "/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
            at(seeded.day, 10), mei, health_notes="allergic to lavender", special_request="quiet room",
            customer={"name": "Aisha Binti Karim", "phone": "+60123456789", "email": "aisha@example.com"}),
    )
    assert r.status_code == 201, r.text
    booking_events = [e for e in await events(seeded) if e.aggregate_type == "booking"]
    everything = " ".join(str(e.payload) for e in await events(seeded)) + str(await history(seeded, r.json()["id"]))
    for secret in ("Aisha", "Karim", "+60123456789", "aisha@example.com", "lavender", "quiet room"):
        assert secret not in everything
    assert {frozenset(e.payload) for e in booking_events} == {frozenset({"booking_id", "status", "previous_status", "start_at", "end_at"})}


async def test_history_and_events_are_private_to_their_tenant(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    bid = await book(a, await a.add_staff("Mei"), at(a.day, 10))
    assert await events(b) == []
    async with tenant_session(b.tenant_id) as db:
        assert await db.scalar(select(func.count()).select_from(BookingStatusHistory)) == 0
    assert (await b.client.get(f"/v1/admin/bookings/{bid}/history", headers=b.admin)).status_code == 404


async def test_history_access_follows_the_bookings_own_rules(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    hers, his = await book(seeded, mei, at(seeded.day, 10)), await book(seeded, lin, at(seeded.day, 10))
    mei_token = await seeded.token("staff", resource_id=mei)
    assert (await seeded.client.get(f"/v1/admin/bookings/{hers}/history", headers=mei_token)).status_code == 200
    assert (await seeded.client.get(f"/v1/admin/bookings/{his}/history", headers=mei_token)).status_code == 403
    assert (await seeded.client.get(f"/v1/admin/bookings/{uuid.uuid4()}/history", headers=seeded.admin)).status_code == 404
    assert (await seeded.client.get(f"/v1/admin/bookings/{hers}/history")).status_code == 401


# --------------------------------------------------------------------- publishing
def test_the_retry_delay_doubles_up_to_an_hour() -> None:
    assert [outbox.backoff(n).total_seconds() for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert outbox.backoff(20) == dt.timedelta(hours=1)


async def dispatch(s: Seed, now: dt.datetime | None = None) -> outbox.DispatchResult:
    async with tenant_session(s.tenant_id) as db:
        return await outbox.dispatch_pending(db, outbox.get_event_publisher(), now=now)  # type: ignore[arg-type]


async def test_pending_events_are_published_in_order_and_marked(seeded: Seed, publisher: Any) -> None:
    bid = await book(seeded, await seeded.add_staff("Mei"), at(seeded.day, 10))
    assert (await status(seeded, bid, "completed")).status_code == 200
    result = await dispatch(seeded)
    assert (result.published, result.failed) == (4, 0)
    assert publisher.seen == ["shift.updated", "booking.created", "booking.confirmed", "booking.completed"]
    assert all(e.published_at is not None for e in await events(seeded))
    assert (await dispatch(seeded)).published == 0  # nothing is published twice


async def test_a_failed_event_is_retried_later_and_holds_back_only_its_own_aggregate(client: httpx.AsyncClient, publisher: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    first, second = await book(s, mei, at(s.day, 10)), await book(s, mei, at(s.day, 12))
    publisher.fail_types = {"booking.created"}
    now = dt.datetime.now(dt.UTC)
    result = await dispatch(s, now)
    assert (result.published, result.failed) == (1, 2)  # the shift event goes out; each booking's first event fails, so its second waits behind it
    rows = {e.event_type: e for e in await events(s, aggregate=first)}
    assert rows["booking.created"].attempts == 1 and "broker unavailable" in (rows["booking.created"].last_error or "")
    assert rows["booking.created"].available_at > now and rows["booking.confirmed"].published_at is None
    early = await dispatch(s, now + dt.timedelta(seconds=10))  # not due yet: not even attempted again
    assert (early.published, early.failed) == (0, 0)
    assert {e.attempts for e in await events(s, aggregate=first) if e.event_type == "booking.created"} == {1}
    publisher.fail_types = set()
    later = await dispatch(s, now + dt.timedelta(seconds=45))
    assert later.published == 4  # both bookings' events now go out
    assert publisher.seen[1:3] == ["booking.created", "booking.confirmed"] and second is not None


async def test_the_sweep_publishes_when_a_publisher_is_configured_and_otherwise_leaves_events_waiting(seeded: Seed) -> None:
    await book(seeded, await seeded.add_staff("Mei"), at(seeded.day, 10))
    assert (await sweep(seeded, now=soon())).tenants[0].events_published == 0
    assert all(e.published_at is None for e in await events(seeded))  # nothing wired: they wait, they are not dropped

    class Sink:
        got = 0

        async def publish(self, event: OutboxEvent) -> None:
            Sink.got += 1

    outbox.set_event_publisher(Sink())
    try:
        report = await sweep(seeded, now=soon())
        assert report.tenants[0].events_published == 3 and Sink.got == 3  # the shift setup and the booking's two
        old = soon(60 * 24 * 8)  # eight days on: published events are past their retention
        await sweep(seeded, now=old)
        assert await events(seeded) == []
    finally:
        outbox.set_event_publisher(None)


async def test_unpublished_events_are_never_purged(seeded: Seed) -> None:
    await book(seeded, await seeded.add_staff("Mei"), at(seeded.day, 10))
    async with tenant_session(seeded.tenant_id) as db:
        assert await outbox.purge_published(db, older_than=soon(60 * 24 * 365)) == 0
    assert len(await events(seeded)) == 3
