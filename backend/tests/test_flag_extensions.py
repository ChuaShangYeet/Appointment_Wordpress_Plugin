"""Extensions (an upsell or an overrun) and the review flags.

The worry was that an extension which commits just after a settings change was validated against the old settings and never re-checked. That can only
matter for a booking the flags examine, and those are the ``pending`` and ``confirmed`` ones; an extension only ever happens to an ``in-progress``
booking, which flags never cover (they are dropped when the appointment starts) and which cannot become ``confirmed`` again except through ``resume``, which
already hands the booking back to the sweep. These tests pin that reasoning down, so that a change breaking it breaks a test.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
import httpx
import pytest
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models import Booking
from app.services import booking as booking_module
from tests.conftest import Seed, at, make_seed
from tests.test_automotive import gbook, make_garage
from tests.test_field_service import _actual, _in_progress, make_practice
from tests.test_flagging import HEADER, flags, put_hours
from tests.test_salon import make_salon, sbook, status, upsell
from tests.test_sweep import soon, sweep


async def marker(s: Seed, bid: str) -> dt.datetime | None:
    async with tenant_session(s.tenant_id) as db:
        return await db.scalar(select(Booking.flags_checked_at).where(Booking.id == uuid.UUID(bid)))


# --------------------------------------------------------------------- an extension needs an appointment that is under way
async def test_an_upsell_is_refused_unless_the_appointment_is_in_progress(client: httpx.AsyncClient) -> None:
    salon = await make_salon(client)
    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    refused = await upsell(salon, bid, ["gel"])
    assert refused.status_code == 409  # a confirmed booking (the kind flags cover) cannot be stretched
    assert (await status(salon, bid, "in-progress")).status_code == 200
    assert (await upsell(salon, bid, ["gel"])).status_code == 200
    assert (await status(salon, bid, "completed")).status_code == 200
    assert (await upsell(salon, bid, ["treat"])).status_code == 409  # nor a finished one


async def test_an_overrun_is_refused_unless_the_job_is_in_progress(client: httpx.AsyncClient) -> None:
    p = await make_practice(client)
    bid = (await p.client.post("/v1/public/bookings", json={
        "service_ids": [str(p.svc["office"])], "start_at": at(p.day, 10).isoformat(), "customer": {"name": "A", "phone": "+60123450001"},
    }, headers=p.pk)).json()["id"]
    assert (await _actual(p, bid, 90)).status_code == 409
    assert (await p.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "in-progress"}, headers=p.admin)).status_code == 200
    assert (await _actual(p, bid, 90)).status_code == 200


def test_nothing_leads_from_a_started_appointment_back_to_a_status_flags_cover() -> None:
    """The one door back is ``resume`` (waiting-for-parts to confirmed), which is not in this table and hands the booking back to the sweep."""
    live = booking_module.LIVE_STATUSES
    assert live == {"pending", "confirmed"}
    for status_ in ("in-progress", "waiting-for-parts"):
        assert not (booking_module.TRANSITIONS[status_] & live), f"{status_} can lead back to a status the review flags examine; revisit the extension re-check"
    assert booking_module.TRANSITIONS["in-progress"] == {"completed", "waiting-for-parts"}


# --------------------------------------------------------------------- in-progress appointments are not flagged, extended or not
async def test_starting_an_appointment_drops_its_flags_and_a_settings_change_does_not_bring_them_back(client: httpx.AsyncClient) -> None:
    salon = await make_salon(client)
    bid = (await sbook(salon, at(salon.day, 16), ["cut"], stylist="Mei")).json()["id"]
    r = await put_hours(salon, "09:00", "15:00")  # the salon now closes before the 16:00 appointment
    assert r.headers[HEADER] == "1" and await flags(salon, bid) == ["outside_operating_hours"]
    assert (await status(salon, bid, "in-progress")).status_code == 200
    assert await flags(salon, bid) == []  # under way: the business has decided to serve this customer
    r = await put_hours(salon, "09:00", "14:00")
    assert r.headers[HEADER] == "0" and await flags(salon, bid) == []


async def test_a_stretched_appointment_that_runs_past_closing_time_is_not_flagged_by_anything(client: httpx.AsyncClient) -> None:
    salon = await make_salon(client)
    bid = (await sbook(salon, at(salon.day, 17), ["cut"], stylist="Mei")).json()["id"]  # 17:00-17:45, the shift ends at 18:00
    assert (await status(salon, bid, "in-progress")).status_code == 200
    assert (await upsell(salon, bid, ["gel", "treat"])).status_code == 200  # ... and now it runs to 18:40, past the end of the shift
    r = await put_hours(salon, "09:00", "18:00")  # a settings change afterwards
    assert r.headers[HEADER] == "0" and await flags(salon, bid) == []
    report = await sweep(salon, now=soon())  # and the sweep, which looks at every booking it has not checked
    assert report.tenants[0].bookings_flagged == 0 and await flags(salon, bid) == []
    assert (await status(salon, bid, "completed")).status_code == 200 and await flags(salon, bid) == []


async def test_an_overrun_is_judged_like_any_other_in_progress_job(client: httpx.AsyncClient) -> None:
    p = await make_practice(client)
    bid = await _in_progress(p, "office", 17)
    assert (await _actual(p, bid, 120)).status_code == 200  # 17:00-19:00, well past the 18:00 end of the day
    r = await put_hours(p, "08:00", "17:30")
    assert r.headers[HEADER] == "0" and await flags(p, bid) == []
    await sweep(p, now=soon())
    assert await flags(p, bid) == []


# --------------------------------------------------------------------- belt and braces: an extension hands the booking back to the sweep
async def test_an_extension_marks_the_booking_for_the_sweep_to_look_at_again(client: httpx.AsyncClient) -> None:
    salon = await make_salon(client)
    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    await sweep(salon, now=soon())
    assert await marker(salon, bid) is not None  # checked once, as every new booking is
    assert (await status(salon, bid, "in-progress")).status_code == 200
    assert (await upsell(salon, bid, ["gel"])).status_code == 200
    assert await marker(salon, bid) is None  # stretched: whatever the sweep concluded before no longer describes the reservation
    await sweep(salon, now=soon(10))
    assert await marker(salon, bid) is not None and await flags(salon, bid) == []  # and one pass later it is settled again


async def test_a_refused_extension_does_not_touch_the_marker(client: httpx.AsyncClient) -> None:
    """If the next appointment is in the way nothing is stretched, so there is nothing new to check."""
    salon = await make_salon(client, stylists=("Mei",))
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]  # 10:00-10:45
    await sbook(salon, at(salon.day, 10, 45), ["cut"], stylist="Mei", customer={"name": "Next", "phone": "+601112223333"})
    await sweep(salon, now=soon())
    assert (await status(salon, first, "in-progress")).status_code == 200
    r = await upsell(salon, first, ["gel"])
    assert r.status_code == 200 and r.json()["extended"] is False  # the neighbour is in the way: the add-on is on the ticket, the reservation is not stretched
    assert await marker(salon, first) is not None


async def test_the_workshop_cannot_be_extended_at_all(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    bid = (await gbook(g, at(g.day, 10), ["oil"])).json()["id"]
    assert (await g.client.patch(f"/v1/admin/bookings/{bid}/actual-duration", json={"minutes": 90}, headers=g.admin)).status_code == 409  # (professional tenants only)
    assert (await g.client.post(f"/v1/admin/bookings/{bid}/items", json={"service_ids": [], "products": []}, headers=g.admin)).status_code in (409, 422)
    async with tenant_session(g.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM booking_items WHERE booking_id = :b AND is_upsell"), {"b": uuid.UUID(bid)})).scalar_one() == 0


async def test_an_extension_the_constraint_refuses_does_not_touch_the_marker_either(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """A booking slips into the gap between the check and the write: nothing is stretched, so there is nothing new to check."""
    salon = await make_salon(client, stylists=("Mei",))
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    await sbook(salon, at(salon.day, 10, 45), ["cut"], stylist="Mei", customer={"name": "Next", "phone": "+601112223333"})
    await sweep(salon, now=soon())
    assert (await status(salon, first, "in-progress")).status_code == 200
    real = booking_module._clashes
    calls = {"n": 0}

    async def blind_first_time(*a: Any, **k: Any) -> list[uuid.UUID]:
        calls["n"] += 1
        return [] if calls["n"] == 1 else await real(*a, **k)

    monkeypatch.setattr(booking_module, "_clashes", blind_first_time)
    r = await upsell(salon, first, ["gel"])
    assert r.status_code == 200 and r.json()["extended"] is False and calls["n"] == 2
    assert await marker(salon, first) is not None
