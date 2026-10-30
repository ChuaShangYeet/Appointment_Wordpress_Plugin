"""Admin rescheduling: a manager or owner moves a customer's pending or confirmed booking (blueprint §14 admin bookings)."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.core.exceptions import OutsideBookingWindow, ValidationFailed
from app.models import Booking, BookingReminder, Tenant
from app.services import booking as booking_module
from tests.conftest import Recorder, Seed, allocations, at, make_seed
from tests.test_automotive import gbook, make_garage
from tests.test_customer_cancel import PHONE, _Together, book, cancel, customer_id, login, otp, set_default_policy  # noqa: F401
from tests.test_customer_reschedule import admin_view, events_of, local, move, vip_move_scene
from tests.test_field_service import NEAR, make_practice, pbook, pin
from tests.test_notifications import run_outbox
from tests.test_restaurant import book as rbook
from tests.test_restaurant import make_restaurant
from tests.test_salon import make_salon, sbook

ROUTE = "POST /v1/admin/bookings/{booking_id}/reschedule"


async def amove(s: Seed, bid: str, start: dt.datetime, headers: dict[str, str] | None = None, **extra: Any) -> httpx.Response:
    return await s.client.post(f"/v1/admin/bookings/{bid}/reschedule", json={"start_at": start.isoformat(), **extra}, headers=headers or s.admin)


async def audit_entries(s: Seed, action: str = "reschedule") -> list[dict[str, Any]]:
    r = await s.client.get("/v1/admin/audit-log", params={"action": action}, headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def active(rows: list[tuple[str, str, str, str, str]]) -> list[tuple[str, str, str, str, str]]:
    return [row for row in rows if row[4] == "active"]


async def run_as_admin(s: Seed, bid: str, start: dt.datetime, *, now: dt.datetime | None = None, **kw: Any) -> Booking:
    async with tenant_session(s.tenant_id) as db:
        tenant = await db.get(Tenant, s.tenant_id)
        assert tenant is not None
        return await booking_module.reschedule_as_admin(db, tenant, uuid.UUID(bid), start, now=now, **kw)


# --------------------------------------------------------------------- the basic move
async def test_an_owner_moves_a_booking_and_the_old_slot_is_freed(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await amove(seeded, bid, at(seeded.day, 15))
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "confirmed" and local(out["start_at"]) == at(seeded.day, 15) and local(out["end_at"]) == at(seeded.day, 16)
    assert out["resource_ids"] == [str(mei)] and out["id"] == bid
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "released"), ("staff", "Mei", "15:00", "16:00", "active")]
    view = await admin_view(seeded, bid)
    assert local(view["start_at"]) == at(seeded.day, 15) and view["status"] == "confirmed"
    assert (await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)).status_code == 201
    assert (await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 15), mei), headers=seeded.pk)).status_code == 409


async def test_a_move_that_overlaps_the_old_time_works(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await amove(seeded, bid, at(seeded.day, 10, 30))
    assert r.status_code == 200 and local(r.json()["start_at"]) == at(seeded.day, 10, 30)
    assert len(active(await allocations(seeded, bid))) == 1


async def test_managers_can_move_bookings_too(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await amove(seeded, bid, at(seeded.day, 15), headers=await seeded.token("manager"))
    assert r.status_code == 200 and local(r.json()["start_at"]) == at(seeded.day, 15)


async def test_staff_and_anonymous_callers_cannot_move_a_booking(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    assert (await amove(seeded, bid, at(seeded.day, 15), headers=await seeded.token("staff"))).status_code == 403
    assert (await amove(seeded, bid, at(seeded.day, 15), headers=await seeded.token("staff", resource_id=mei))).status_code == 403  # not even their own
    assert (await seeded.client.post(f"/v1/admin/bookings/{bid}/reschedule", json={"start_at": at(seeded.day, 15).isoformat()})).status_code == 401
    assert (await amove(seeded, bid, at(seeded.day, 15), headers=seeded.pk)).status_code == 401  # the public site key is no admin credential
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "active")]
    assert await events_of(seeded, bid, "booking.rescheduled") == []
    assert await audit_entries(seeded) == []


async def test_the_move_is_recorded_as_staff_work_and_in_the_audit_log(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    assert (await amove(seeded, bid, at(seeded.day, 15), headers=await seeded.token("manager"))).status_code == 200
    (event,) = await events_of(seeded, bid, "booking.rescheduled")
    assert local(event["previous_start_at"]) == at(seeded.day, 10) and local(event["start_at"]) == at(seeded.day, 15)
    assert local(event["previous_end_at"]) == at(seeded.day, 11) and local(event["end_at"]) == at(seeded.day, 16)
    assert event["status"] == "confirmed" and event["actor_type"] == "staff"
    assert not ({"name", "phone", "email", "customer"} & set(event))
    history = (await seeded.client.get(f"/v1/admin/bookings/{bid}/history", headers=seeded.admin)).json()
    assert [h["to_status"] for h in history] == ["confirmed"]  # the status never changed
    (entry,) = await audit_entries(seeded)
    assert entry["action"] == ROUTE and entry["role"] == "manager" and entry["actor_id"] == "wp-1"
    assert entry["target_type"] == "booking" and entry["target_id"] == bid and entry["detail"] == {}


async def test_a_refused_move_leaves_no_event_and_no_audit_entry(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await book(seeded, mei, at(seeded.day, 15), phone="+60198880000")
    assert (await amove(seeded, bid, at(seeded.day, 15))).status_code == 409
    assert await events_of(seeded, bid, "booking.rescheduled") == []
    assert await audit_entries(seeded) == []


async def test_moving_to_the_time_it_already_has_is_refused(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await amove(seeded, bid, at(seeded.day, 10))
    assert r.status_code == 422 and "already at that time" in r.json()["detail"]


async def test_an_unknown_booking_is_not_found_and_so_is_another_businesses(client: httpx.AsyncClient) -> None:
    mine, other = await make_seed(client), await make_seed(client)
    theirs = await other.add_staff("Zed")
    bid = await book(other, theirs, at(other.day, 10))
    assert (await amove(mine, bid, at(mine.day, 15))).status_code == 404  # row-level security: it does not exist for this business
    assert (await amove(mine, str(uuid.uuid4()), at(mine.day, 15))).status_code == 404
    assert await allocations(other, bid) == [("staff", "Zed", "10:00", "11:00", "active")]


# --------------------------------------------------------------------- what the business may do that a customer may not
async def test_the_customers_cancellation_deadline_does_not_apply(seeded: Seed, otp: Any) -> None:
    await set_default_policy(seeded, min_notice_cancel=10 * 86400)
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    refused = await move(seeded, await login(seeded), bid, at(seeded.day, 15))
    assert refused.status_code == 422 and refused.json()["error"] == "too_late_to_reschedule"
    moved = await amove(seeded, bid, at(seeded.day, 15))
    assert moved.status_code == 200 and local(moved.json()["start_at"]) == at(seeded.day, 15)


async def test_the_booking_window_does_not_apply_far_ahead(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))  # made under: 60 days ahead at most
    far = seeded.day + dt.timedelta(days=120)
    refused = await move(seeded, await login(seeded), bid, at(far, 10))
    assert refused.status_code == 422 and refused.json()["error"] == "outside_booking_window"
    moved = await amove(seeded, bid, at(far, 10))
    assert moved.status_code == 200 and local(moved.json()["start_at"]) == at(far, 10)
    assert active(await allocations(seeded, bid)) == [("staff", "Mei", "10:00", "11:00", "active")]


async def test_the_minimum_notice_does_not_apply_either(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    earlier = seeded.day - dt.timedelta(days=2)  # an earlier day, so the customer's own cancellation deadline (a day before 10:00) is not what stops them
    clock = at(earlier, 10) - dt.timedelta(seconds=30)  # the policy asks for 60 seconds of notice
    cust = await customer_id(seeded, bid)
    async with tenant_session(seeded.tenant_id) as db:
        tenant = await db.get(Tenant, seeded.tenant_id)
        assert tenant is not None
        with pytest.raises(OutsideBookingWindow):  # the customer's version of the same move is refused
            await booking_module.reschedule_as_customer(db, tenant, uuid.UUID(bid), cust, at(earlier, 10), now=clock)
    moved = await run_as_admin(seeded, bid, at(earlier, 10), now=clock)
    assert moved.start_at == at(earlier, 10)


async def test_the_new_time_must_lie_in_the_future(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    yesterday = seeded.day - dt.timedelta(days=4)
    r = await amove(seeded, bid, at(yesterday, 10))
    assert r.status_code == 422 and "future" in r.json()["detail"]
    with pytest.raises(ValidationFailed, match="future"):  # the clock reading itself is not "in the future"
        await run_as_admin(seeded, bid, at(seeded.day, 15), now=at(seeded.day, 15))
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "active")]


# --------------------------------------------------------------------- which bookings can be moved
async def test_only_pending_or_confirmed_bookings_can_be_moved(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    for status in ("in-progress", "completed", "rejected", "no-show", "cancelled"):
        async with tenant_session(seeded.tenant_id) as s:
            await s.execute(text("UPDATE bookings SET status = :st WHERE id = :i"), {"st": status, "i": uuid.UUID(bid)})
        r = await amove(seeded, bid, at(seeded.day, 15))
        assert r.status_code == 409 and status in r.json()["detail"], (status, r.text)
    assert await events_of(seeded, bid, "booking.rescheduled") == []


async def test_a_job_waiting_for_parts_is_pointed_to_resume(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    async with tenant_session(seeded.tenant_id) as s:
        await s.execute(text("UPDATE bookings SET status = 'waiting-for-parts' WHERE id = :i"), {"i": uuid.UUID(bid)})
    r = await amove(seeded, bid, at(seeded.day, 15))
    assert r.status_code == 409 and "/resume" in r.json()["detail"]


async def test_pending_requests_move_and_keep_their_hold(client: httpx.AsyncClient) -> None:
    s = await make_seed(client, default_status="pending")
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, at(s.day, 10))
    before = await admin_view(s, bid)
    r = await amove(s, bid, at(s.day, 15))
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert (await admin_view(s, bid))["hold_expires_at"] == before["hold_expires_at"]  # moving does not buy more time
    assert await allocations(s, bid) == [("staff", "Mei", "10:00", "11:00", "released"), ("staff", "Mei", "15:00", "16:00", "held")]
    assert (await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "confirmed"}, headers=s.admin)).status_code == 200
    assert [row[4] for row in await allocations(s, bid)] == ["released", "active"]  # approving activates the moved slot


async def test_a_pending_request_whose_hold_ran_out_cannot_be_moved(client: httpx.AsyncClient) -> None:
    s = await make_seed(client, default_status="pending")
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, at(s.day, 10))
    async with tenant_session(s.tenant_id) as db:
        await db.execute(text("UPDATE bookings SET hold_expires_at = now() - interval '1 minute' WHERE id = :i"), {"i": uuid.UUID(bid)})
    r = await amove(s, bid, at(s.day, 15))
    assert r.status_code == 409 and "rejected" in r.json()["detail"]  # (the refusal rolls the request back; the sweep records the rejection)
    assert await events_of(s, bid, "booking.rescheduled") == []
    assert await allocations(s, bid) == [("staff", "Mei", "10:00", "11:00", "held")]


# --------------------------------------------------------------------- where it may go: the business's own rules still hold
async def test_a_taken_slot_refuses_the_move_and_the_original_stays(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await book(seeded, mei, at(seeded.day, 15), phone="+60198880000")
    r = await amove(seeded, bid, at(seeded.day, 15))
    assert r.status_code == 409
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "active")]  # rolled back whole
    assert local((await admin_view(seeded, bid))["start_at"]) == at(seeded.day, 10)


async def test_times_outside_the_shift_are_still_refused(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    for hour, minute in ((8, 0), (13, 0), (17, 30), (23, 0)):  # before the shift, in the break, running past the end, night
        r = await amove(seeded, bid, at(seeded.day, hour, minute))
        assert r.status_code == 409, (hour, minute, r.text)
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "active")]


async def test_a_day_off_blocks_the_move(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    other = seeded.day + dt.timedelta(days=1)
    r = await seeded.client.post("/v1/admin/days-off", json={"name": "Holiday", "start_date": other.isoformat(), "end_date": other.isoformat()}, headers=seeded.admin)
    assert r.status_code in (200, 201), r.text
    assert (await amove(seeded, bid, at(other, 10))).status_code == 409


async def test_the_staff_member_stays_unless_the_business_chooses_otherwise(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    bid = await book(seeded, mei, at(seeded.day, 10))
    await book(seeded, mei, at(seeded.day, 15), phone="+60198880000")  # Mei is busy at 15:00, Lin is free
    assert (await amove(seeded, bid, at(seeded.day, 15))).status_code == 409  # not silently handed to someone else
    both = await amove(seeded, bid, at(seeded.day, 15), resource_id=str(lin), any_available=True)
    assert both.status_code == 422
    assert (await amove(seeded, bid, at(seeded.day, 15), any_available=True)).status_code == 200
    assert await allocations(seeded, bid) == [("staff", "Lin", "15:00", "16:00", "active"), ("staff", "Mei", "10:00", "11:00", "released")]
    again = await amove(seeded, bid, at(seeded.day, 16), resource_id=str(mei))  # and Mei can be asked for by name
    assert again.status_code == 200
    assert [row[1] for row in active(await allocations(seeded, bid))] == ["Mei"]


async def test_the_staff_member_is_kept_when_free(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    bid = await book(seeded, lin, at(seeded.day, 10))
    for hour in (11, 15):  # Mei would win by name and by load, but Lin was booked
        assert (await amove(seeded, bid, at(seeded.day, hour))).status_code == 200
    assert [row[1] for row in active(await allocations(seeded, bid))] == ["Lin"]
    assert mei


async def test_a_booking_keeps_the_duration_it_was_made_with(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await seeded.client.patch(f"/v1/admin/services/{seeded.service_id}", json={"duration_minutes": 90, "buffer_minutes": 15}, headers=seeded.admin)
    assert r.status_code == 200, r.text
    moved = await amove(seeded, bid, at(seeded.day, 15))
    assert moved.status_code == 200 and local(moved.json()["end_at"]) == at(seeded.day, 16)  # still 60 minutes
    assert await allocations(seeded, bid) == [("staff", "Mei", "10:00", "11:00", "released"), ("staff", "Mei", "15:00", "16:00", "active")]


async def test_moving_resets_the_review_flags_and_forgets_the_reminders(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    async with tenant_session(seeded.tenant_id) as s:
        await s.execute(text("UPDATE bookings SET flags = ARRAY['outside_shift'], flags_checked_at = now() WHERE id = :i"), {"i": uuid.UUID(bid)})
        s.add(BookingReminder(tenant_id=seeded.tenant_id, booking_id=uuid.UUID(bid), kind="24h"))
    assert (await amove(seeded, bid, at(seeded.day, 15))).status_code == 200
    async with tenant_session(seeded.tenant_id) as s:
        b = await s.get(Booking, uuid.UUID(bid))
        assert b is not None and b.flags == [] and b.flags_checked_at is None
        assert (await s.scalars(select(BookingReminder).where(BookingReminder.booking_id == uuid.UUID(bid)))).all() == []  # the new time earns its own


# --------------------------------------------------------------------- the customer hears about it
async def test_the_customer_is_told_their_booking_was_moved(seeded: Seed, mail: Recorder) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
        at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": PHONE, "email": "aisha@example.com"}))
    bid = r.json()["id"]
    assert (await amove(seeded, bid, at(seeded.day, 15))).status_code == 200
    await run_outbox(seeded)
    moved = mail.sent[-1]
    assert moved.subject == "Your booking was moved" and moved.recipient == "aisha@example.com"
    assert f"from {at(seeded.day, 10).strftime('%a %d %b %Y')} at 10:00 to {at(seeded.day, 15).strftime('%a %d %b %Y')} at 15:00" in moved.body


# --------------------------------------------------------------------- races
async def test_two_admin_moves_into_the_same_slot_have_one_winner(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    a = await book(seeded, mei, at(seeded.day, 10), phone="+601111110001")
    b = await book(seeded, mei, at(seeded.day, 12), phone="+601111110002")
    with monkeypatch.context() as m:
        m.setattr(booking_module, "reschedule_as_admin", _Together(2).wrap(booking_module.reschedule_as_admin))
        ra, rb = await asyncio.gather(amove(seeded, a, at(seeded.day, 15)), amove(seeded, b, at(seeded.day, 15)))
    assert sorted((ra.status_code, rb.status_code)) == [200, 409], (ra.text, rb.text)
    lost = b if ra.status_code == 200 else a
    assert len(active(await allocations(seeded, lost))) == 1  # the loser kept exactly its old slot
    starts = {local(v["start_at"]) for v in [await admin_view(seeded, a), await admin_view(seeded, b)]}
    assert len(starts) == 2


async def test_two_moves_of_the_same_booking_run_one_after_the_other(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    with monkeypatch.context() as m:
        m.setattr(booking_module, "reschedule_as_admin", _Together(2).wrap(booking_module.reschedule_as_admin))
        r1, r2 = await asyncio.gather(amove(seeded, bid, at(seeded.day, 15)), amove(seeded, bid, at(seeded.day, 16)))
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    live = active(await allocations(seeded, bid))
    assert len(live) == 1  # never two slots held for one booking
    assert live[0][2] == local((await admin_view(seeded, bid))["start_at"]).astimezone(at(seeded.day, 0).tzinfo).strftime("%H:%M")


async def test_a_move_racing_a_customers_cancellation_never_disagrees_with_itself(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    for round_ in range(3):
        bid = await book(seeded, mei, at(seeded.day, 9 + round_))
        me = await login(seeded)
        with monkeypatch.context() as m:
            together = _Together(2)
            m.setattr(booking_module, "reschedule_as_admin", together.wrap(booking_module.reschedule_as_admin))
            m.setattr(booking_module, "transition_booking", together.wrap(booking_module.transition_booking))
            moved, cancelled = await asyncio.gather(amove(seeded, bid, at(seeded.day, 15 + round_)), cancel(seeded, me, bid))
        assert moved.status_code in (200, 409) and cancelled.status_code in (200, 409), (moved.text, cancelled.text)
        view = await admin_view(seeded, bid)
        live = [row for row in await allocations(seeded, bid) if row[4] in ("held", "active")]
        if view["status"] == "cancelled":
            assert live == []
        else:
            assert view["status"] == "confirmed" and len(live) == 1
            assert live[0][2] == local(view["start_at"]).astimezone(at(seeded.day, 0).tzinfo).strftime("%H:%M")
        assert cancelled.status_code == 200 or view["status"] == "confirmed"


# --------------------------------------------------------------------- the other verticals
async def test_salon_keeps_the_add_ons_and_the_stylist(client: httpx.AsyncClient) -> None:
    salon = await make_salon(client)
    r = await sbook(salon, at(salon.day, 10), ["cut", "gel"], stylist="Lin", customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    moved = await amove(salon, bid, at(salon.day, 15))
    assert moved.status_code == 200, moved.text
    assert local(moved.json()["end_at"]) == at(salon.day, 16, 15)  # 45 + 30 minutes, add-on included
    assert active(await allocations(salon, bid)) == [("staff", "Lin", "15:00", "16:15", "active")]
    far = salon.day + dt.timedelta(days=120)
    assert (await amove(salon, bid, at(far, 10))).status_code == 200  # the salon's booking window does not bind the business


async def test_restaurant_moves_to_a_fresh_table_and_the_window_does_not_bind(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    r = await rbook(rest, at(rest.day, 19), 3, customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    moved = await amove(rest, bid, at(rest.day, 12))
    assert moved.status_code == 200 and local(moved.json()["start_at"]) == at(rest.day, 12)
    assert active(await allocations(rest, bid)) == [("table", "c4", "12:00", "13:45", "active")]  # 90 minutes seated + 15 to reset
    far = rest.day + dt.timedelta(days=120)
    assert (await amove(rest, bid, at(far, 12))).status_code == 200
    assert local((await admin_view(rest, bid))["start_at"]) == at(far, 12)


async def test_restaurant_vip_move_to_a_different_minimum_spend_needs_consent(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    v = await rbook(rest, at(rest.day, 12), 2, table_type="vip", accept_minimum_spend=True, customer={"name": "Aisha", "phone": PHONE})
    assert v.status_code == 201 and v.json()["minimum_spend_agreed"] in ("200", "200.00", 200)
    blocker = await rbook(rest, at(rest.day, 20), 2, table_type="vip", accept_minimum_spend=True)
    assert blocker.status_code == 201
    vid = v.json()["id"]
    r = await amove(rest, vid, at(rest.day, 20))  # only the dearer VIP table is free then
    assert r.status_code == 422 and r.json()["error"] == "minimum_spend_not_accepted" and float(r.json()["minimum_spend"]) == 300
    assert float((await admin_view(rest, vid))["minimum_spend_agreed"]) == 200  # refused: nothing changed
    assert (await amove(rest, vid, at(rest.day, 20), accept_minimum_spend=True)).status_code == 200
    assert float((await admin_view(rest, vid))["minimum_spend_agreed"]) == 300


async def test_restaurant_vip_move_never_lands_above_the_amount_shown(client: httpx.AsyncClient) -> None:
    rest, bid, shown = await vip_move_scene(client)
    r = await amove(rest, bid, at(rest.day, 20), accept_minimum_spend=True, accepted_minimum_spend=shown)
    assert r.status_code == 422 and r.json()["error"] == "minimum_spend_not_accepted", r.text
    assert float(r.json()["minimum_spend"]) == 1200 and float(r.json()["accepted_minimum_spend"]) == 300
    view = await admin_view(rest, bid)
    assert float(view["minimum_spend_agreed"]) == 200 and local(view["start_at"]) == at(rest.day, 12)  # refused: nothing changed
    assert (await amove(rest, bid, at(rest.day, 20), accepted_minimum_spend=1200)).status_code == 200
    assert float((await admin_view(rest, bid))["minimum_spend_agreed"]) == 1200
    back = await amove(rest, bid, at(rest.day, 12), accepted_minimum_spend=200)  # the 200 room again
    assert back.status_code == 200 and float((await admin_view(rest, bid))["minimum_spend_agreed"]) == 200  # cheaper than agreed: fine


async def test_workshop_segments_keep_their_order_and_the_window_does_not_bind(client: httpx.AsyncClient) -> None:
    garage = await make_garage(client)
    r = await gbook(garage, at(garage.day, 9), ["brake", "oil"], customer={"name": "Aisha", "phone": PHONE})  # 120 min then 60 min
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    moved = await amove(garage, bid, at(garage.day, 14))
    assert moved.status_code == 200, moved.text
    after = [row for row in active(await allocations(garage, bid)) if row[0] == "staff"]
    assert [(row[2], row[3]) for row in after] == [("14:00", "16:00"), ("16:00", "17:00")]  # brake first, as booked
    assert [row for row in active(await allocations(garage, bid)) if row[0] == "bay"] != []
    far = garage.day + dt.timedelta(days=120)
    assert (await amove(garage, bid, at(far, 9))).status_code == 200


async def test_on_site_visit_keeps_its_travel_time_and_the_window_does_not_bind(client: httpx.AsyncClient, postgis: None) -> None:
    practice = await make_practice(client)
    r = await pbook(practice, at(practice.day, 10), "site", address=pin(NEAR), customer={"name": "Aisha", "phone": PHONE})
    assert r.status_code == 201, r.text
    bid = r.json()["id"]
    moved = await amove(practice, bid, at(practice.day, 15))
    assert moved.status_code == 200, moved.text
    assert local(moved.json()["start_at"]) == at(practice.day, 15) and local(moved.json()["end_at"]) == at(practice.day, 16)
    assert active(await allocations(practice, bid)) == [("staff", "Dr Lim", "14:30", "16:30", "active")]
    far = practice.day + dt.timedelta(days=120)
    assert (await amove(practice, bid, at(far, 10))).status_code == 200


async def test_an_office_visit_moves_without_travel_time(client: httpx.AsyncClient) -> None:
    practice = await make_practice(client)
    r = await pbook(practice, at(practice.day, 10), "office", customer={"name": "Aisha", "phone": PHONE})
    bid = r.json()["id"]
    assert (await amove(practice, bid, at(practice.day, 15))).status_code == 200
    assert active(await allocations(practice, bid)) == [("staff", "Dr Lim", "15:00", "16:00", "active")]
