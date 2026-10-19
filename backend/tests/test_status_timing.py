"""A booking can be marked started, finished or missed only once its time has come (``booking.ensure_status_time``).

Before: a booking next week could be completed ("Thank you for visiting us" and the review link went out ahead of the visit) or marked a no-show, and either freed its slot.
The rest of the suite switches this rule off (``no_status_timing`` in conftest) so that its tests can mark tomorrow's bookings without waiting; this file puts it back.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.database import tenant_session
from app.core.exceptions import InvalidTransition
from app.models import Booking, Tenant
from app.services import booking as booking_module
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_notifications import log_rows, run_outbox
from tests.test_salon import Salon, salon, sbook, status  # noqa: F401  (``salon`` is a fixture)

REAL = booking_module.ensure_status_time
MIN = dt.timedelta(minutes=1)


@pytest.fixture(autouse=True)
def real_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(booking_module, "ensure_status_time", REAL)


START = dt.datetime(2026, 10, 20, 2, 0, tzinfo=dt.UTC)  # 10:00 in Kuala Lumpur


def pure(current: str, new: str, now: dt.datetime, tz: str = "Asia/Kuala_Lumpur") -> None:
    booking_module.ensure_status_time(Booking(status=current, start_at=START, end_at=START + 60 * MIN), new, Tenant(timezone=tz), now)


# --------------------------------------------------------------------- the rule itself
@pytest.mark.parametrize(("current", "new", "minutes_from_start", "allowed"), [
    ("confirmed", "no-show", -1, False),
    ("confirmed", "no-show", 0, True),
    ("confirmed", "no-show", 90, True),
    ("confirmed", "no-show", -10_000, False),
    ("confirmed", "in-progress", -61, False),
    ("confirmed", "in-progress", -60, True),
    ("confirmed", "in-progress", 5, True),
    ("confirmed", "completed", -61, False),
    ("confirmed", "completed", -60, True),
    ("confirmed", "completed", 240, True),
    ("in-progress", "completed", -10_000, True),  # it is under way already: there is nothing to wait for
    ("in-progress", "waiting-for-parts", -10_000, True),
    ("confirmed", "cancelled", -10_000, True),  # how a future booking ends
    ("pending", "rejected", -10_000, True),
    ("pending", "confirmed", -10_000, True),
])
def test_when_a_booking_may_be_started_finished_or_missed(current: str, new: str, minutes_from_start: int, allowed: bool) -> None:
    now = START + minutes_from_start * MIN
    if allowed:
        pure(current, new, now)
    else:
        with pytest.raises(InvalidTransition):
            pure(current, new, now)


@pytest.mark.parametrize(("early", "minutes_from_start", "allowed"), [(0, -1, False), (0, 0, True), (120, -121, False), (120, -120, True), (1440, -1441, False), (1440, -1440, True)])
def test_the_early_window_is_a_setting(monkeypatch: pytest.MonkeyPatch, early: int, minutes_from_start: int, allowed: bool) -> None:
    monkeypatch.setattr(get_settings(), "status_early_minutes", early)
    for new in ("in-progress", "completed"):
        if allowed:
            pure("confirmed", new, START + minutes_from_start * MIN)
        else:
            with pytest.raises(InvalidTransition):
                pure("confirmed", new, START + minutes_from_start * MIN)


def test_the_window_never_applies_to_a_no_show_and_never_to_a_booking_already_in_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "status_early_minutes", 1440)
    with pytest.raises(InvalidTransition):
        pure("confirmed", "no-show", START - MIN)
    monkeypatch.setattr(get_settings(), "status_early_minutes", 0)
    pure("in-progress", "completed", START - MIN)
    with pytest.raises(InvalidTransition):
        pure("confirmed", "completed", START - MIN)


@pytest.mark.parametrize("bad", [-1, 1441])
def test_the_setting_has_sane_bounds(bad: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, status_early_minutes=bad)  # type: ignore[call-arg]
    assert Settings(_env_file=None).status_early_minutes == 60  # type: ignore[call-arg]


def test_the_refusal_says_when_in_the_businesss_own_time() -> None:
    with pytest.raises(InvalidTransition) as late:
        pure("confirmed", "completed", START - 3 * 60 * MIN)
    assert "marked completed" in str(late.value) and "starts at Tue 20 Oct 2026 10:00" in str(late.value) and "from 09:00 Tue 20 Oct" in str(late.value)
    with pytest.raises(InvalidTransition) as missed:
        pure("confirmed", "no-show", START - MIN)
    assert "marked as a no-show" in str(missed.value) and "starts at Tue 20 Oct 2026 10:00" in str(missed.value) and "(from" not in str(missed.value)
    with pytest.raises(InvalidTransition) as sydney:
        pure("confirmed", "no-show", START - MIN, tz="Australia/Sydney")
    assert "starts at Tue 20 Oct 2026 13:00" in str(sydney.value)


# --------------------------------------------------------------------- through the API and the database
async def book(s: Seed, hour: int = 10) -> str:
    mei = await s.add_staff(f"Mei{uuid.uuid4().hex[:4]}")
    r = await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, hour), mei, customer={"name": "Aisha", "phone": f"+6012{uuid.uuid4().int % 10**7:07d}", "email": "a@example.com"}))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def move(s: Seed, bid: str, new: str, now: dt.datetime) -> Booking:
    async with tenant_session(s.tenant_id) as db:
        tenant = await db.get(Tenant, s.tenant_id)
        assert tenant is not None
        return await booking_module.transition_booking(db, tenant, uuid.UUID(bid), new, now=now)


async def allocation_states(s: Seed, bid: str) -> list[str]:
    async with tenant_session(s.tenant_id) as db:
        return [r[0] for r in (await db.execute(text("SELECT state FROM booking_allocations WHERE booking_id = :b"), {"b": bid})).all()]


@pytest.mark.parametrize("new", ["completed", "no-show"])
async def test_a_future_booking_cannot_be_completed_or_missed_and_nothing_is_freed_or_sent(seeded: Seed, new: str) -> None:
    bid = await book(seeded)
    r = await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": new}, headers=seeded.admin)
    assert r.status_code == 409 and r.json()["error"] == "invalid_transition" and "cannot be" in r.json()["detail"] and "starts at" in r.json()["detail"]
    assert (await seeded.client.get(f"/v1/admin/bookings/{bid}", headers=seeded.admin)).json()["status"] == "confirmed"
    assert await allocation_states(seeded, bid) == ["active"]  # the slot is still held
    async with tenant_session(seeded.tenant_id) as db:
        types = [r[0] for r in (await db.execute(text("SELECT event_type FROM outbox_events WHERE aggregate_id = :b ORDER BY seq"), {"b": bid})).all()]
    assert types == ["booking.created", "booking.confirmed"]  # no 'completed', no thank-you message to send


async def test_a_future_booking_can_still_be_cancelled_rejected_or_confirmed(client: httpx.AsyncClient) -> None:
    s = await make_seed(client, default_status="pending")
    first, second, third = await book(s, 10), await book(s, 12), await book(s, 14)
    for bid, new in ((first, "confirmed"), (second, "rejected"), (third, "cancelled")):
        r = await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": new}, headers=s.admin)
        assert r.status_code == 200, (new, r.text)
    assert (await s.client.patch(f"/v1/admin/bookings/{first}/status", json={"status": "cancelled"}, headers=s.admin)).status_code == 200


async def test_no_show_opens_at_the_start_and_then_frees_the_slot(seeded: Seed) -> None:
    bid = await book(seeded)
    start = at(seeded.day, 10)
    with pytest.raises(InvalidTransition):
        await move(seeded, bid, "no-show", start - dt.timedelta(seconds=1))
    assert await allocation_states(seeded, bid) == ["active"]
    done = await move(seeded, bid, "no-show", start)
    assert done.status == "no-show" and await allocation_states(seeded, bid) == ["released"]


async def test_completed_opens_an_hour_before_and_the_thank_you_waits_for_it(seeded: Seed, mail: Recorder) -> None:
    bid = await book(seeded)
    start = at(seeded.day, 10)
    with pytest.raises(InvalidTransition):
        await move(seeded, bid, "completed", start - 61 * MIN)
    assert (await move(seeded, bid, "completed", start - 60 * MIN)).status == "completed"
    assert [r.event for r in await log_rows(seeded, bid)] == []  # (nothing has been sent yet: the outbox has not run)
    await run_outbox(seeded)
    assert "booking.completed" in [r.event for r in await log_rows(seeded, bid)]


async def test_arriving_early_then_finishing_is_fine_whatever_the_window(salon: Salon, monkeypatch: pytest.MonkeyPatch) -> None:
    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    start = at(salon.day, 10)
    monkeypatch.setattr(get_settings(), "status_early_minutes", 0)
    with pytest.raises(InvalidTransition):
        await move(salon, bid, "in-progress", start - dt.timedelta(seconds=1))
    assert (await move(salon, bid, "in-progress", start)).status == "in-progress"
    assert (await move(salon, bid, "completed", start - 30 * MIN)).status == "completed"  # in progress already: nothing to wait for


async def test_the_api_applies_the_rule_to_in_progress_too(salon: Salon) -> None:
    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    r = await status(salon, bid, "in-progress")
    assert r.status_code == 409 and "marked in-progress" in r.json()["detail"]
