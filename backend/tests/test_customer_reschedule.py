"""Customer rescheduling (blueprint §9.3 "cancel or reschedule"): rules, atomicity, races, every vertical."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.exceptions import TooLateToReschedule
from app.models import Booking, OutboxEvent, Tenant
from app.services import booking as booking_module
from tests.conftest import Seed, allocations, at, make_seed
from tests.test_automotive import gbook, make_garage
from tests.test_customer_cancel import PHONE, _Together, book, cancel, customer_id, login, otp, set_default_policy  # noqa: F401
from tests.test_field_service import KL, NEAR, SEREMBAN, make_practice, pbook, pin
from tests.test_restaurant import book as rbook
from tests.test_restaurant import add_table, make_restaurant, slots
from tests.test_salon import make_salon, sbook


async def move(s: Seed, headers: dict[str, str], bid: str, start: dt.datetime, **extra: Any) -> httpx.Response:
    return await s.client.post(f"/v1/public/bookings/{bid}/reschedule", json={"start_at": start.isoformat(), **extra}, headers=headers)


async def admin_view(s: Seed, bid: str) -> dict[str, Any]:
    r = await s.client.get(f"/v1/admin/bookings/{bid}", headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def local(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


async def events_of(s: Seed, bid: str, event_type: str) -> list[dict[str, Any]]:
    async with tenant_session(s.tenant_id) as db:
        rows = (await db.scalars(select(OutboxEvent).where(OutboxEvent.aggregate_id == uuid.UUID(bid), OutboxEvent.event_type == event_type))).all()
        return [e.payload for e in rows]


# --------------------------------------------------------------------- the basic move
async def test_a_confirmed_booking_moves_and_the_old_slot_is_freed(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    r = await move(seeded, me, bid, at(seeded.day, 15))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "confirmed" and local(out["start_at"]) == at(seeded.day, 15) and local(out["end_at"]) == at(seeded.day, 16)
    assert await allocations(seeded, bid) == [
        ("staff", "Mei", "10:00", "11:00", "released"), ("staff", "Mei", "15:00", "16:00", "active"),
    ]
    view = await admin_view(seeded, bid)
    assert local(view["start_at"]) == at(seeded.day, 15) and view["status"] == "confirmed"
    rebooked = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert rebooked.status_code == 201  # the freed slot is bookable straight away
    taken = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 15), mei), headers=seeded.pk)
    assert taken.status_code == 409  # and the new one is really held


async def test_a_move_that_overlaps_the_old_time_works(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await move(seeded, await login(seeded), bid, at(seeded.day, 10, 30))  # half of it lies inside the booking's own old slot
    assert r.status_code == 200 and local(r.json()["start_at"]) == at(seeded.day, 10, 30)
    assert [row[4] for row in await allocations(seeded, bid)].count("active") == 1


async def test_the_move_is_recorded_as_an_event_not_as_a_status_change(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    assert (await move(seeded, await login(seeded), bid, at(seeded.day, 15))).status_code == 200
    (event,) = await events_of(seeded, bid, "booking.rescheduled")
    assert local(event["previous_start_at"]) == at(seeded.day, 10) and local(event["start_at"]) == at(seeded.day, 15)
    assert local(event["previous_end_at"]) == at(seeded.day, 11) and local(event["end_at"]) == at(seeded.day, 16)
    assert event["status"] == "confirmed" and event["actor_type"] == "customer"
    assert not ({"name", "phone", "email", "customer"} & set(event))  # ids and times only
    history = (await seeded.client.get(f"/v1/admin/bookings/{bid}/history", headers=seeded.admin)).json()
    assert [h["to_status"] for h in history] == ["confirmed"]  # the status never changed


async def test_a_refused_move_leaves_no_event(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await book(seeded, mei, at(seeded.day, 15), phone="+60188880000")
    assert (await move(seeded, await login(seeded), bid, at(seeded.day, 15))).status_code == 409
    assert await events_of(seeded, bid, "booking.rescheduled") == []


async def test_moving_to_the_time_it_already_has_is_refused(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await move(seeded, await login(seeded), bid, at(seeded.day, 10))
    assert r.status_code == 422 and "already at that time" in r.json()["detail"]


async def test_the_list_says_whether_a_booking_can_be_moved(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    listing = (await seeded.client.get("/v1/public/me/bookings", headers=me)).json()
    assert listing[0]["can_reschedule"] is True
    await set_default_policy(seeded, min_notice_cancel=10 * 86400)
    late = await book(seeded, mei, at(seeded.day, 12))  # made under a 10-day cancellation notice, three days out
    listing = {b["id"]: b for b in (await seeded.client.get("/v1/public/me/bookings", headers=me)).json()}
    assert listing[bid]["can_reschedule"] is True and listing[late]["can_reschedule"] is False


# --------------------------------------------------------------------- who and when
async def test_only_the_owner_can_move_a_booking(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    stranger = await login(seeded, "+60199990000")
    assert (await move(seeded, stranger, bid, at(seeded.day, 15))).status_code == 404
    assert (await move(seeded, stranger, str(uuid.uuid4()), at(seeded.day, 15))).status_code == 404
    assert (await move(seeded, seeded.pk, bid, at(seeded.day, 15))).status_code == 401
    assert (await move(seeded, {**seeded.pk, **seeded.admin}, bid, at(seeded.day, 15))).status_code == 403
    assert local((await admin_view(seeded, bid))["start_at"]) == at(seeded.day, 10)


async def test_too_late_to_move_is_refused_and_changes_nothing(seeded: Seed, otp: Any) -> None:
    await set_default_policy(seeded, min_notice_cancel=10 * 86400)
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await move(seeded, await login(seeded), bid, at(seeded.day, 15))
    assert r.status_code == 422 and r.json()["error"] == "too_late_to_reschedule"
    assert local(r.json()["reschedule_deadline"]) == at(seeded.day, 10) - dt.timedelta(days=10)
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "active")]


async def test_the_reschedule_deadline_is_inclusive(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    cust = await customer_id(seeded, bid)
    deadline = at(seeded.day, 10) - dt.timedelta(hours=24)  # the seeded policy's cancellation notice
    async with tenant_session(seeded.tenant_id) as s:
        tenant = await s.get(Tenant, seeded.tenant_id)
        assert tenant is not None
        late = deadline + dt.timedelta(seconds=1)
        with pytest.raises(TooLateToReschedule):
            await booking_module.reschedule_as_customer(s, tenant, uuid.UUID(bid), cust, at(seeded.day, 15), now=late)
    async with tenant_session(seeded.tenant_id) as s:
        tenant = await s.get(Tenant, seeded.tenant_id)
        assert tenant is not None
        moved = await booking_module.reschedule_as_customer(s, tenant, uuid.UUID(bid), cust, at(seeded.day, 15), now=deadline)
        assert moved.start_at == at(seeded.day, 15)


async def test_pending_requests_can_be_moved_freely_and_keep_their_hold(client: httpx.AsyncClient, otp: Any) -> None:
    s = await make_seed(client, default_status="pending")
    await set_default_policy(s, default_status="pending", min_notice_cancel=10 * 86400)  # a confirmed one would be too late
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, at(s.day, 10))
    before = await admin_view(s, bid)
    r = await move(s, await login(s), bid, at(s.day, 15))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    after = await admin_view(s, bid)
    assert after["hold_expires_at"] == before["hold_expires_at"]  # moving does not buy more time
    assert await allocations(s, bid) == [("staff", "Mei", "10:00", "11:00", "released"), ("staff", "Mei", "15:00", "16:00", "held")]
    assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "confirmed"}, headers=s.admin)).status_code == 200
    assert [row[4] for row in await allocations(s, bid)] == ["released", "active"]  # approval activates the moved slot


async def test_only_pending_or_confirmed_bookings_can_be_moved(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    for status in ("in-progress", "waiting-for-parts", "completed", "rejected", "no-show", "cancelled"):
        async with tenant_session(seeded.tenant_id) as s:
            await s.execute(text("UPDATE bookings SET status = :st WHERE id = :i"), {"st": status, "i": uuid.UUID(bid)})
        r = await move(seeded, me, bid, at(seeded.day, 15))
        assert r.status_code == 409, (status, r.text)


async def test_a_cancelled_booking_cannot_be_revived_by_moving_it(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    assert (await cancel(seeded, me, bid)).status_code == 200
    assert (await move(seeded, me, bid, at(seeded.day, 15))).status_code == 409
    assert all(row[4] == "released" for row in await allocations(seeded, bid))


# --------------------------------------------------------------------- where it may go
async def test_a_taken_slot_refuses_the_move_and_the_original_stays(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await book(seeded, mei, at(seeded.day, 15), phone="+60188880000")
    r = await move(seeded, await login(seeded), bid, at(seeded.day, 15))
    assert r.status_code == 409
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "active")]  # rolled back whole
    assert local((await admin_view(seeded, bid))["start_at"]) == at(seeded.day, 10)


async def test_times_outside_the_shift_are_refused(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    for hour, minute in ((8, 0), (13, 0), (17, 30), (23, 0)):  # before the shift, in the break, running past the end, night
        r = await move(seeded, me, bid, at(seeded.day, hour, minute))
        assert r.status_code == 409, (hour, minute, r.text)
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "active")]


async def test_the_booking_window_is_the_one_the_booking_was_made_under(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))  # made under: 60 min notice, 60 days ahead
    me = await login(seeded)
    await set_default_policy(seeded, booking_window=86400, min_notice_booking=172800)  # a far stricter policy from now on
    assert (await move(seeded, me, bid, at(seeded.day + dt.timedelta(days=20), 10))).status_code == 200  # still judged by the old rules
    now = dt.datetime.now(dt.UTC)
    soon = (now + dt.timedelta(seconds=30)).astimezone(at(seeded.day, 0).tzinfo)
    r = await move(seeded, me, bid, soon)  # inside the snapshot's 60 seconds of notice
    assert r.status_code == 422 and r.json()["error"] == "outside_booking_window"
    r = await move(seeded, me, bid, at(seeded.day + dt.timedelta(days=90), 10))
    assert r.status_code == 422 and r.json()["error"] == "outside_booking_window"


async def test_the_staff_member_stays_unless_the_customer_chooses_otherwise(seeded: Seed, otp: Any) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await book(seeded, mei, at(seeded.day, 15), phone="+60188880000")  # Mei is busy at 15:00, Lin is free
    me = await login(seeded)
    assert (await move(seeded, me, bid, at(seeded.day, 15))).status_code == 409  # not silently handed to someone else
    both = await move(seeded, me, bid, at(seeded.day, 15), resource_id=str(lin), any_available=True)
    assert both.status_code == 422
    assert (await move(seeded, me, bid, at(seeded.day, 15), any_available=True)).status_code == 200
    assert await allocations(seeded, bid) == [("staff", "Lin", "15:00", "16:00", "active"), ("staff", "Mei", "10:00", "11:00", "released")]
    again = await move(seeded, me, bid, at(seeded.day, 16), resource_id=str(mei))  # and Mei can be asked for by name
    assert again.status_code == 200
    assert [row[1] for row in await allocations(seeded, bid) if row[4] == "active"] == ["Mei"]


async def test_the_staff_member_is_kept_when_free(seeded: Seed, otp: Any) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    bid = await book(seeded, lin, at(seeded.day, 10))
    for hour in (11, 15):  # Mei is the least-loaded one by name and by load, but Lin was booked
        assert (await move(seeded, await login(seeded), bid, at(seeded.day, hour))).status_code == 200
    assert [row[1] for row in await allocations(seeded, bid) if row[4] == "active"] == ["Lin"]
    assert mei  # (Mei exists and is free throughout)


async def test_a_booking_keeps_the_duration_it_was_made_with(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await seeded.client.patch(f"/v1/admin/services/{seeded.service_id}", json={"duration_minutes": 90, "buffer_minutes": 15}, headers=seeded.admin)
    assert r.status_code == 200, r.text
    moved = await move(seeded, await login(seeded), bid, at(seeded.day, 15))
    assert moved.status_code == 200 and local(moved.json()["end_at"]) == at(seeded.day, 16)  # still 60 minutes
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "released"), ("staff", "Mei", "15:00", "16:00", "active")]


async def test_moving_resets_the_review_flags_for_the_sweep(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    async with tenant_session(seeded.tenant_id) as s:
        await s.execute(text("UPDATE bookings SET flags = ARRAY['outside_shift'], flags_checked_at = now() WHERE id = :i"), {"i": uuid.UUID(bid)})
    assert (await move(seeded, await login(seeded), bid, at(seeded.day, 15))).status_code == 200
    async with tenant_session(seeded.tenant_id) as s:
        b = await s.get(Booking, uuid.UUID(bid))
        assert b is not None and b.flags == [] and b.flags_checked_at is None


async def test_a_day_off_blocks_the_move(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    other = seeded.day + dt.timedelta(days=1)
    r = await seeded.client.post("/v1/admin/days-off", json={"name": "Holiday", "start_date": other.isoformat(), "end_date": other.isoformat()}, headers=seeded.admin)
    assert r.status_code in (200, 201), r.text
    assert (await move(seeded, await login(seeded), bid, at(other, 10))).status_code == 409


# --------------------------------------------------------------------- races
async def test_two_customers_moving_into_the_same_slot_have_one_winner(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    a = await book(seeded, mei, at(seeded.day, 10), phone="+60111110001")
    b = await book(seeded, mei, at(seeded.day, 12), phone="+60111110002")
    ha, hb = await login(seeded, "+60111110001"), await login(seeded, "+60111110002")
    with monkeypatch.context() as m:
        m.setattr(booking_module, "reschedule_as_customer", _Together(2).wrap(booking_module.reschedule_as_customer))
        ra, rb = await asyncio.gather(move(seeded, ha, a, at(seeded.day, 15)), move(seeded, hb, b, at(seeded.day, 15)))
    assert sorted((ra.status_code, rb.status_code)) == [200, 409], (ra.text, rb.text)
    lost = b if ra.status_code == 200 else a
    assert [row for row in await allocations(seeded, lost) if row[4] == "active"] != [] and len(
        [row for row in await allocations(seeded, lost) if row[4] == "active"]) == 1  # the loser kept exactly its old slot
    starts = sorted(local(v["start_at"]) for v in [await admin_view(seeded, a), await admin_view(seeded, b)])
    assert len(set(starts)) == 2


async def test_a_move_racing_a_cancellation_never_disagrees_with_itself(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    for round_ in range(3):
        bid = await book(seeded, mei, at(seeded.day, 9 + round_))
        me = await login(seeded)
        with monkeypatch.context() as m:
            together = _Together(2)  # one barrier for both entry points: whichever arrives first waits for the other
            m.setattr(booking_module, "reschedule_as_customer", together.wrap(booking_module.reschedule_as_customer))
            m.setattr(booking_module, "transition_booking", together.wrap(booking_module.transition_booking))
            moved, cancelled = await asyncio.gather(move(seeded, me, bid, at(seeded.day, 15 + round_)), cancel(seeded, me, bid))
        assert moved.status_code in (200, 409) and cancelled.status_code in (200, 409), (moved.text, cancelled.text)
        view = await admin_view(seeded, bid)
        live = [row for row in await allocations(seeded, bid) if row[4] in ("held", "active")]
        if view["status"] == "cancelled":
            assert live == []
        else:
            assert view["status"] == "confirmed" and len(live) == 1
            assert live[0][2] == local(view["start_at"]).astimezone(at(seeded.day, 0).tzinfo).strftime("%H:%M")
        assert cancelled.status_code == 200 or view["status"] == "confirmed"


async def test_two_moves_of_the_same_booking_run_one_after_the_other(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    with monkeypatch.context() as m:
        m.setattr(booking_module, "reschedule_as_customer", _Together(2).wrap(booking_module.reschedule_as_customer))
        r1, r2 = await asyncio.gather(move(seeded, me, bid, at(seeded.day, 15)), move(seeded, me, bid, at(seeded.day, 16)))
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    live = [row for row in await allocations(seeded, bid) if row[4] == "active"]
    assert len(live) == 1  # never two slots held for one booking
    view = await admin_view(seeded, bid)
    assert live[0][2] == local(view["start_at"]).astimezone(at(seeded.day, 0).tzinfo).strftime("%H:%M")


# --------------------------------------------------------------------- the other verticals
async def test_salon_keeps_the_add_ons_and_the_stylist(client: httpx.AsyncClient, otp: Any) -> None:
    salon = await make_salon(client)
    r = await sbook(salon, at(salon.day, 10), ["cut", "gel"], stylist="Lin", customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    moved = await move(salon, await login(salon), bid, at(salon.day, 15))
    assert moved.status_code == 200, moved.text
    assert local(moved.json()["end_at"]) == at(salon.day, 16, 15)  # 45 + 30 minutes, add-on included
    assert [row for row in await allocations(salon, bid) if row[4] == "active"] == [("staff", "Lin", "15:00", "16:15", "active")]


async def test_restaurant_moves_to_a_fresh_table_and_asks_again_about_the_minimum_spend(client: httpx.AsyncClient, otp: Any) -> None:
    rest = await make_restaurant(client)
    r = await rbook(rest, at(rest.day, 19), 3, customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    me = await login(rest)
    moved = await move(rest, me, bid, at(rest.day, 12))
    assert moved.status_code == 200 and local(moved.json()["start_at"]) == at(rest.day, 12)
    live = [row for row in await allocations(rest, bid) if row[4] == "active"]
    assert live == [("table", "c4", "12:00", "13:45", "active")]  # 90 minutes seated + 15 to reset
    assert local(moved.json()["end_at"]) == at(rest.day, 13, 45)  # a restaurant booking's window includes the table reset
    # a VIP booking: the same table, but accepting the minimum spend is part of every VIP booking, so a move needs no new consent
    v = await rbook(rest, at(rest.day, 19), 3, table_type="vip", accept_minimum_spend=True, customer={"name": "Aisha", "phone": PHONE})
    assert v.status_code == 201, v.text
    vid = v.json()["id"]
    assert (await move(rest, me, vid, at(rest.day, 20))).status_code == 200
    assert Decimal((await admin_view(rest, vid))["minimum_spend_agreed"]) == Decimal(300)


async def test_restaurant_vip_move_to_a_different_minimum_spend_needs_consent(client: httpx.AsyncClient, otp: Any) -> None:
    rest = await make_restaurant(client)
    # a party of 2 gets the cheaper VIP 2-top (200); while that is taken at 20:00 the 4-top (300) is the only VIP table left
    v = await rbook(rest, at(rest.day, 12), 2, table_type="vip", accept_minimum_spend=True, customer={"name": "Aisha", "phone": PHONE})
    assert v.status_code == 201 and Decimal(v.json()["minimum_spend_agreed"]) == Decimal(200)
    blocker = await rbook(rest, at(rest.day, 20), 2, table_type="vip", accept_minimum_spend=True)
    assert blocker.status_code == 201
    me = await login(rest)
    r = await move(rest, me, v.json()["id"], at(rest.day, 20))
    assert r.status_code == 422 and r.json()["error"] == "minimum_spend_not_accepted" and Decimal(r.json()["minimum_spend"]) == Decimal(300)
    assert Decimal((await admin_view(rest, v.json()["id"]))["minimum_spend_agreed"]) == Decimal(200)  # refused: nothing changed
    ok = await move(rest, me, v.json()["id"], at(rest.day, 20), accept_minimum_spend=True)
    assert ok.status_code == 200
    assert Decimal((await admin_view(rest, v.json()["id"]))["minimum_spend_agreed"]) == Decimal(300)


async def vip_move_scene(client: httpx.AsyncClient) -> tuple[Any, str, str]:
    """A VIP booking agreed at 200 (12:00). At 20:00 the 200 room is taken, so the page shows 300; before the customer submits, a party of 4
    takes the 300 room, leaving only the courtyard (1,200). Returns the restaurant, the booking id and the amount that was shown."""
    rest = await make_restaurant(client)
    await add_table(rest, "courtyard", 6, {"is_vip": True, "minimum_spend": 1200})
    v = await rbook(rest, at(rest.day, 12), 2, table_type="vip", accepted_minimum_spend=200, customer={"name": "Aisha", "phone": PHONE})
    assert v.status_code == 201 and Decimal(v.json()["minimum_spend_agreed"]) == Decimal(200)
    assert (await rbook(rest, at(rest.day, 20), 2, table_type="vip", accept_minimum_spend=True)).status_code == 201
    shown = next(s for s in await slots(rest, 2, table_type="vip") if s["start_at"] == at(rest.day, 20).isoformat())
    assert Decimal(shown["minimum_spend"]) == Decimal(300)
    assert (await rbook(rest, at(rest.day, 20), 4, table_type="vip", accept_minimum_spend=True)).status_code == 201
    return rest, v.json()["id"], shown["minimum_spend"]


async def test_restaurant_vip_move_never_lands_above_the_amount_shown(client: httpx.AsyncClient, otp: Any) -> None:
    rest, bid, shown = await vip_move_scene(client)
    me = await login(rest)
    r = await move(rest, me, bid, at(rest.day, 20), accept_minimum_spend=True, accepted_minimum_spend=shown)
    assert r.status_code == 422 and r.json()["error"] == "minimum_spend_not_accepted", r.text
    assert Decimal(r.json()["minimum_spend"]) == Decimal(1200) and Decimal(r.json()["accepted_minimum_spend"]) == Decimal(300)
    view = await admin_view(rest, bid)
    assert Decimal(view["minimum_spend_agreed"]) == Decimal(200) and local(view["start_at"]) == at(rest.day, 12)  # refused: nothing changed
    ok = await move(rest, me, bid, at(rest.day, 20), accepted_minimum_spend="1200")  # the customer confirms the new amount
    assert ok.status_code == 200, ok.text
    assert Decimal((await admin_view(rest, bid))["minimum_spend_agreed"]) == Decimal(1200)


async def test_restaurant_vip_move_at_the_agreed_amount_needs_no_new_number(client: httpx.AsyncClient, otp: Any) -> None:
    rest = await make_restaurant(client)
    v = await rbook(rest, at(rest.day, 12), 2, table_type="vip", accepted_minimum_spend=200, customer={"name": "Aisha", "phone": PHONE})
    assert v.status_code == 201
    me = await login(rest)
    r = await move(rest, me, v.json()["id"], at(rest.day, 15), accepted_minimum_spend=0)  # the same 200 room: already agreed for this booking
    assert r.status_code == 200, r.text
    assert Decimal((await admin_view(rest, v.json()["id"]))["minimum_spend_agreed"]) == Decimal(200)


async def test_workshop_segments_keep_their_order(client: httpx.AsyncClient, otp: Any) -> None:
    garage = await make_garage(client)
    r = await gbook(garage, at(garage.day, 9), ["brake", "oil"], customer={"name": "Aisha", "phone": PHONE})  # 120 min then 60 min
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    before = [row for row in await allocations(garage, bid) if row[0] == "staff"]
    assert [(row[2], row[3]) for row in before] == [("09:00", "11:00"), ("11:00", "12:00")]
    moved = await move(garage, await login(garage), bid, at(garage.day, 14))
    assert moved.status_code == 200, moved.text
    after = [row for row in await allocations(garage, bid) if row[0] == "staff" and row[4] == "active"]
    assert [(row[2], row[3]) for row in after] == [("14:00", "16:00"), ("16:00", "17:00")]  # brake first, as booked
    assert [row for row in await allocations(garage, bid) if row[0] == "bay" and row[4] == "active"] != []  # and a bay is held too
    assert local(moved.json()["end_at"]) == at(garage.day, 17)


async def test_on_site_visit_keeps_its_travel_time_and_address(client: httpx.AsyncClient, otp: Any, postgis: None) -> None:
    practice = await make_practice(client)
    r = await pbook(practice, at(practice.day, 10), "site", address=pin(NEAR), customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    assert await allocations(practice, bid) == [("staff", "Dr Lim", "09:30", "11:30", "active")]  # 30 min of travel either side
    moved = await move(practice, await login(practice), bid, at(practice.day, 15))
    assert moved.status_code == 200, moved.text
    assert local(moved.json()["start_at"]) == at(practice.day, 15) and local(moved.json()["end_at"]) == at(practice.day, 16)  # visit time only
    assert [row for row in await allocations(practice, bid) if row[4] == "active"] == [("staff", "Dr Lim", "14:30", "16:30", "active")]


async def test_on_site_move_rechecks_the_service_radius(client: httpx.AsyncClient, otp: Any, postgis: None) -> None:
    practice = await make_practice(client)
    r = await pbook(practice, at(practice.day, 10), "site", address=pin(NEAR), customer={"name": "Aisha", "phone": PHONE})
    bid = r.json()["id"]
    async with tenant_session(practice.tenant_id) as s:  # the business has since moved far away from the customer
        await s.execute(text("UPDATE tenants SET latitude = :la, longitude = :lo WHERE id = :t"), {"la": SEREMBAN[0], "lo": SEREMBAN[1], "t": practice.tenant_id})
    moved = await move(practice, await login(practice), bid, at(practice.day, 15))
    assert moved.status_code == 422 and moved.json()["error"] == "outside_service_area"
    assert await allocations(practice, bid) == [("staff", "Dr Lim", "09:30", "11:30", "active")]
    assert KL  # (the business was at KL when the booking was made)


async def test_office_visit_moves_without_travel_time(client: httpx.AsyncClient, otp: Any) -> None:
    practice = await make_practice(client)
    r = await pbook(practice, at(practice.day, 10), "office", customer={"name": "Aisha", "phone": PHONE})
    bid = r.json()["id"]
    assert (await move(practice, await login(practice), bid, at(practice.day, 15))).status_code == 200
    assert [row for row in await allocations(practice, bid) if row[4] == "active"] == [("staff", "Dr Lim", "15:00", "16:00", "active")]


async def test_services_are_stored_in_the_order_they_were_chosen(client: httpx.AsyncClient, otp: Any) -> None:
    garage = await make_garage(client)
    for services, expected in ((["brake", "oil"], ["Brake service", "Oil change"]), (["oil", "brake"], ["Oil change", "Brake service"])):
        r = await gbook(garage, at(garage.day, 9 if services[0] == "brake" else 14), services, customer={"name": "Aisha", "phone": PHONE})
        assert r.status_code == 201, r.text
        async with tenant_session(garage.tenant_id) as db:
            rows = (await db.execute(text(
                "SELECT s.name, i.seq FROM booking_items i JOIN services s ON s.id = i.service_id WHERE i.booking_id = :b ORDER BY i.seq"
            ), {"b": uuid.UUID(r.json()["id"])})).all()
        assert [(n, q) for n, q in rows] == [(expected[0], 0), (expected[1], 1)]


async def test_restaurant_party_that_needed_several_tables_gets_several_again(client: httpx.AsyncClient, otp: Any) -> None:
    rest = await make_restaurant(client)
    r = await rbook(rest, at(rest.day, 19), 7, allow_separate_seating=True, customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    assert len(r.json()["resource_ids"]) == 2
    bid = r.json()["id"]
    moved = await move(rest, await login(rest), bid, at(rest.day, 12))
    assert moved.status_code == 200, moved.text
    live = [row for row in await allocations(rest, bid) if row[4] == "active"]
    assert len(live) == 2 and {row[2] for row in live} == {"12:00"}  # the party still sits together-ish: two tables, same hours


async def test_a_restaurant_that_lets_guests_choose_a_table_lets_them_choose_again(client: httpx.AsyncClient, otp: Any) -> None:
    rest = await make_restaurant(client)
    assert (await client.put("/v1/admin/time-slot-settings", headers=rest.admin, json={
        "booking_mode": "flexible", "slot_duration_minutes": 90, "slot_interval_minutes": 30, "first_slot_time": "11:00:00",
        "last_slot_time": "20:30:00", "buffer_minutes": 15, "allow_table_selection": True})).status_code == 200
    r = await rbook(rest, at(rest.day, 19), 2, resource_id=str(rest.tables["b2"]), customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    me = await login(rest)
    moved = await move(rest, me, bid, at(rest.day, 12), resource_id=str(rest.tables["a2"]))
    assert moved.status_code == 200 and [row[1] for row in await allocations(rest, bid) if row[4] == "active"] == ["a2"]
    nonsense = await move(rest, me, bid, at(rest.day, 13), resource_id=str(uuid.uuid4()))
    assert nonsense.status_code in (404, 409, 422)
    assert [row[1] for row in await allocations(rest, bid) if row[4] == "active"] == ["a2"]


async def test_the_previous_staff_member_is_found_even_when_a_bay_is_listed_first(client: httpx.AsyncClient, otp: Any) -> None:
    from types import SimpleNamespace

    garage = await make_garage(client)
    r = await gbook(garage, at(garage.day, 9), ["oil"], resource_id=str(garage.mechanics["Bala"]), customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    async with tenant_session(garage.tenant_id) as db:
        booking = await db.get(Booking, uuid.UUID(r.json()["id"]))
        assert booking is not None
        live = sorted(booking.allocations, key=lambda a: a.resource_id != garage.bays["Bay 1"] and a.resource_id != garage.bays["Bay 2"])  # bay first
        assert live[0].resource_id in garage.bays.values()
        found = await booking_module._previous_staff(db, SimpleNamespace(allocations=live))  # type: ignore[arg-type]
        assert found == garage.mechanics["Bala"]
    moved = await move(garage, await login(garage), r.json()["id"], at(garage.day, 15))
    assert moved.status_code == 200
    assert [row[1] for row in await allocations(garage, r.json()["id"]) if row[0] == "staff" and row[4] == "active"] == ["Bala"]
