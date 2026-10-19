"""Plans and limits (migration 0023): the free tier is the same backend with per-business limits, the paid tier has none."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.exc import IntegrityError

from app.core import ratelimit
from app.core.config import get_settings
from app.core.database import tenant_session
from app.models import Booking, Plan, Tenant
from app.services import booking as booking_module
from app.services import plans
from tests.conftest import PLATFORM, Recorder, Seed, at
from tests.test_country_setup import business, signup
from tests.test_customer_cancel import _Together, otp, request_code  # noqa: F401  (``otp`` is a fixture)
from tests.test_customer_identity import EMAIL, booked_with_email
from tests.test_notifications import log_rows, put_template, run_outbox
from tests.test_salon import Salon, salon  # noqa: F401  (``salon`` is a fixture)
from tests.test_staff_alerts import collide, set_contact
from tests.test_staff_alerts import log_rows as alert_rows


#: The site limits of a plan that has none (a plan made for a test; the paid plan) and of the free plan.
NO_SITE_LIMITS: dict[str, int | None] = {"sites": None, "domains": None, "live_keys_per_kind": None, "domain_change_days": None}
FREE_SITE_LIMITS: dict[str, int | None] = {"sites": 1, "domains": 1, "live_keys_per_kind": 2, "domain_change_days": 30}


async def new_plan(**limits: int | None) -> str:
    """A plan of its own for one test (the shared ``free`` and ``paid`` rows are never touched); ``limits`` are ``resources=2`` and the like."""
    code = "t" + uuid.uuid4().hex[:10]
    async with tenant_session(None) as db:
        await db.execute(insert(Plan).values(code=code, name="Tiny", **{f"max_{k}": v for k, v in limits.items()}))
    return code


async def on(client: httpx.AsyncClient, **limits: int | None) -> Seed:
    """A spa on a plan with just these limits."""
    return await business(client, plan=await new_plan(**limits))


async def usage_of(s: Seed) -> dict[str, Any]:
    r = await s.client.get("/v1/admin/plan", headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def refused(r: httpx.Response, limit: str, allowed: int, used: int) -> None:
    body = r.json()
    assert r.status_code == 402, r.text
    assert (body["error"], body["limit"], body["allowed"], body["used"]) == ("plan_limit_reached", limit, allowed, used), body
    assert str(allowed) in body["detail"]


# --------------------------------------------------------------------- the plans themselves
async def test_the_free_and_paid_plans_exist_with_the_proposed_limits(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/platform/plans", headers=PLATFORM)
    assert r.status_code == 200, r.text
    by_code = {p["code"]: p for p in r.json()}
    assert by_code["free"] == {"code": "free", "name": "Free", "limits": {"resources": 3, "services": 10, "bookings_per_month": 50, "whatsapp_per_month": 20, "sms_per_month": 20, **FREE_SITE_LIMITS}}
    assert by_code["paid"] == {"code": "paid", "name": "Paid", "limits": {k: None for k in ("resources", "services", "bookings_per_month", "whatsapp_per_month", "sms_per_month", *NO_SITE_LIMITS)}}


async def test_whatsapp_and_sms_limits_are_told_apart(client: httpx.AsyncClient) -> None:
    code = await new_plan(whatsapp_per_month=5, sms_per_month=7)
    listed = {p["code"]: p["limits"] for p in (await client.get("/v1/platform/plans", headers=PLATFORM)).json()}
    assert (listed[code]["whatsapp_per_month"], listed[code]["sms_per_month"]) == (5, 7)
    s = await business(client, plan=code)
    assert (await usage_of(s))["plan"]["limits"] == {"resources": None, "services": None, "bookings_per_month": None, "whatsapp_per_month": 5, "sms_per_month": 7, **NO_SITE_LIMITS}


async def test_only_the_platform_operator_sees_the_plans_or_changes_one(client: httpx.AsyncClient) -> None:
    s = await business(client)
    for headers in ({}, {"X-Platform-Token": "wrong"}, {"X-Platform-Token": ""}, s.sk, s.admin):
        assert (await client.get("/v1/platform/plans", headers=headers)).status_code == 401, headers
        assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={"plan": "free"}, headers=headers)).status_code == 401, headers
    assert (await tenant_row(s.tenant_id)).plan == "paid"


async def tenant_row(tenant_id: uuid.UUID) -> Tenant:
    async with tenant_session(None) as db:
        row = await db.get(Tenant, tenant_id)
    assert row is not None
    return row


def test_platform_calls_are_limited_like_onboarding() -> None:
    assert ratelimit.classify("PUT", "/v1/platform/tenants/x/plan") == "platform" and ratelimit.classify("GET", "/v1/platform/plans") == "platform"
    assert ratelimit.classify("GET", "/v1/platformx") is None


async def test_the_database_refuses_a_negative_limit(client: httpx.AsyncClient) -> None:
    with pytest.raises(IntegrityError):
        await new_plan(resources=-1)
    assert await new_plan(resources=0, services=None)  # none at all, or no limit, are fine


# --------------------------------------------------------------------- which plan a business is on
async def test_a_business_set_up_by_the_operator_is_paid_unless_the_request_says_free(client: httpx.AsyncClient) -> None:
    assert (await tenant_row(uuid.UUID((await signup(client)).json()["tenant_id"]))).plan == "paid"
    assert (await tenant_row(uuid.UUID((await signup(client, plan="free")).json()["tenant_id"]))).plan == "free"


@pytest.mark.parametrize("plan", ["gold", "Free", "", "free plan", "x" * 21])
async def test_an_unknown_plan_stops_the_sign_up(client: httpx.AsyncClient, plan: str) -> None:
    name = f"Refused {uuid.uuid4().hex[:6]}"
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": name, "plan": plan})
    assert r.status_code == 422, r.text
    async with tenant_session(None) as db:
        assert await db.scalar(select(Tenant.id).where(Tenant.name == name)) is None


async def test_the_operator_moves_a_business_between_plans_at_once(client: httpx.AsyncClient) -> None:
    s = await business(client, plan="free")
    for n in range(3):
        await s.add_staff(f"Mei{n}")
    assert (await client.post("/v1/admin/resources", json={"type": "staff", "name": "Four"}, headers=s.admin)).status_code == 402
    moved = await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={"plan": "paid"}, headers=PLATFORM)
    assert moved.status_code == 200 and moved.json() == {"tenant_id": str(s.tenant_id), "plan": "paid"}
    assert (await client.post("/v1/admin/resources", json={"type": "staff", "name": "Four"}, headers=s.admin)).status_code == 201  # no new token, no waiting
    assert (await tenant_row(s.tenant_id)).plan == "paid"


async def test_a_move_to_a_plan_that_does_not_exist_or_for_a_business_that_does_not_is_refused(client: httpx.AsyncClient) -> None:
    s = await business(client)
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={"plan": "gold"}, headers=PLATFORM)).status_code == 422
    assert (await client.put(f"/v1/platform/tenants/{uuid.uuid4()}/plan", json={"plan": "free"}, headers=PLATFORM)).status_code == 404
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={"plan": "free", "x": 1}, headers=PLATFORM)).status_code == 422
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={}, headers=PLATFORM)).status_code == 422
    assert (await tenant_row(s.tenant_id)).plan == "paid"


# --------------------------------------------------------------------- GET /admin/plan
async def test_the_business_can_see_its_plan_what_it_allows_and_what_it_uses(client: httpx.AsyncClient) -> None:
    s = await business(client, plan="free")
    await s.add_staff("Mei")
    got = await usage_of(s)
    assert got["plan"] == {"code": "free", "name": "Free", "limits": {"resources": 3, "services": 10, "bookings_per_month": 50, "whatsapp_per_month": 20, "sms_per_month": 20, **FREE_SITE_LIMITS}}
    assert got["usage"] == {"resources": 1, "services": 1, "bookings_per_month": 0, "whatsapp_per_month": 0, "sms_per_month": 0, "sites": 1, "domains": 1, "live_keys_per_kind": 1}
    assert got["over_limit"] == []
    assert (await client.get("/v1/admin/plan", headers=await s.token("manager"))).status_code == 200
    assert (await client.get("/v1/admin/plan", headers=await s.token("staff"))).status_code == 403
    assert (await client.get("/v1/admin/plan", headers=s.pk)).status_code == 401


async def test_one_business_never_sees_anothers_usage(client: httpx.AsyncClient) -> None:
    a, b = await business(client, plan="free"), await business(client, plan="free")
    await a.add_staff("Mei")
    await a.add_staff("Lin")
    assert (await usage_of(a))["usage"]["resources"] == 2 and (await usage_of(b))["usage"]["resources"] == 0


# --------------------------------------------------------------------- resources and services
async def test_a_free_business_may_have_three_active_staff_and_no_more(client: httpx.AsyncClient) -> None:
    s = await business(client, plan="free")
    for n in range(3):
        await s.add_staff(f"Mei{n}")
    r = await client.post("/v1/admin/resources", json={"type": "staff", "name": "Four"}, headers=s.admin)
    refused(r, "max_resources", 3, 3)
    assert "Free plan" in r.json()["detail"] and r.json()["plan"] == "free"
    assert (await usage_of(s))["usage"]["resources"] == 3  # nothing was added
    assert len((await client.get("/v1/admin/resources", headers=s.admin)).json()) == 3


async def test_tables_and_bays_count_like_staff(client: httpx.AsyncClient) -> None:
    s = await on(client, resources=2)
    assert (await client.post("/v1/admin/resources", json={"type": "table", "name": "T1", "capacity": 2, "attributes": {}}, headers=s.admin)).status_code == 201
    assert (await client.post("/v1/admin/resources", json={"type": "bay", "name": "Bay"}, headers=s.admin)).status_code == 201
    refused(await client.post("/v1/admin/resources", json={"type": "staff", "name": "Mei"}, headers=s.admin), "max_resources", 2, 2)


async def test_deactivating_a_resource_gives_its_place_back_and_reactivating_takes_it_again(client: httpx.AsyncClient) -> None:
    s = await on(client, resources=2)
    a, b = await s.add_staff("Mei"), await s.add_staff("Lin")
    off = lambda rid, to: client.patch(f"/v1/admin/resources/{rid}", json={"status": to}, headers=s.admin)  # noqa: E731
    assert (await off(a, "inactive")).status_code == 200
    c = (await client.post("/v1/admin/resources", json={"type": "staff", "name": "Ola"}, headers=s.admin))
    assert c.status_code == 201  # the place was free again
    refused(await off(a, "active"), "max_resources", 2, 2)  # switching Mei back on needs a place, and there is none
    assert (await usage_of(s))["usage"]["resources"] == 2
    assert (await off(b, "active")).status_code == 200  # (already active: not a new place)
    assert (await client.patch(f"/v1/admin/resources/{b}", json={"name": "Lin 2"}, headers=s.admin)).status_code == 200  # renaming is not adding
    assert (await off(c.json()["id"], "inactive")).status_code == 200
    assert (await off(a, "active")).status_code == 200


async def test_a_resource_made_inactive_from_the_start_takes_no_place(client: httpx.AsyncClient) -> None:
    s = await on(client, resources=1)
    await s.add_staff("Mei")
    assert (await client.post("/v1/admin/resources", json={"type": "staff", "name": "Later", "status": "inactive"}, headers=s.admin)).status_code == 201


async def test_a_limit_of_none_at_all_refuses_the_first(client: httpx.AsyncClient) -> None:
    s = await on(client, resources=0)
    refused(await client.post("/v1/admin/resources", json={"type": "staff", "name": "Mei"}, headers=s.admin), "max_resources", 0, 0)


async def test_services_are_limited_the_same_way(client: httpx.AsyncClient) -> None:
    s = await on(client, services=2)  # (the helper made one already)
    make = lambda **b: client.post("/v1/admin/services", json={"name": f"S{uuid.uuid4().hex[:4]}", "duration_minutes": 30, **b}, headers=s.admin)  # noqa: E731
    first = await make()
    assert first.status_code == 201
    refused(await make(), "max_services", 2, 2)
    assert (await make(active=False)).status_code == 201  # inactive from the start: no place taken
    assert (await client.patch(f"/v1/admin/services/{first.json()['id']}", json={"active": False}, headers=s.admin)).status_code == 200
    assert (await make()).status_code == 201
    inactive = (await client.get("/v1/admin/services", headers=s.admin)).json()
    spare = next(x["id"] for x in inactive if not x["active"])
    refused(await client.patch(f"/v1/admin/services/{spare}", json={"active": True}, headers=s.admin), "max_services", 2, 2)
    assert (await client.patch(f"/v1/admin/services/{spare}", json={"name": "Renamed"}, headers=s.admin)).status_code == 200
    on_already = (await client.get("/v1/admin/services", headers=s.admin)).json()
    live = next(x["id"] for x in on_already if x["active"])
    assert (await client.patch(f"/v1/admin/services/{live}", json={"active": True}, headers=s.admin)).status_code == 200  # saying "on" to one that is on takes no new place
    assert (await usage_of(s))["usage"]["services"] == 2


async def test_a_paid_business_has_no_limits(client: httpx.AsyncClient) -> None:
    s = await business(client)
    for n in range(6):
        await s.add_staff(f"Mei{n}")
    assert (await usage_of(s))["usage"]["resources"] == 6 and (await usage_of(s))["plan"]["limits"]["resources"] is None


# --------------------------------------------------------------------- bookings a month
async def book(s: Seed, hour: int, staff: uuid.UUID | None = None, day: dt.date | None = None) -> httpx.Response:
    staff = staff or await s.add_staff(f"Mei{uuid.uuid4().hex[:4]}")
    return await s.client.post("/v1/public/bookings", json=s.booking_body(at(day or s.day, hour), staff), headers=s.pk)


async def test_a_business_may_take_its_months_bookings_and_no_more(client: httpx.AsyncClient) -> None:
    s = await on(client, bookings_per_month=2)
    assert (await book(s, 10)).status_code == 201 and (await book(s, 11)).status_code == 201
    r = await book(s, 12)
    refused(r, "max_bookings_per_month", 2, 2)
    listed = (await s.client.get("/v1/admin/bookings", headers=s.admin)).json()
    assert len(listed) == 2  # the refused one left nothing behind
    assert (await usage_of(s))["usage"]["bookings_per_month"] == 2


async def test_a_cancelled_booking_frees_its_place(client: httpx.AsyncClient) -> None:
    s = await on(client, bookings_per_month=1)
    first = (await book(s, 10)).json()["id"]
    refused(await book(s, 11), "max_bookings_per_month", 1, 1)
    cancelled = await s.client.patch(f"/v1/admin/bookings/{first}/status", json={"status": "cancelled"}, headers=s.admin)
    assert cancelled.status_code == 200, cancelled.text
    assert (await usage_of(s))["usage"]["bookings_per_month"] == 0
    assert (await book(s, 11)).status_code == 201


async def test_a_pending_request_that_times_out_frees_its_place_for_the_very_next_booking(client: httpx.AsyncClient) -> None:
    """A booking nobody answered is rejected once its hold lapses; the next request must find its place free (the lapsed hold is released before the count)."""
    from tests.conftest import make_seed

    s = await make_seed(client, default_status="pending")
    async with tenant_session(None) as db:
        await db.execute(update(Tenant).where(Tenant.id == s.tenant_id).values(plan=await new_plan(bookings_per_month=1)))
    assert (await book(s, 10)).status_code == 201
    refused(await book(s, 11), "max_bookings_per_month", 1, 1)  # (the hold is still running)
    async with tenant_session(s.tenant_id) as db:
        await db.execute(update(Booking).values(hold_expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)))  # it lapses; nothing has swept it yet
    assert (await book(s, 11)).status_code == 201


async def test_last_months_bookings_do_not_count(client: httpx.AsyncClient) -> None:
    s = await on(client, bookings_per_month=1)
    assert (await book(s, 10)).status_code == 201
    refused(await book(s, 11), "max_bookings_per_month", 1, 1)
    async with tenant_session(s.tenant_id) as db:
        await db.execute(update(Booking).values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=40)))
    assert (await usage_of(s))["usage"]["bookings_per_month"] == 0
    assert (await book(s, 11)).status_code == 201


async def test_two_requests_for_the_last_place_cannot_both_have_it(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await on(client, bookings_per_month=1)
    one, two = await s.add_staff("Mei"), await s.add_staff("Lin")
    together = _Together(2)
    with monkeypatch.context() as m:
        m.setattr(plans, "ensure_can_book", together.wrap(plans.ensure_can_book))  # both arrive before either has counted
        results = await asyncio.gather(book(s, 10, one), book(s, 11, two))
    assert sorted(r.status_code for r in results) == [201, 402], [r.text for r in results]
    assert (await usage_of(s))["usage"]["bookings_per_month"] == 1


async def test_every_business_type_counts_its_bookings(client: httpx.AsyncClient) -> None:
    from tests.test_restaurant import body as rbody
    from tests.test_restaurant import make_restaurant

    rest = await make_restaurant(client)
    async with tenant_session(None) as db:
        await db.execute(update(Tenant).where(Tenant.id == rest.tenant_id).values(plan=await new_plan(bookings_per_month=1)))
    assert (await client.post("/v1/public/bookings", json=rbody(rest, at(rest.day, 12), 2), headers=rest.pk)).status_code == 201
    refused(await client.post("/v1/public/bookings", json=rbody(rest, at(rest.day, 14), 2), headers=rest.pk), "max_bookings_per_month", 1, 1)


async def test_a_follow_up_booked_by_staff_counts_too(client: httpx.AsyncClient) -> None:
    from tests.test_field_service import make_practice, pbook

    p = await make_practice(client)
    async with tenant_session(None) as db:
        await db.execute(update(Tenant).where(Tenant.id == p.tenant_id).values(plan=await new_plan(bookings_per_month=1)))
    inspection = (await pbook(p, at(p.day, 10), "office")).json()["id"]
    assert (await p.client.patch(f"/v1/admin/bookings/{inspection}/status", json={"status": "in-progress"}, headers=p.admin)).status_code == 200
    later = p.day + dt.timedelta(days=1)
    r = await p.client.post(f"/v1/admin/bookings/{inspection}/follow-up", json={"service_id": str(p.svc["work"]), "start_at": at(later, 10).isoformat()}, headers=p.admin)
    refused(r, "max_bookings_per_month", 1, 1)


# --------------------------------------------------------------------- the month, in the business's zone (pure)
@pytest.mark.parametrize(("zone", "now", "lo", "hi"), [
    ("Asia/Kuala_Lumpur", "2026-10-15T00:00:00+00:00", "2026-09-30T16:00:00+00:00", "2026-10-31T16:00:00+00:00"),
    ("Australia/Sydney", "2026-09-30T14:30:00+00:00", "2026-09-30T14:00:00+00:00", "2026-10-31T13:00:00+00:00"),  # 00:30 on 1 October there; the clocks changed on the 4th
    ("Australia/Sydney", "2026-09-30T13:30:00+00:00", "2026-08-31T14:00:00+00:00", "2026-09-30T14:00:00+00:00"),  # 23:30 on 30 September there
    ("Asia/Kuala_Lumpur", "2026-12-15T00:00:00+00:00", "2026-11-30T16:00:00+00:00", "2026-12-31T16:00:00+00:00"),
    ("Asia/Kuala_Lumpur", "2026-12-31T16:00:00+00:00", "2026-12-31T16:00:00+00:00", "2027-01-31T16:00:00+00:00"),  # the first instant of January belongs to January, across a year end
])
def test_a_month_runs_from_midnight_to_midnight_on_the_businesss_clock(zone: str, now: str, lo: str, hi: str) -> None:
    tenant: Any = SimpleNamespace(timezone=zone)
    got = plans.month_bounds(tenant, dt.datetime.fromisoformat(now))
    assert (got[0].isoformat(), got[1].isoformat()) == (lo, hi)


def test_a_limit_is_over_only_when_exceeded() -> None:
    limits = plans.Limits(resources=3, services=None, bookings_per_month=0, whatsapp_per_month=5, sms_per_month=5)
    assert plans.over_limit(limits, plans.Usage(resources=3, services=99, bookings_per_month=0, whatsapp_per_month=5, sms_per_month=5)) == []
    assert plans.over_limit(limits, plans.Usage(resources=4, services=0, bookings_per_month=1, whatsapp_per_month=6, sms_per_month=5)) == [
        "resources", "bookings_per_month", "whatsapp_per_month"]


async def test_a_business_moved_to_a_smaller_plan_keeps_what_it_has_and_is_told_what_is_over(client: httpx.AsyncClient) -> None:
    s = await business(client)
    ids = [await s.add_staff(f"Mei{n}") for n in range(5)]
    assert (await book(s, 10, ids[0])).status_code == 201
    await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={"plan": "free"}, headers=PLATFORM)
    got = await usage_of(s)
    assert got["usage"]["resources"] == 5 and got["over_limit"] == ["resources"]
    assert len((await client.get("/v1/admin/resources", headers=s.admin)).json()) == 5  # nothing was removed
    assert (await book(s, 11, ids[1])).status_code == 201  # and what it already has keeps working
    refused(await client.post("/v1/admin/resources", json={"type": "staff", "name": "Six"}, headers=s.admin), "max_resources", 3, 5)
    assert (await client.patch(f"/v1/admin/resources/{ids[0]}", json={"status": "inactive"}, headers=s.admin)).status_code == 200
    assert (await client.patch(f"/v1/admin/resources/{ids[1]}", json={"status": "inactive"}, headers=s.admin)).status_code == 200
    assert (await usage_of(s))["over_limit"] == []


# --------------------------------------------------------------------- messages
async def book_for_messages(s: Seed, hour: int) -> str:
    return await booked_with_email(s, hour=hour, phone=f"+6012{uuid.uuid4().int % 10**7:07d}")


async def test_sms_over_the_plans_limit_is_skipped_and_email_carries_on(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, sms_per_month=1)
    assert (await put_template(s, "booking.confirmed", "sms", body="Booked $time")).status_code == 200
    first, second = await book_for_messages(s, 10), await book_for_messages(s, 11)
    assert (await run_outbox(s)).failed == 0
    assert sorted(m.channel for m in mail.sent) == ["email", "email", "sms"]  # one text, both emails
    rows = {(r.booking_id and str(r.booking_id), r.channel): r for r in await log_rows(s)}
    assert rows[(first, "sms")].status == "sent" and rows[(second, "sms")].status == "skipped"
    assert rows[(second, "sms")].error == "plan limit: this month's sms messages are used up" and rows[(second, "email")].status == "sent"
    await run_outbox(s)
    assert len(mail.to("sms")) == 1  # a skipped message is not tried again
    assert (await usage_of(s))["usage"]["sms_per_month"] == 1


async def test_a_plan_with_no_whatsapp_sends_none(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, whatsapp_per_month=0)
    assert (await put_template(s, "booking.confirmed", "whatsapp", body="x", external_template="booking_confirmed")).status_code == 200
    bid = await book_for_messages(s, 10)
    await run_outbox(s)
    assert [m.channel for m in mail.sent] == ["email"]
    (row,) = [r for r in await log_rows(s, bid) if r.channel == "whatsapp"]
    assert row.status == "skipped" and "plan limit" in (row.error or "")


async def test_a_paid_business_sends_as_many_as_it_likes(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await business(client)
    assert (await put_template(s, "booking.confirmed", "sms", body="Booked $time")).status_code == 200
    for hour in (10, 11, 12):
        await book_for_messages(s, hour)
    await run_outbox(s)
    assert len(mail.to("sms")) == 3


async def test_last_months_messages_do_not_count(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, sms_per_month=1)
    await put_template(s, "booking.confirmed", "sms", body="Booked $time")
    await book_for_messages(s, 10)
    await run_outbox(s)
    assert len(mail.to("sms")) == 1
    async with tenant_session(s.tenant_id) as db:
        await db.execute(text("UPDATE notification_log SET sent_at = now() - interval '40 days'"))
    assert (await usage_of(s))["usage"]["sms_per_month"] == 0
    await book_for_messages(s, 11)
    await run_outbox(s)
    assert len(mail.to("sms")) == 2


async def test_a_failed_attempt_does_not_use_up_a_message(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, sms_per_month=1)
    await put_template(s, "booking.confirmed", "sms", body="Booked $time")
    mail.failing["sms"] = RuntimeError("gateway down")
    await book_for_messages(s, 10)
    await run_outbox(s)
    assert (await usage_of(s))["usage"]["sms_per_month"] == 0  # nothing went out, nothing is counted
    mail.failing.clear()
    await run_outbox(s, now=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    assert len(mail.to("sms")) == 1


async def test_a_sign_in_code_over_the_limit_is_refused_so_the_customer_uses_the_email_link(client: httpx.AsyncClient, otp: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 0)
    s = await on(client, whatsapp_per_month=1)
    assert (await request_code(s, "012-345 0001")).status_code == 202
    assert len(mail.to("whatsapp")) == 1
    r = await request_code(s, "012-345 0002")
    refused(r, "max_whatsapp_per_month", 1, 1)
    assert len(mail.to("whatsapp")) == 1 and len([k for k in await otp.keys("otp:*") if not k.endswith(":day")]) == 1  # only the first customer's code exists (the day counters are not codes): none was left behind for the second
    await booked_with_email(s)  # a customer with an email address can still get a sign-in link
    link = await s.client.post("/v1/public/auth/magic-link", json={"email": EMAIL}, headers=s.pk)
    assert link.status_code == 202 and len(mail.to("email")) >= 1


async def test_the_sign_in_code_counts_as_a_message(client: httpx.AsyncClient, otp: Any, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "otp_channel", "sms")
    s = await on(client, sms_per_month=1)
    assert (await request_code(s)).status_code == 202
    assert (await usage_of(s))["usage"] == {"resources": 0, "services": 1, "bookings_per_month": 0, "whatsapp_per_month": 0, "sms_per_month": 1, "sites": 1, "domains": 0, "live_keys_per_kind": 1}


async def test_the_stylists_text_is_held_back_by_the_plan_but_the_email_is_not(salon: Salon, mail: Recorder) -> None:
    await set_contact(salon, salon.stylists["Mei"], contact_email="mei@example.com", contact_phone="012-345 0001")
    async with tenant_session(None) as db:
        await db.execute(update(Tenant).where(Tenant.id == salon.tenant_id).values(plan=await new_plan(sms_per_month=0)))
    await collide(salon)
    await run_outbox(salon)
    assert [m.channel for m in mail.sent] == ["email"]
    rows = {r.channel: r for r in await alert_rows(salon)}
    assert rows["email"].status == "sent" and rows["sms"].status == "skipped" and "plan limit" in (rows["sms"].error or "")


# --------------------------------------------------------------------- the pure parts
def test_a_plan_is_described_by_its_limits() -> None:
    plan = Plan(code="x", name="X", max_resources=1, max_services=2, max_bookings_per_month=3, max_whatsapp_per_month=None, max_sms_per_month=0)
    assert plans.limits_of(plan) == plans.Limits(1, 2, 3, None, 0)
    assert plans.METERED == {"whatsapp": "max_whatsapp_per_month", "sms": "max_sms_per_month"}
    assert set(plans.LABELS) == {"max_resources", "max_services", "max_bookings_per_month", "max_whatsapp_per_month", "max_sms_per_month", "max_sites", "max_domains", "max_live_keys_per_kind"}
