"""Customer OTP login and cancellation (blueprint §14): ownership, notice rules, races."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import TooLateToCancel
from app.core.security import decode_jwt
from app.models import Tenant
from app.services import booking as booking_module
from tests.conftest import Seed, allocations, at, make_seed

PHONE = "+60123450001"
CODE = "123456"


# --------------------------------------------------------------------- fixtures
@pytest.fixture
def otp(monkeypatch: pytest.MonkeyPatch, mail: Any) -> Any:
    """An in-memory Redis, a fixed one-time code and a recording delivery provider (``mail``), so the OTP flow runs without Redis or a gateway."""
    import fakeredis.aioredis

    from app.api.v1 import auth as auth_module
    from app.core import redis as redis_module

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis", fake)
    monkeypatch.setattr(auth_module, "generate_otp", lambda: CODE)
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 0)  # tests that need it switch it on
    return fake


async def request_code(s: Seed, phone: str = PHONE) -> httpx.Response:
    return await s.client.post("/v1/public/auth/otp", json={"phone": phone}, headers=s.pk)


async def verify(s: Seed, phone: str = PHONE, code: str = CODE, **extra: Any) -> httpx.Response:
    return await s.client.post("/v1/public/auth/otp/verify", json={"phone": phone, "code": code, **extra}, headers=s.pk)


async def login(s: Seed, phone: str = PHONE) -> dict[str, str]:
    """Headers of a signed-in customer: the site key plus the OTP-issued bearer token."""
    assert (await request_code(s, phone)).status_code == 202
    r = await verify(s, phone)
    assert r.status_code == 200, r.text
    return {**s.pk, "Authorization": f"Bearer {r.json()['access_token']}"}


async def book(s: Seed, staff: uuid.UUID, start: dt.datetime, phone: str = PHONE) -> str:
    body = s.booking_body(start, staff, customer={"name": "Aisha", "phone": phone})
    r = await s.client.post("/v1/public/bookings", json=body, headers=s.pk)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def cancel(s: Seed, headers: dict[str, str], bid: str) -> httpx.Response:
    return await s.client.post(f"/v1/public/bookings/{bid}/cancel", headers=headers)


async def set_default_policy(s: Seed, **over: Any) -> None:
    body: dict[str, Any] = {"name": f"P{uuid.uuid4().hex[:4]}", "default_status": "confirmed", "min_notice_booking": 60,
                            "min_notice_cancel": 86400, "booking_window": 60 * 86400, "pending_timeout": 1800, "make_default": True}
    body.update(over)
    assert (await s.client.post("/v1/admin/policies", json=body, headers=s.admin)).status_code == 201


async def customer_id(s: Seed, bid: str) -> uuid.UUID:
    return uuid.UUID((await s.client.get(f"/v1/admin/bookings/{bid}", headers=s.admin)).json()["customer_id"])


# --------------------------------------------------------------------- OTP login
async def test_otp_login_issues_a_customer_token(seeded: Seed, otp: Any) -> None:
    assert (await request_code(seeded)).status_code == 202
    r = await verify(seeded, name="Aisha")
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"
    claims = decode_jwt(r.json()["access_token"])
    assert claims["role"] == "customer" and claims["tid"] == str(seeded.tenant_id) and uuid.UUID(claims["sub"])


async def test_wrong_or_expired_or_reused_codes_are_refused(seeded: Seed, otp: Any) -> None:
    await request_code(seeded)
    assert (await verify(seeded, code="000000")).status_code == 401
    assert (await verify(seeded)).status_code == 200  # the right code still works after one miss...
    assert (await verify(seeded)).status_code == 401  # ...exactly once
    await request_code(seeded)
    await otp.flushall()  # the code expired
    assert (await verify(seeded)).status_code == 401
    assert (await verify(seeded, phone="+60123459990")).status_code == 401  # a code is bound to its phone


async def test_guessing_is_capped_and_a_new_code_does_not_reset_the_cap(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 60)
    assert (await request_code(seeded)).status_code == 202
    assert (await request_code(seeded)).status_code == 429  # one code per cooldown window
    assert (await request_code(seeded, "+60123459999")).status_code == 202  # other phones are independent
    for _ in range(5):
        assert (await verify(seeded, code="000000")).status_code == 401
    locked = await verify(seeded)  # even the right code is refused now
    assert locked.status_code == 403
    assert (await request_code(seeded)).status_code == 429  # and asking for a fresh code cannot lift the lock


async def test_codes_are_not_stored_in_clear(seeded: Seed, otp: Any) -> None:
    await request_code(seeded)
    values = [await otp.get(k) for k in await otp.keys("*")]
    assert values and CODE not in values


async def test_customer_tokens_are_bound_to_their_tenant(client: httpx.AsyncClient, otp: Any) -> None:
    a, b = await make_seed(client), await make_seed(client)
    headers_a = await login(a)
    crossed = {**b.pk, "Authorization": headers_a["Authorization"]}
    assert (await client.get("/v1/public/me/bookings", headers=crossed)).status_code == 403
    assert (await client.get("/v1/public/me/bookings", headers=b.pk)).status_code == 401  # no token at all
    assert (await client.get("/v1/public/me/bookings", headers={**b.pk, "Authorization": f"Bearer {b.admin['Authorization'][7:]}"})).status_code == 403  # staff token


# --------------------------------------------------------------------- cancelling
async def test_a_customer_can_cancel_their_own_booking_and_the_slot_is_freed(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    r = await cancel(seeded, me, bid)
    assert r.status_code == 200 and r.json()["status"] == "cancelled" and r.json()["can_cancel"] is False
    assert all(row[4] == "released" for row in await allocations(seeded, bid))
    again = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert again.status_code == 201  # the freed slot can be booked straight away
    second = await cancel(seeded, me, bid)
    assert second.status_code == 409 and "cancelled" in second.json()["detail"]


async def test_cancelling_after_the_notice_period_is_refused_and_changes_nothing(seeded: Seed, otp: Any) -> None:
    await set_default_policy(seeded, min_notice_cancel=10 * 86400)  # 10 days' notice; the booking is 3 days out
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    r = await cancel(seeded, await login(seeded), bid)
    assert r.status_code == 422 and r.json()["error"] == "too_late_to_cancel"
    deadline = dt.datetime.fromisoformat(r.json()["cancel_deadline"])
    assert deadline == at(seeded.day, 10) - dt.timedelta(days=10)
    assert (await seeded.client.get(f"/v1/admin/bookings/{bid}", headers=seeded.admin)).json()["status"] == "confirmed"
    assert all(row[4] == "active" for row in await allocations(seeded, bid))


async def test_the_notice_boundary_is_inclusive(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    start = at(seeded.day, 10)
    deadline = start - dt.timedelta(hours=24)  # the seeded policy's cancellation notice
    ids = [await book(seeded, mei, at(seeded.day, h)) for h in (10, 12, 15)]  # 10:00, 12:00, 15:00
    async with tenant_session(seeded.tenant_id) as s:
        tenant = await s.get(Tenant, seeded.tenant_id)
        assert tenant is not None
        cust = await customer_id(seeded, ids[0])
        ok = await booking_module.cancel_as_customer(s, tenant, uuid.UUID(ids[0]), cust, now=deadline)  # exactly on the deadline
        assert ok.status == "cancelled"
    async with tenant_session(seeded.tenant_id) as s:
        tenant = await s.get(Tenant, seeded.tenant_id)
        assert tenant is not None
        late = at(seeded.day, 12) - dt.timedelta(hours=24) + dt.timedelta(seconds=1)  # one second past the 12:00 booking's deadline
        with pytest.raises(TooLateToCancel):
            await booking_module.cancel_as_customer(s, tenant, uuid.UUID(ids[1]), cust, now=late)


async def test_pending_requests_can_be_withdrawn_freely_but_confirmed_ones_cannot(client: httpx.AsyncClient, otp: Any) -> None:
    s = await make_seed(client, default_status="pending")
    await set_default_policy(s, default_status="pending", min_notice_cancel=10 * 86400)
    mei = await s.add_staff("Mei")
    me = await login(s)
    withdrawn = await book(s, mei, at(s.day, 10))
    r = await cancel(s, me, withdrawn)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"  # a pending request is no commitment
    kept = await book(s, mei, at(s.day, 12))
    assert (await s.client.patch(f"/v1/admin/bookings/{kept}/status", json={"status": "confirmed"}, headers=s.admin)).status_code == 200
    late = await cancel(s, me, kept)  # once confirmed, the 10-day notice applies to the very same booking
    assert late.status_code == 422 and late.json()["error"] == "too_late_to_cancel"


async def test_started_or_finished_bookings_cannot_be_cancelled_online(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    for status in ("in-progress", "waiting-for-parts", "completed", "rejected", "no-show", "cancelled"):
        async with tenant_session(seeded.tenant_id) as s:
            await s.execute(text("UPDATE bookings SET status = :st WHERE id = :i"), {"st": status, "i": uuid.UUID(bid)})
        r = await cancel(seeded, me, bid)
        assert r.status_code == 409, (status, r.text)


async def test_only_the_owner_can_cancel_and_others_learn_nothing(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    stranger = await login(seeded, "+60199990000")
    assert (await cancel(seeded, stranger, bid)).status_code == 404  # indistinguishable from "does not exist"
    assert (await cancel(seeded, stranger, str(uuid.uuid4()))).status_code == 404
    assert (await cancel(seeded, seeded.pk, bid)).status_code == 401  # a site key alone is not a login
    assert (await seeded.client.post(f"/v1/public/bookings/{bid}/cancel", headers={**seeded.pk, **seeded.admin})).status_code == 403  # staff tokens are not customer tokens
    assert (await seeded.client.get(f"/v1/admin/bookings/{bid}", headers=seeded.admin)).json()["status"] == "confirmed"


async def test_guests_can_manage_their_booking_by_logging_in_with_the_phone_they_booked_with(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))  # booked anonymously: no account, only a phone number
    listing = (await seeded.client.get("/v1/public/me/bookings", headers=await login(seeded))).json()
    assert [b["id"] for b in listing] == [bid]


# --------------------------------------------------------------------- the customer's own list
async def test_my_bookings_lists_only_mine_with_the_cancel_rules_applied(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    later = await book(seeded, mei, at(seeded.day, 15))
    sooner = await book(seeded, mei, at(seeded.day, 10))
    await book(seeded, mei, at(seeded.day, 12), phone="+60177770000")  # somebody else's
    listing = (await seeded.client.get("/v1/public/me/bookings", headers=await login(seeded))).json()
    assert [b["id"] for b in listing] == [sooner, later]  # soonest first, mine only
    first = listing[0]
    assert first["service_names"] == ["Massage"] and first["can_cancel"] is True
    assert dt.datetime.fromisoformat(first["cancel_deadline"]) == at(seeded.day, 10) - dt.timedelta(hours=24)
    assert set(first) == {"id", "status", "start_at", "end_at", "service_names", "party_size", "meeting_link", "can_cancel", "cancel_deadline", "can_reschedule"}


async def test_past_bookings_are_hidden_unless_asked_for(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    async with tenant_session(seeded.tenant_id) as s:
        await s.execute(text("UPDATE bookings SET start_at = now() - interval '2 hours', end_at = now() - interval '1 hour' WHERE id = :i"), {"i": uuid.UUID(bid)})
    me = await login(seeded)
    assert (await seeded.client.get("/v1/public/me/bookings", headers=me)).json() == []
    past = (await seeded.client.get("/v1/public/me/bookings", params={"include_past": "true"}, headers=me)).json()
    assert [b["id"] for b in past] == [bid] and past[0]["can_cancel"] is False  # its cancellation deadline is long gone


# --------------------------------------------------------------------- races
class _Together:
    """Waits until every racer has arrived, then lets them all enter the state machine at once."""

    def __init__(self, n: int) -> None:
        self.barrier = asyncio.Barrier(n)

    def wrap(self, real: Any) -> Any:
        async def racer(*a: Any, **k: Any) -> Any:
            await asyncio.wait_for(self.barrier.wait(), timeout=10)
            return await real(*a, **k)

        return racer


async def test_two_simultaneous_cancellations_have_one_winner(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    with monkeypatch.context() as m:  # scoped: undo() would also remove the fake-Redis patch from the ``otp`` fixture
        m.setattr(booking_module, "transition_booking", _Together(2).wrap(booking_module.transition_booking))
        results = await asyncio.gather(cancel(seeded, me, bid), cancel(seeded, me, bid))
    assert sorted(r.status_code for r in results) == [200, 409]


async def test_approval_racing_a_cancellation_never_leaves_a_status_that_disagrees_with_the_slot(
    client: httpx.AsyncClient, otp: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    for _ in range(3):  # a few rounds: the two orders are both legal, the mixed state is not
        s = await make_seed(client, default_status="pending")
        mei = await s.add_staff("Mei")
        bid = await book(s, mei, at(s.day, 10))
        me = await login(s)
        with monkeypatch.context() as m:
            m.setattr(booking_module, "transition_booking", _Together(2).wrap(booking_module.transition_booking))
            approve, withdraw = await asyncio.gather(
                s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "confirmed"}, headers=s.admin), cancel(s, me, bid)
            )
        assert all(r.status_code in (200, 409) for r in (approve, withdraw)), (approve.text, withdraw.text)
        status = (await s.client.get(f"/v1/admin/bookings/{bid}", headers=s.admin)).json()["status"]
        states = frozenset(row[4] for row in await allocations(s, bid))
        assert (status, states) in {("cancelled", frozenset({"released"})), ("confirmed", frozenset({"active"}))}, (status, states)


def test_can_cancel_online_matches_the_rule_the_state_machine_enforces() -> None:
    from types import SimpleNamespace

    start = at(dt.date(2030, 1, 7), 10)
    confirmed: Any = SimpleNamespace(status="confirmed", start_at=start, policy_snapshot={"min_notice_cancel_s": 86400})
    deadline = start - dt.timedelta(hours=24)
    assert booking_module.cancel_deadline(confirmed) == deadline
    assert booking_module.can_cancel_online(confirmed, deadline) is True  # the boundary itself is allowed
    assert booking_module.can_cancel_online(confirmed, deadline + dt.timedelta(seconds=1)) is False
    pending: Any = SimpleNamespace(status="pending", start_at=start, policy_snapshot={"min_notice_cancel_s": 86400})
    assert booking_module.cancel_deadline(pending) is None and booking_module.can_cancel_online(pending, start) is True
    for status in ("in-progress", "waiting-for-parts", "completed", "cancelled", "rejected", "no-show"):
        started: Any = SimpleNamespace(status=status, start_at=start, policy_snapshot={"min_notice_cancel_s": 86400})
        assert booking_module.can_cancel_online(started, deadline - dt.timedelta(days=30)) is False, status
