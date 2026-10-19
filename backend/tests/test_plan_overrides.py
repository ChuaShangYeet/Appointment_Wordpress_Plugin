"""Per-business limits (migration 0026) and the warnings a business gets before it hits one."""
from __future__ import annotations

import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import select, text

from app.core.config import Settings, get_settings
from app.core.database import tenant_session
from app.models import Plan, PlanWarning
from app.models.events import OutboxEvent
from app.models.webhooks import WEBHOOK_EVENTS
from app.schemas.plans import PlanOverridesIn
from app.services import plans
from app.services.plans import Limits, Usage
from tests.conftest import PLATFORM, Recorder, Seed, at
from tests.test_customer_cancel import otp, request_code  # noqa: F401  (``otp`` is a fixture)
from tests.test_notifications import log_rows, run_outbox
from tests.test_plans import NO_SITE_LIMITS, new_plan, on, refused, tenant_row, usage_of

NAMES = ("resources", "services", "bookings_per_month", "whatsapp_per_month", "sms_per_month", "sites", "domains", "live_keys_per_kind")


def plan(**limits: int | None) -> Any:
    return SimpleNamespace(**{f"max_{n}": limits.get(n) for n in NAMES}, domain_change_days=limits.get("domain_change_days"), code="p", name="Tiny")


def tenant_with(overrides: dict[str, Any] | None = None, tz: str = "Asia/Kuala_Lumpur") -> Any:
    return SimpleNamespace(plan_overrides=overrides, timezone=tz, id=uuid.uuid4())


def limits(**kw: int | None) -> Limits:
    return Limits(**{n: kw.get(n) for n in NAMES})


def usage(**kw: int) -> Usage:
    return Usage(**{n: kw.get(n, 0) for n in NAMES})


# --------------------------------------------------------------------- pure: which number applies
def test_a_limit_set_for_the_business_beats_the_plans() -> None:
    p = plan(resources=3, services=10)
    t = tenant_with({"resources": 7})
    assert plans.limit_value(p, t, "resources") == 7 and plans.limit_value(p, t, "services") == 10  # the other is the plan's


def test_null_means_no_limit_and_zero_means_none() -> None:
    p = plan(resources=3)
    assert plans.limit_value(p, tenant_with({"resources": None}), "resources") is None
    assert plans.limit_value(p, tenant_with({"resources": 0}), "resources") == 0
    assert plans.limit_value(plan(resources=None), tenant_with({"resources": 2}), "resources") == 2  # a limit on an unlimited plan


@pytest.mark.parametrize("overrides", [None, {}])
def test_without_overrides_the_plan_applies(overrides: Any) -> None:
    p = plan(resources=3, sms_per_month=9)
    assert plans.limits_of(p, tenant_with(overrides)) == plans.limits_of(p) == Limits(3, None, None, None, 9)
    assert plans.overrides_of(tenant_with(overrides)) == {} and plans.overrides_of(None) == {}


def test_things_that_are_not_limits_are_ignored() -> None:
    assert plans.overrides_of(tenant_with({"resources": 4, "colour": 1, "max_services": 2})) == {"resources": 4}
    assert plans.limits_of(plan(services=5), tenant_with({"max_services": 99})).services == 5


def test_every_limit_can_be_overridden() -> None:
    values = {n: i + 1 for i, n in enumerate(NAMES)}
    assert plans.limits_of(plan(), tenant_with(values)) == Limits(1, 2, 3, 4, 5, 6, 7, 8)
    assert plans.LIMIT_NAMES == NAMES and plans.MONTHLY == {"bookings_per_month", "whatsapp_per_month", "sms_per_month"}


def test_the_month_label_is_the_businesss_own_month() -> None:
    moment = dt.datetime(2026, 9, 30, 14, 30, tzinfo=dt.UTC)  # already October in Sydney, still September in Kuala Lumpur
    assert plans.month_label(tenant_with(tz="Australia/Sydney"), moment) == "2026-10"
    assert plans.month_label(tenant_with(tz="Asia/Kuala_Lumpur"), moment) == "2026-09"
    assert plans.month_label(tenant_with(), dt.datetime(2027, 1, 1, tzinfo=dt.UTC)) == "2027-01"


# --------------------------------------------------------------------- pure: when to warn
@pytest.mark.parametrize("used, allowed, level", [
    (0, 10, None), (7, 10, None), (8, 10, "near"), (9, 10, "near"), (10, 10, "reached"), (11, 10, "reached"),
    (1, 3, None), (2, 3, None), (3, 3, "reached"), (0, 1, None), (1, 1, "reached"), (4, 5, "near"), (3, 5, None),  # (80% of 3 rounds up to 3: the limit itself)
    (5, 0, None), (0, 0, None), (50, None, None),
])
def test_the_warning_level_at_eighty_percent(used: int, allowed: int | None, level: str | None) -> None:
    assert plans.warning_level(used, allowed) == level


def test_the_warning_percentage_is_a_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "plan_warning_percent", 50)
    assert [plans.warning_level(n, 10) for n in (4, 5, 9, 10)] == [None, "near", "near", "reached"]
    monkeypatch.setattr(get_settings(), "plan_warning_percent", 99)
    assert [plans.warning_level(n, 100) for n in (98, 99, 100)] == [None, "near", "reached"]
    monkeypatch.setattr(get_settings(), "plan_warning_percent", 0)
    assert plans.warning_level(10, 10) is None and plans.warning_level(9, 10) is None  # (0 switches warnings off, even at the limit)


def test_the_percentage_setting_has_bounds() -> None:
    assert Settings(_env_file=None).plan_warning_percent == 80  # type: ignore[call-arg]
    for bad in (-1, 100):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, plan_warning_percent=bad)  # type: ignore[call-arg]


def test_the_admin_list_of_warnings_follows_the_same_rule() -> None:
    out = plans.warnings(limits(resources=10, services=3, sms_per_month=20, bookings_per_month=10), usage(resources=8, services=3, sms_per_month=2, bookings_per_month=7))
    assert out == [
        {"limit": "resources", "level": "near", "used": 8, "allowed": 10, "percent": 80},
        {"limit": "services", "level": "reached", "used": 3, "allowed": 3, "percent": 100},
    ]  # (7 of 10 is not yet near; 2 of 20 is far from it)
    assert plans.warnings(limits(resources=7), usage(resources=6)) == [{"limit": "resources", "level": "near", "used": 6, "allowed": 7, "percent": 86}]  # (85.7 rounds, it is not cut)
    assert plans.warnings(limits(sms_per_month=10), usage(sms_per_month=9)) == [{"limit": "sms_per_month", "level": "near", "used": 9, "allowed": 10, "percent": 90}]  # (every limit is looked at, the last included)
    assert plans.warnings(limits(whatsapp_per_month=10), usage(whatsapp_per_month=10))[0]["limit"] == "whatsapp_per_month"
    assert plans.warnings(limits(resources=10), usage(resources=14))[0]["percent"] == 100  # (over the limit is still 100)


# --------------------------------------------------------------------- the operator sets them
async def set_overrides(client: httpx.AsyncClient, s: Seed, **body: Any) -> httpx.Response:
    return await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", json=body, headers=PLATFORM)


async def test_only_the_operator_sets_or_reads_a_businesss_limits(client: httpx.AsyncClient) -> None:
    s = await on(client, resources=2)
    for headers in ({}, {"X-Platform-Token": "wrong"}, s.sk, s.admin):
        assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", json={"resources": 9}, headers=headers)).status_code == 401, headers
        assert (await client.get(f"/v1/platform/tenants/{s.tenant_id}/plan", headers=headers)).status_code == 401, headers
    assert (await tenant_row(s.tenant_id)).plan_overrides == {}


async def test_an_unknown_business_is_not_found(client: httpx.AsyncClient) -> None:
    assert (await client.put(f"/v1/platform/tenants/{uuid.uuid4()}/overrides", json={"resources": 1}, headers=PLATFORM)).status_code == 404
    assert (await client.get(f"/v1/platform/tenants/{uuid.uuid4()}/plan", headers=PLATFORM)).status_code == 404


@pytest.mark.parametrize("body", [{"resources": -1}, {"sms_per_month": -5}, {"resources": "many"}, {"colour": 1}, {"max_resources": 3}, {"resources": 1.5}])
async def test_nonsense_limits_are_refused(client: httpx.AsyncClient, body: dict[str, Any]) -> None:
    s = await on(client, resources=2)
    r = await set_overrides(client, s, **body)
    assert r.status_code == 422 and (await tenant_row(s.tenant_id)).plan_overrides == {}


async def test_the_set_replaces_the_whole_set(client: httpx.AsyncClient) -> None:
    s = await on(client, resources=2, services=5)
    r = await set_overrides(client, s, resources=9, services=None)
    assert r.status_code == 200
    body = r.json()
    assert body["overrides"] == {"resources": 9, "services": None}  # null = no limit, and it is kept as a limit that was set
    assert body["plan_limits"] == {"resources": 2, "services": 5, "bookings_per_month": None, "whatsapp_per_month": None, "sms_per_month": None, **NO_SITE_LIMITS}
    assert body["limits"] == {"resources": 9, "services": None, "bookings_per_month": None, "whatsapp_per_month": None, "sms_per_month": None, **NO_SITE_LIMITS}
    again = await set_overrides(client, s, sms_per_month=4)  # a second set replaces the first
    assert again.json()["overrides"] == {"sms_per_month": 4} and again.json()["limits"]["resources"] == 2 and again.json()["limits"]["services"] == 5
    assert (await tenant_row(s.tenant_id)).plan_overrides == {"sms_per_month": 4}
    cleared = await set_overrides(client, s)
    assert cleared.json()["overrides"] == {} and cleared.json()["limits"] == cleared.json()["plan_limits"]


async def test_the_operator_sees_the_whole_picture(client: httpx.AsyncClient) -> None:
    s = await on(client, services=2, resources=None)
    await set_overrides(client, s, services=3)
    r = await client.get(f"/v1/platform/tenants/{s.tenant_id}/plan", headers=PLATFORM)
    assert r.status_code == 200
    body = r.json()
    assert body["tenant_id"] == str(s.tenant_id) and body["plan"].startswith("t") and body["overrides"] == {"services": 3}
    assert body["usage"] == {"resources": 0, "services": 1, "bookings_per_month": 0, "whatsapp_per_month": 0, "sms_per_month": 0, "sites": 1, "domains": 0, "live_keys_per_kind": 1}
    assert body["over_limit"] == [] and body["warnings"] == [] and body["limits"]["services"] == 3 and body["plan_limits"]["services"] == 2


async def test_a_limit_set_below_what_the_business_has_is_shown_as_over(client: httpx.AsyncClient) -> None:
    s = await on(client, services=None)
    assert (await s.client.post("/v1/admin/services", json={"name": "Facial", "duration_minutes": 30}, headers=s.admin)).status_code == 201
    body = (await set_overrides(client, s, services=1)).json()
    assert body["over_limit"] == ["services"] and body["warnings"] == [{"limit": "services", "level": "reached", "used": 2, "allowed": 1, "percent": 100}]


def test_the_body_model_tells_a_limit_left_out_from_one_set_to_null() -> None:
    assert PlanOverridesIn.model_validate({"resources": None, "services": 2}).model_dump(exclude_unset=True) == {"resources": None, "services": 2}
    assert PlanOverridesIn.model_validate({}).model_dump(exclude_unset=True) == {}


# --------------------------------------------------------------------- they take effect
async def add_service(s: Seed, name: str) -> httpx.Response:
    return await s.client.post("/v1/admin/services", json={"name": name, "duration_minutes": 30}, headers=s.admin)


async def test_a_higher_limit_lets_a_business_add_more(client: httpx.AsyncClient) -> None:
    s = await on(client, services=2)  # (one service exists already)
    assert (await add_service(s, "B")).status_code == 201
    refused(await add_service(s, "C"), "max_services", 2, 2)
    await set_overrides(client, s, services=4)
    assert (await add_service(s, "C")).status_code == 201 and (await add_service(s, "D")).status_code == 201
    refused(await add_service(s, "E"), "max_services", 4, 4)
    assert (await usage_of(s))["plan"]["limits"]["services"] == 4


async def test_a_lower_limit_stops_a_business_sooner(client: httpx.AsyncClient) -> None:
    s = await on(client, services=10)
    await set_overrides(client, s, services=2)
    assert (await add_service(s, "B")).status_code == 201
    r = await add_service(s, "C")
    refused(r, "max_services", 2, 2)
    assert "with limits set for your business" in r.json()["detail"]  # and it says that the number is not the plan's


async def test_no_limit_at_all_lifts_a_plans_limit(client: httpx.AsyncClient) -> None:
    s = await on(client, services=2)
    await set_overrides(client, s, services=None)
    for name in "BCDEFG":
        assert (await add_service(s, name)).status_code == 201
    assert (await usage_of(s))["plan"]["limits"]["services"] is None


async def test_a_limit_of_zero_means_none_at_all(client: httpx.AsyncClient) -> None:
    s = await on(client, resources=None)
    await set_overrides(client, s, resources=0)
    refused(await s.client.post("/v1/admin/resources", json={"type": "staff", "name": "Mei"}, headers=s.admin), "max_resources", 0, 0)


async def test_the_plan_is_unchanged_for_a_business_without_overrides(client: httpx.AsyncClient) -> None:
    s = await on(client, services=2)
    other = await on(client, services=2)
    await set_overrides(client, other, services=9)
    assert (await add_service(s, "B")).status_code == 201
    plain = await add_service(s, "C")
    refused(plain, "max_services", 2, 2)  # (the other business's number is not shared)
    assert "with limits set" not in plain.json()["detail"]  # and nothing is said about limits that were never set


async def test_a_resource_limit_can_be_set_for_one_business(client: httpx.AsyncClient) -> None:
    s = await on(client, resources=1)
    await set_overrides(client, s, resources=2)
    assert (await s.client.post("/v1/admin/resources", json={"type": "staff", "name": "Mei"}, headers=s.admin)).status_code == 201
    assert (await s.client.post("/v1/admin/resources", json={"type": "staff", "name": "Lin"}, headers=s.admin)).status_code == 201
    refused(await s.client.post("/v1/admin/resources", json={"type": "staff", "name": "Kim"}, headers=s.admin), "max_resources", 2, 2)


async def test_a_booking_limit_set_for_one_business_applies(client: httpx.AsyncClient) -> None:
    s = await on(client, bookings_per_month=None)
    await set_overrides(client, s, bookings_per_month=1)
    mei = await s.add_staff("Mei")
    assert (await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, 10), mei))).status_code == 201
    refused(await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, 11), mei)), "max_bookings_per_month", 1, 1)


async def test_a_message_limit_set_for_one_business_applies(client: httpx.AsyncClient, otp: Any, mail: Recorder) -> None:
    s = await on(client, whatsapp_per_month=5)
    await set_overrides(client, s, whatsapp_per_month=1)
    assert (await request_code(s, "012-345 0001")).status_code == 202
    r = await request_code(s, "012-345 0002")
    refused(r, "max_whatsapp_per_month", 1, 1)
    await set_overrides(client, s, whatsapp_per_month=None)
    assert (await request_code(s, "012-345 0003")).status_code == 202


async def test_what_the_admin_sees_is_what_applies(client: httpx.AsyncClient) -> None:
    s = await on(client, services=5)
    plain = await usage_of(s)
    assert plain["customised"] == [] and plain["plan"]["limits"]["services"] == 5
    await set_overrides(client, s, services=7, sms_per_month=None)
    custom = await usage_of(s)
    assert custom["customised"] == ["services", "sms_per_month"] and custom["plan"]["limits"]["services"] == 7 and custom["plan"]["code"] == plain["plan"]["code"]


# --------------------------------------------------------------------- the warnings
OWNER = "owner@lotus.example.com"


async def set_contact(s: Seed, email: str | None = OWNER) -> None:
    body: dict[str, Any] = {"business_name": "Lotus Spa"}
    if email:
        body["contact_email"] = email
    assert (await s.client.put("/v1/admin/business-profile", json=body, headers=s.admin)).status_code == 200


def sent(mail: Recorder) -> list[Any]:
    return [m for m in mail.sent if m.channel == "email" and ("close to your limit" in (m.subject or "") or "reached your limit" in (m.subject or ""))]


async def pending_events(s: Seed) -> list[OutboxEvent]:
    async with tenant_session(s.tenant_id) as db:
        return list((await db.scalars(select(OutboxEvent).where(OutboxEvent.event_type == plans.LIMIT_WARNING_EVENT).order_by(OutboxEvent.seq))).all())


async def recorded(s: Seed) -> list[tuple[str, str, str, int]]:
    async with tenant_session(s.tenant_id) as db:
        return sorted((w.limit_name, w.period, w.level, w.allowed) for w in (await db.scalars(select(PlanWarning))).all())


async def services_up_to(s: Seed, total: int) -> None:
    have = len((await s.client.get("/v1/admin/services", headers=s.admin)).json())
    for i in range(total - have):
        assert (await add_service(s, f"Extra {uuid.uuid4().hex[:5]}")).status_code == 201


async def test_a_business_is_warned_near_its_limit_and_again_at_it(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, services=10)
    await set_contact(s)
    await services_up_to(s, 7)
    assert await pending_events(s) == [] and await recorded(s) == []  # (7 of 10: not yet)
    await services_up_to(s, 8)
    (event,) = await pending_events(s)
    assert event.payload == {"limit": "services", "level": "near", "used": 8, "allowed": 10, "period": "total"} and event.aggregate_type == "tenant"
    await run_outbox(s)
    (near,) = sent(mail)
    assert near.recipient == OWNER and near.subject == "Lotus Spa: you are close to your limit of services"
    assert "used 8 of its 10 services on the Tiny plan" in near.body and "ask to move to a bigger plan" in near.body and "this month" not in near.body
    await services_up_to(s, 9)
    await services_up_to(s, 10)
    await run_outbox(s)
    near_and_reached = sent(mail)
    assert len(near_and_reached) == 2 and near_and_reached[1].subject == "Lotus Spa: you have reached your limit of services"
    assert "reached its limit of 10 services" in near_and_reached[1].body and "until something is removed" in near_and_reached[1].body
    assert await recorded(s) == [("services", "total", "near", 10), ("services", "total", "reached", 10)]  # one row each, not one per service


async def test_a_refused_attempt_warns_nobody_again(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, services=3)
    await set_contact(s)
    await services_up_to(s, 3)
    await run_outbox(s)
    refused(await add_service(s, "Over"), "max_services", 3, 3)
    refused(await add_service(s, "Over again"), "max_services", 3, 3)
    await run_outbox(s)
    assert [m.subject for m in sent(mail)] == ["Lotus Spa: you have reached your limit of services"]  # (80% of 3 rounds up to 3, so the first warning is the limit itself)
    assert await recorded(s) == [("services", "total", "reached", 3)]


async def test_a_business_that_jumps_straight_to_its_limit_gets_the_one_that_matters(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, services=2)  # (one exists: the next is the 2nd of 2)
    await set_contact(s)
    await services_up_to(s, 2)
    await run_outbox(s)
    assert await recorded(s) == [("services", "total", "reached", 2)] and [m.subject for m in sent(mail)] == ["Lotus Spa: you have reached your limit of services"]


async def test_a_changed_limit_is_warned_about_afresh(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, services=10)
    await set_contact(s)
    await services_up_to(s, 8)
    await set_overrides(client, s, services=20)
    await services_up_to(s, 16)  # 16 of 20 is 80% of the new limit
    await run_outbox(s)
    assert await recorded(s) == [("services", "total", "near", 10), ("services", "total", "near", 20)]
    assert len(sent(mail)) == 2 and "used 16 of its 20 services" in sent(mail)[1].body


async def test_a_limit_that_is_per_month_says_so_and_is_kept_per_month(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, bookings_per_month=10)
    await set_contact(s)
    mei = await s.add_staff("Mei")
    for hour in (9, 10, 11, 12, 14, 15, 16):
        assert (await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, hour), mei))).status_code == 201
    assert await recorded(s) == []
    assert (await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, 17), mei))).status_code == 201  # the 8th
    month = plans.month_label(await tenant_row(s.tenant_id), dt.datetime.now(dt.UTC))
    assert await recorded(s) == [("bookings_per_month", month, "near", 10)]
    await run_outbox(s)
    (mail_,) = sent(mail)
    assert "used 8 of its 10 bookings a month this month" in mail_.body and "until next month" in mail_.body


async def test_a_booking_that_does_not_happen_leaves_no_warning(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, bookings_per_month=10)
    mei = await s.add_staff("Mei")
    for hour in (9, 10, 11, 12, 14, 15, 16):
        assert (await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, hour), mei))).status_code == 201
    taken = await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, 9), mei))
    assert taken.status_code == 409  # it would have been the 8th
    assert await recorded(s) == [] and await pending_events(s) == []  # (rolled back with the booking)
    assert (await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, 17), mei))).status_code == 201
    assert len(await recorded(s)) == 1


async def test_messages_are_warned_about_too(client: httpx.AsyncClient, otp: Any, mail: Recorder) -> None:
    s = await on(client, whatsapp_per_month=5)
    await set_contact(s)
    for n in range(1, 4):
        assert (await request_code(s, f"012-345 000{n}")).status_code == 202
    assert await recorded(s) == []
    assert (await request_code(s, "012-345 0004")).status_code == 202  # the 4th of 5
    assert (await request_code(s, "012-345 0005")).status_code == 202
    await run_outbox(s)
    month = plans.month_label(await tenant_row(s.tenant_id), dt.datetime.now(dt.UTC))
    assert await recorded(s) == [("whatsapp_per_month", month, "near", 5), ("whatsapp_per_month", month, "reached", 5)]
    assert [m.subject for m in sent(mail)] == ["Lotus Spa: you are close to your limit of WhatsApp messages a month", "Lotus Spa: you have reached your limit of WhatsApp messages a month"]


async def test_nothing_is_said_without_a_limit_or_with_warnings_switched_off(client: httpx.AsyncClient, mail: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    free_of_limits = await on(client)
    await services_up_to(free_of_limits, 12)
    zero = await on(client, resources=0)
    refused(await zero.client.post("/v1/admin/resources", json={"type": "staff", "name": "Mei"}, headers=zero.admin), "max_resources", 0, 0)
    assert await pending_events(free_of_limits) == [] and await pending_events(zero) == [] and await recorded(zero) == []  # (none at all: refused, never warned about)
    monkeypatch.setattr(get_settings(), "plan_warning_percent", 0)
    quiet = await on(client, services=3)
    await services_up_to(quiet, 3)
    assert await pending_events(quiet) == [] and await recorded(quiet) == []


async def test_without_a_contact_email_the_warning_is_noted_not_sent(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, services=2)
    await services_up_to(s, 2)
    await run_outbox(s)
    assert sent(mail) == []
    rows = [r for r in await log_rows(s) if r.event == plans.LIMIT_WARNING_EVENT]
    assert [(r.status, r.error) for r in rows] == [("skipped", "no business contact email on file")]


async def test_a_warning_that_could_not_be_sent_is_tried_again(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, services=2)
    await set_contact(s)
    await services_up_to(s, 2)
    mail.failing["email"] = RuntimeError("smtp down")
    t0 = dt.datetime.now(dt.UTC)
    result = await run_outbox(s, now=t0)
    assert result.failed == 1 and sent(mail) == []
    mail.failing.clear()
    await run_outbox(s, now=t0 + dt.timedelta(minutes=10))
    assert len(sent(mail)) == 1
    await run_outbox(s, now=t0 + dt.timedelta(hours=3))
    assert len(sent(mail)) == 1  # and not again


async def test_the_admin_sees_the_same_warnings_the_emails_are_about(client: httpx.AsyncClient) -> None:
    s = await on(client, services=10)
    await services_up_to(s, 8)
    shown = (await usage_of(s))["warnings"]
    assert shown == [{"limit": "services", "level": "near", "used": 8, "allowed": 10, "percent": 80}]
    await services_up_to(s, 10)
    assert (await usage_of(s))["warnings"][0]["level"] == "reached"


async def test_warnings_belong_to_one_business_and_are_not_webhook_events(client: httpx.AsyncClient) -> None:
    a, b = await on(client, services=2), await on(client, services=2)
    await services_up_to(a, 2)
    assert len(await recorded(a)) == 1 and await recorded(b) == []
    assert plans.LIMIT_WARNING_EVENT not in WEBHOOK_EVENTS
    async with tenant_session(b.tenant_id) as db:
        assert (await db.scalars(select(PlanWarning))).all() == []


async def test_the_database_will_not_record_the_same_warning_twice_or_an_odd_level(client: httpx.AsyncClient) -> None:
    from sqlalchemy.exc import IntegrityError

    s = await on(client, services=2)
    async with tenant_session(s.tenant_id) as db:
        db.add(PlanWarning(tenant_id=s.tenant_id, limit_name="services", period="total", level="near", allowed=2))
    with pytest.raises(IntegrityError):
        async with tenant_session(s.tenant_id) as db:
            db.add(PlanWarning(tenant_id=s.tenant_id, limit_name="services", period="total", level="near", allowed=2))
    with pytest.raises(IntegrityError):
        async with tenant_session(s.tenant_id) as db:
            db.add(PlanWarning(tenant_id=s.tenant_id, limit_name="services", period="total", level="loud", allowed=2))
    async with tenant_session(s.tenant_id) as db:
        await db.execute(text("DELETE FROM plan_warnings"))


# --------------------------------------------------------------------- gaps found by mutation testing
async def test_a_resource_limit_warns_too(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, resources=3)
    await set_contact(s)
    for name in ("Mei", "Lin"):
        assert (await s.client.post("/v1/admin/resources", json={"type": "staff", "name": name}, headers=s.admin)).status_code == 201
    assert await recorded(s) == []
    assert (await s.client.post("/v1/admin/resources", json={"type": "staff", "name": "Kim"}, headers=s.admin)).status_code == 201
    assert await recorded(s) == [("resources", "total", "reached", 3)]
    await run_outbox(s)
    assert [m.subject for m in sent(mail)] == ["Lotus Spa: you have reached your limit of staff, tables and bays"]


async def test_a_refusal_is_not_a_warning(client: httpx.AsyncClient, mail: Recorder) -> None:
    s = await on(client, services=None)
    await services_up_to(s, 3)
    await set_overrides(client, s, services=1)  # (already over it)
    assert await recorded(s) == [] and await pending_events(s) == []
    refused(await add_service(s, "Too many"), "max_services", 1, 3)
    assert await recorded(s) == [] and await pending_events(s) == []


async def test_a_warning_already_sent_is_not_sent_again_if_the_event_comes_round_again(client: httpx.AsyncClient, mail: Recorder) -> None:
    from app.services.notifications import NotificationPublisher

    s = await on(client, services=2)
    await set_contact(s)
    await services_up_to(s, 2)
    await run_outbox(s)
    assert len(sent(mail)) == 1
    (event,) = await pending_events(s)
    await NotificationPublisher().publish(event)
    await NotificationPublisher().publish(event)
    assert len(sent(mail)) == 1
