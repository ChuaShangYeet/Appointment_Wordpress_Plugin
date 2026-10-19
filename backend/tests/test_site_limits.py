"""The free plan is one account, one business, one website (migration 0028).

* the website address is required on a plan that limits domains, and the browser origins are derived from it (www and non-www are one site);
* the address can change once every ``domain_change_days`` (30 on free), through the owner's ``PUT /admin/site`` and through the sync webhook alike;
* a site has at most ``max_domains`` domains, a business ``max_sites`` sites, and at most ``max_live_keys_per_kind`` live keys of each kind - and a key recovery always works;
* the plugin may report the address it runs on (``site_url`` at token exchange): a signal for the operator, never a reason to refuse.

Paid businesses and those the operator set up without a domain limit keep what they had: a free-form, validated list of origins, and an empty list lets any origin in.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text, update

from app.core import origins
from app.core.database import tenant_session
from app.core.exceptions import PlanLimitReached
from app.core.security import sign_payload
from app.models import ApiKey, Plan, Site, Tenant
from app.services import plans, sites
from tests.conftest import PLATFORM, Seed
from tests.test_country_setup import business
from tests.test_plans import new_plan, usage_of

UTC = dt.UTC


# --------------------------------------------------------------------- helpers
async def free(client: httpx.AsyncClient, domain: str = "glow.example", **extra: Any) -> Seed:
    return await business(client, plan="free", domain=domain, **extra)


async def site_of(s: Seed) -> Site:
    async with tenant_session(None) as db:
        return (await db.scalars(select(Site).where(Site.tenant_id == s.tenant_id))).one()


async def edit_site(s: Seed, **values: Any) -> None:
    async with tenant_session(None) as db:
        await db.execute(update(Site).where(Site.tenant_id == s.tenant_id).values(**values))


async def put_site(s: Seed, headers: dict[str, str] | None = None, **body: Any) -> httpx.Response:
    return await s.client.put("/v1/admin/site", json=body, headers=headers or s.admin)


async def get_site(s: Seed) -> dict[str, Any]:
    r = await s.client.get("/v1/admin/site", headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def reach(s: Seed, origin: str) -> int:
    """What the publishable key gets from a browser page on ``origin``."""
    return (await s.client.get("/v1/public/services", headers={**s.pk, "Origin": origin})).status_code


def refused_for(r: httpx.Response, limit: str, **expect: Any) -> dict[str, Any]:
    body = r.json()
    assert r.status_code == 402, r.text
    assert body["error"] == "plan_limit_reached" and body["limit"] == limit, body
    for key, value in expect.items():
        assert body[key] == value, body
    return body  # type: ignore[no-any-return]


async def sync(s: Seed, data: dict[str, Any]) -> httpx.Response:
    body = json.dumps({"event": "site.updated", "data": data}).encode()
    ts = str(int(time.time()))
    headers = {**s.pk, "X-Timestamp": ts, "X-Signature": sign_payload(s.webhook_secret, ts, body), "Content-Type": "application/json"}
    return await s.client.post("/v1/sync/webhook", content=body, headers=headers)


async def keys_of(s: Seed) -> list[dict[str, Any]]:
    r = await s.client.get("/v1/auth/api-keys", headers=s.admin)
    assert r.status_code == 200, r.text
    return list(r.json())


def live(keys: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [k for k in keys if k["kind"] == kind and k["revoked_at"] is None]


# --------------------------------------------------------------------- the address as text
@pytest.mark.parametrize(("typed", "host"), [
    ("shop.example", "shop.example"), ("https://shop.example", "shop.example"), ("HTTPS://WWW.Shop.Example/", "shop.example"),
    ("http://www.shop.example/book?x=1#top", "shop.example"), ("  shop.example  ", "shop.example"), ("www.shop.example.my", "shop.example.my"),
    ("book.shop.example", "book.shop.example"), ("bücher.example", "xn--bcher-kva.example"), ("https://shop.example:443", None), ("shop.example.", "shop.example"),
])
def test_a_website_address_becomes_one_canonical_host(typed: str, host: str | None) -> None:
    if host is None:
        with pytest.raises(origins.WebsiteProblem, match="port"):
            origins.host_of(typed)
    else:
        assert origins.host_of(typed) == host


@pytest.mark.parametrize("typed", ["", "   ", "localhost", "127.0.0.1", "http://[::1]/", "10.0.0.8", "ftp://shop.example", "https://user:pw@shop.example", "shop", "a..b.example",
                                   "-shop.example", "shop.123", "x" * 64 + ".example", "shop example", "https://", "mailto:a@b.example", "shop.example:8443", "a" * 301])
def test_what_is_not_a_public_website_is_refused(typed: str) -> None:
    with pytest.raises(origins.WebsiteProblem):
        origins.host_of(typed)


def test_the_origins_of_a_host_are_https_with_and_without_www() -> None:
    assert origins.derive_origins(["shop.example"]) == ["https://shop.example", "https://www.shop.example"]
    assert origins.derive_origins(["www.shop.example", "shop.example", "b.example"]) == ["https://b.example", "https://shop.example", "https://www.b.example", "https://www.shop.example"]
    assert origins.derive_origins([]) == []


@pytest.mark.parametrize(("typed", "origin"), [
    ("https://Shop.Example", "https://shop.example"), ("https://shop.example/", "https://shop.example"), ("https://shop.example:443", "https://shop.example"),
    ("http://shop.example:80", "http://shop.example"), ("http://localhost:3000", "http://localhost:3000"), ("https://shop.example:8443", "https://shop.example:8443"),
])
def test_one_origin_of_a_free_form_list_is_made_canonical(typed: str, origin: str) -> None:
    assert origins.origin_of(typed) == origin


@pytest.mark.parametrize("typed", ["shop.example", "*", "https://shop.example/book", "https://shop.example?x=1", "ftp://shop.example", "https://", "", "https://shop.example#a"])
def test_a_free_form_origin_must_be_an_origin(typed: str) -> None:
    with pytest.raises(origins.WebsiteProblem, match="write it as https://|leave out the path|not a web address|http or https|no host|empty"):
        origins.origin_of(typed)
    if "://" not in typed:
        with pytest.raises(origins.WebsiteProblem, match="write it as https://"):  # (the message says what to do)
            origins.origin_of(typed)


def test_the_developers_own_machine_is_recognised_and_left_out_of_the_hosts() -> None:
    for origin in ("http://localhost", "http://localhost:5173", "http://127.0.0.1:8000"):
        assert origins.is_dev_origin(origin)
    for origin in ("https://localhost", "http://localhost.evil.example", "http://127.0.0.1.evil.example", "http://evil.example/localhost", "http://localhost:99999x"):
        assert not origins.is_dev_origin(origin)
    assert origins.hosts_of("Shop.example", ["https://www.shop.example", "http://localhost:3000", "https://other.example:8443"]) == {"shop.example", "other.example"}
    assert origins.hosts_of(None, []) == frozenset()


def test_the_address_a_plugin_reports_is_read_leniently() -> None:
    assert origins.site_url_host("https://WWW.Shop.example/wp/") == "shop.example"
    assert origins.site_url_host("shop.example") == "shop.example"
    assert origins.site_url_host("http://localhost:8080") == "localhost"
    for junk in (None, "", "   ", "http://[::1", "https://"):
        assert origins.site_url_host(junk) is None


# --------------------------------------------------------------------- the plans
async def test_the_free_plan_holds_one_website_and_the_paid_plan_nothing_back(client: httpx.AsyncClient) -> None:
    got = {p["code"]: p["limits"] for p in (await client.get("/v1/platform/plans", headers=PLATFORM)).json()}
    assert {k: got["free"][k] for k in ("sites", "domains", "live_keys_per_kind", "domain_change_days")} == {"sites": 1, "domains": 1, "live_keys_per_kind": 2, "domain_change_days": 30}
    assert {k: got["paid"][k] for k in ("sites", "domains", "live_keys_per_kind", "domain_change_days")} == {"sites": None, "domains": None, "live_keys_per_kind": None, "domain_change_days": None}


async def test_the_database_refuses_a_negative_site_limit(client: httpx.AsyncClient) -> None:
    from sqlalchemy.exc import IntegrityError

    for column in ("max_sites", "max_domains", "max_live_keys_per_kind", "domain_change_days"):
        with pytest.raises(IntegrityError):
            async with tenant_session(None) as db:
                await db.execute(text(f"INSERT INTO plans (code, name, {column}) VALUES ('neg{uuid.uuid4().hex[:6]}', 'x', -1)"))


async def test_the_business_sees_what_it_uses_and_the_structural_limits_are_never_warned_about(client: httpx.AsyncClient) -> None:
    s = await free(client)
    got = await usage_of(s)
    assert (got["usage"]["sites"], got["usage"]["domains"], got["usage"]["live_keys_per_kind"]) == (1, 1, 1)
    assert got["plan"]["limits"]["sites"] == 1 and got["plan"]["limits"]["domain_change_days"] == 30
    assert got["warnings"] == [] and got["over_limit"] == []  # (1 of 1 sites is where a free business starts: that is not news)
    assert got["domain_change"] == {"every_days": 30, "next_allowed_at": None}
    async with tenant_session(s.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM outbox_events WHERE event_type = 'plan.limit_warning'"))).scalar() == 0


async def test_the_operator_can_give_one_business_its_own_site_limits(client: httpx.AsyncClient) -> None:
    s = await free(client)
    r = await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", headers=PLATFORM, json={"sites": 2, "domains": 3, "live_keys_per_kind": 5, "domain_change_days": 0})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["overrides"] == {"sites": 2, "domains": 3, "live_keys_per_kind": 5, "domain_change_days": 0}
    assert {k: body["limits"][k] for k in ("sites", "domains", "live_keys_per_kind", "domain_change_days")} == {"sites": 2, "domains": 3, "live_keys_per_kind": 5, "domain_change_days": 0}
    assert {k: body["plan_limits"][k] for k in ("sites", "domains", "live_keys_per_kind", "domain_change_days")} == {"sites": 1, "domains": 1, "live_keys_per_kind": 2, "domain_change_days": 30}
    assert sorted((await usage_of(s))["customised"]) == ["domain_change_days", "domains", "live_keys_per_kind", "sites"]
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", headers=PLATFORM, json={"domain_change_days": -1})).status_code == 422
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", headers=PLATFORM, json={})).json()["overrides"] == {}


async def test_a_business_moved_to_a_smaller_plan_is_told_what_it_is_over_and_loses_nothing(client: httpx.AsyncClient) -> None:
    s = await business(client)  # paid
    extra = await s.client.post("/v1/auth/api-keys", json={"kind": "secret"}, headers=s.admin)
    assert extra.status_code == 201
    third = await s.client.post("/v1/auth/api-keys", json={"kind": "secret"}, headers=s.admin)
    assert third.status_code == 201
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={"plan": "free"}, headers=PLATFORM)).status_code == 200
    got = await usage_of(s)
    assert got["over_limit"] == ["live_keys_per_kind"] and len(live(await keys_of(s), "secret")) == 3
    assert (await s.client.post("/v1/auth/api-keys", json={"kind": "secret"}, headers=s.admin)).status_code == 402  # (and cannot add more)


# --------------------------------------------------------------------- sign-up and set-up
async def test_a_free_business_is_set_up_with_its_website_and_origins_derived_from_it(client: httpx.AsyncClient) -> None:
    s = await free(client, domain="https://WWW.Glow.Example/book")
    site = await site_of(s)
    assert site.domain == "glow.example" and site.allowed_origins == ["https://glow.example", "https://www.glow.example"]
    assert site.domain_changed_at is None  # (the address given at set-up is not a change)
    assert [await reach(s, o) for o in ("https://glow.example", "https://www.glow.example", "https://GLOW.example")] == [200, 200, 200]
    assert [await reach(s, o) for o in ("https://other.example", "http://glow.example", "https://glow.example.evil.example", "https://evilglow.example", "null")] == [403] * 5


async def test_the_developers_own_machine_may_call_with_the_key_but_nobody_elses_local_page_name_does(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert [await reach(s, o) for o in ("http://localhost:5173", "http://127.0.0.1:8000", "http://localhost")] == [200, 200, 200]
    assert [await reach(s, o) for o in ("https://localhost", "http://localhost.evil.example", "http://192.168.0.5")] == [403, 403, 403]


async def test_a_call_with_no_origin_is_not_held_by_the_list(client: httpx.AsyncClient) -> None:
    """The ``Origin`` header only exists in browsers: a server calling the API itself is not stopped by the allowed origins. The limits that cost money are shared by the whole business."""
    s = await free(client)
    assert (await s.client.get("/v1/public/services", headers=s.pk)).status_code == 200


async def test_a_free_business_cannot_be_set_up_without_a_website_or_with_its_own_list_of_origins(client: httpx.AsyncClient) -> None:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "No site", "business_type": "spa", "plan": "free"})
    assert r.status_code == 422 and "A website address (domain) is needed on this plan" in r.json()["detail"]
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "Two", "business_type": "spa", "plan": "free", "domain": "a.example", "allowed_origins": ["https://b.example"]})
    assert r.status_code == 422 and "follow the website address" in r.json()["detail"]
    for bad in ("localhost", "10.1.2.3", "a.example:8443", "not a site"):
        assert (await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "Bad", "business_type": "spa", "plan": "free", "domain": bad})).status_code == 422, bad
    async with tenant_session(None) as db:
        assert (await db.execute(text("SELECT count(*) FROM tenants WHERE name IN ('No site', 'Two', 'Bad')"))).scalar() == 0  # nothing was created


async def test_a_paid_business_keeps_a_free_form_list_and_an_empty_list_lets_any_origin_in(client: httpx.AsyncClient) -> None:
    open_site = await business(client)  # paid, no domain, no origins
    assert (await site_of(open_site)).allowed_origins == [] and await reach(open_site, "https://anything.example") == 200
    listed = await business(client, domain="Shop.Example", allowed_origins=["HTTPS://One.example/", "https://two.example:8443"])
    site = await site_of(listed)
    assert site.domain == "shop.example" and site.allowed_origins == ["https://one.example", "https://two.example:8443"]
    assert [await reach(listed, o) for o in ("https://one.example", "https://two.example:8443", "https://three.example")] == [200, 200, 403]


@pytest.mark.parametrize("bad", ["one.example", "*", "https://one.example/path", "ftp://one.example", ""])
async def test_a_list_of_origins_must_be_origins(client: httpx.AsyncClient, bad: str) -> None:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "Bad", "business_type": "spa", "allowed_origins": [bad]})
    assert r.status_code == 422, r.text


async def test_a_list_of_origins_cannot_be_endless(client: httpx.AsyncClient) -> None:
    many = [f"https://site{i}.example" for i in range(51)]
    assert (await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "Many", "business_type": "spa", "allowed_origins": many})).status_code == 422
    assert (await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "Fifty", "business_type": "spa", "allowed_origins": many[:50]})).status_code == 201


# --------------------------------------------------------------------- the owner changes the address
async def test_the_owner_may_change_the_address_once_and_then_must_wait_30_days(client: httpx.AsyncClient) -> None:
    s = await free(client)
    before = dt.datetime.now(UTC)
    r = await put_site(s, domain="https://new-glow.example")
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["domain"] == "new-glow.example" and got["allowed_origins"] == ["https://new-glow.example", "https://www.new-glow.example"] and got["origins_follow_domain"] is True
    assert dt.datetime.fromisoformat(got["domain_changed_at"]) >= before
    due = dt.datetime.fromisoformat(got["domain_change"]["next_allowed_at"])
    assert got["domain_change"]["every_days"] == 30 and dt.timedelta(days=29, hours=23) < due - before <= dt.timedelta(days=30, minutes=1)
    assert [await reach(s, "https://new-glow.example"), await reach(s, "https://glow.example")] == [200, 403]  # at once
    again = await put_site(s, domain="third.example")
    body = refused_for(again, "domain_change_days", allowed=30, plan="free")
    assert dt.datetime.fromisoformat(body["next_allowed_at"]) == due
    assert (await get_site(s))["domain"] == "new-glow.example" and await reach(s, "https://new-glow.example") == 200  # nothing changed
    assert (await usage_of(s))["domain_change"] == {"every_days": 30, "next_allowed_at": got["domain_change"]["next_allowed_at"]}


async def test_after_30_days_the_address_may_change_again(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await put_site(s, domain="second.example")).status_code == 200
    await edit_site(s, domain_changed_at=dt.datetime.now(UTC) - dt.timedelta(days=29, hours=23))
    assert (await put_site(s, domain="third.example")).status_code == 402
    await edit_site(s, domain_changed_at=dt.datetime.now(UTC) - dt.timedelta(days=30, minutes=1))
    r = await put_site(s, domain="third.example")
    assert r.status_code == 200 and r.json()["domain"] == "third.example"
    assert (await put_site(s, domain="fourth.example")).status_code == 402  # and the clock started again


@pytest.mark.parametrize("same", ["glow.example", "GLOW.example", "www.glow.example", "https://www.glow.example/", "http://glow.example/shop"])
async def test_the_same_site_spelled_another_way_is_not_a_change(client: httpx.AsyncClient, same: str) -> None:
    s = await free(client)
    assert (await put_site(s, domain="new.example")).status_code == 200  # a real change: the clock is running
    assert (await put_site(s, domain="new.example")).status_code == 200
    r = await put_site(s, domain=same.lower().replace("glow", "new"))
    assert r.status_code == 200 and r.json()["domain"] == "new.example"
    assert (await site_of(s)).domain_changed_at is not None


async def test_setting_the_same_address_never_starts_the_clock(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await put_site(s, domain="www.GLOW.example")).status_code == 200
    assert (await site_of(s)).domain_changed_at is None
    assert (await put_site(s, domain="other.example")).status_code == 200  # still the first change


async def test_the_operator_can_lift_the_wait_for_a_business_that_needs_it(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await put_site(s, domain="b.example")).status_code == 200
    assert (await put_site(s, domain="c.example")).status_code == 402
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", headers=PLATFORM, json={"domain_change_days": 0})).status_code == 200
    assert (await put_site(s, domain="c.example")).status_code == 200
    assert (await put_site(s, domain="d.example")).status_code == 200
    refused = await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", headers=PLATFORM, json={"domain_change_days": 7})
    assert refused.status_code == 200
    again = await put_site(s, domain="e.example")
    assert "with limits set for your business" in refused_for(again, "domain_change_days", allowed=7)["detail"]


async def test_the_free_plan_allows_one_domain_only(client: httpx.AsyncClient) -> None:
    s = await free(client)
    r = await put_site(s, domain="glow.example", allowed_origins=["https://glow.example", "https://www.glow.example", "https://second.example"])
    refused_for(r, "max_domains", allowed=1, used=2)
    assert (await put_site(s, domain="glow.example", allowed_origins=["https://www.glow.example"])).status_code == 200  # the same site
    assert (await site_of(s)).allowed_origins == ["https://glow.example", "https://www.glow.example"]
    assert await reach(s, "https://second.example") == 403


async def test_a_plan_with_more_domains_allows_that_many_and_changing_the_set_counts_as_a_change(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", headers=PLATFORM, json={"domains": 2})).status_code == 200
    r = await put_site(s, domain="glow.example", allowed_origins=["https://second.example"])
    assert r.status_code == 200, r.text
    assert (await site_of(s)).allowed_origins == ["https://glow.example", "https://second.example", "https://www.glow.example", "https://www.second.example"]
    assert await reach(s, "https://second.example") == 200
    refused_for(await put_site(s, domain="glow.example", allowed_origins=["https://second.example", "https://third.example"]), "max_domains", allowed=2, used=3)
    refused_for(await put_site(s, domain="glow.example", allowed_origins=["https://third.example"]), "domain_change_days", allowed=30)  # the first change started the clock
    assert (await put_site(s, domain="glow.example")).status_code == 200  # no allowed_origins: the extra domain is kept, so nothing changes


async def test_the_address_must_be_a_public_website(client: httpx.AsyncClient) -> None:
    s = await free(client)
    for bad in ("localhost", "10.0.0.1", "shop.example:8443", "ftp://shop.example", "no dots"):
        r = await put_site(s, domain=bad)
        assert r.status_code == 422, bad
    assert (await put_site(s, domain="a.example", allowed_origins=["not-an-origin"])).status_code == 422
    assert (await site_of(s)).domain == "glow.example" and (await site_of(s)).domain_changed_at is None
    assert (await s.client.put("/v1/admin/site", json={"domain": "a.example", "x": 1}, headers=s.admin)).status_code == 422
    assert (await s.client.put("/v1/admin/site", json={}, headers=s.admin)).status_code == 422


async def test_a_paid_business_changes_its_address_and_list_at_will(client: httpx.AsyncClient) -> None:
    s = await business(client, domain="one.example", allowed_origins=["https://one.example"])
    for host in ("two.example", "three.example", "four.example"):
        r = await put_site(s, domain=host, allowed_origins=[f"https://{host}", f"https://www.{host}", "https://extra.example"])
        assert r.status_code == 200, r.text
        assert r.json()["origins_follow_domain"] is False and r.json()["domain_change"] == {"every_days": None, "next_allowed_at": None}
    assert (await site_of(s)).domain_changed_at is None
    assert (await put_site(s, domain="four.example", allowed_origins=[])).status_code == 200
    assert (await site_of(s)).allowed_origins == [] and await reach(s, "https://whoever.example") == 200


async def test_only_the_owner_changes_the_address_and_a_manager_may_look(client: httpx.AsyncClient) -> None:
    s = await free(client)
    manager, staff = await s.token("manager"), await s.token("staff")
    assert (await put_site(s, manager, domain="x.example")).status_code == 403
    assert (await put_site(s, staff, domain="x.example")).status_code == 403
    assert (await s.client.get("/v1/admin/site", headers=manager)).status_code == 200
    assert (await s.client.get("/v1/admin/site", headers=staff)).status_code == 403
    assert (await site_of(s)).domain == "glow.example"


async def test_a_change_of_address_is_in_the_audit_log(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await put_site(s, domain="logged.example")).status_code == 200
    assert (await put_site(s, domain="refused.example")).status_code == 402
    rows = (await s.client.get("/v1/admin/audit-log", headers=s.admin, params={"action": "PUT /v1/admin/site"})).json()
    assert len(rows) == 1  # (only the change that happened)


# --------------------------------------------------------------------- the sync webhook obeys the same rules
async def test_the_plugin_may_change_the_address_through_the_sync_webhook_once_in_30_days(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await sync(s, {"domain": "https://plugin.example"})).status_code == 200
    site = await site_of(s)
    assert site.domain == "plugin.example" and site.allowed_origins == ["https://plugin.example", "https://www.plugin.example"] and site.domain_changed_at is not None
    refused_for(await sync(s, {"domain": "other.example"}), "domain_change_days", allowed=30)
    assert (await site_of(s)).domain == "plugin.example"
    assert (await put_site(s, domain="third.example")).status_code == 402  # the owner endpoint shares the clock


async def test_the_sync_webhook_cannot_add_a_second_domain_on_the_free_plan(client: httpx.AsyncClient) -> None:
    s = await free(client)
    refused_for(await sync(s, {"allowed_origins": ["https://glow.example", "https://elsewhere.example"]}), "max_domains", allowed=1)
    refused_for(await sync(s, {"domain": "glow.example", "allowed_origins": [f"https://s{i}.example" for i in range(20)]}), "max_domains", allowed=1)
    assert (await sync(s, {"allowed_origins": ["https://glow.example", "https://www.glow.example"]})).status_code == 200  # the same site again
    assert (await sync(s, {"allowed_origins": ["https://www.glow.example"]})).status_code == 200
    assert (await site_of(s)).allowed_origins == ["https://glow.example", "https://www.glow.example"]


async def test_the_sync_webhook_still_refuses_junk(client: httpx.AsyncClient) -> None:
    s = await free(client)
    for data in ({"domain": 5}, {"domain": ["a.example"]}, {"allowed_origins": "https://a.example"}, {"allowed_origins": [1]}, {"allowed_origins": ["x"] * 51}, {"domain": "localhost"}, {"domain": "a b"}):
        assert (await sync(s, data)).status_code == 422, data
    assert (await sync(s, {"domain": None})).status_code == 422  # a free site needs its address
    assert (await site_of(s)).domain == "glow.example"


async def test_on_a_paid_plan_the_sync_webhook_keeps_a_free_form_list_but_a_checked_one(client: httpx.AsyncClient) -> None:
    s = await business(client)
    assert (await sync(s, {"domain": "Paid.Example", "allowed_origins": ["https://A.example/", "https://b.example", "https://c.example"]})).status_code == 200
    site = await site_of(s)
    assert site.domain == "paid.example" and site.allowed_origins == ["https://a.example", "https://b.example", "https://c.example"]
    assert (await sync(s, {"allowed_origins": ["not an origin"]})).status_code == 422
    assert (await site_of(s)).allowed_origins == ["https://a.example", "https://b.example", "https://c.example"]


# --------------------------------------------------------------------- keys
async def test_a_free_business_has_one_key_of_each_kind_and_room_for_a_spare(client: httpx.AsyncClient) -> None:
    s = await free(client)
    first = await keys_of(s)
    assert [(k["kind"], k["site_id"] is not None) for k in sorted(first, key=lambda k: k["kind"])] == [("publishable", True), ("secret", True)]
    spare = await s.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=s.admin)
    assert spare.status_code == 201, spare.text
    refused_for(await s.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=s.admin), "max_live_keys_per_kind", allowed=2, used=2)
    assert (await s.client.post("/v1/auth/api-keys", json={"kind": "secret"}, headers=s.admin)).status_code == 201  # the other kind has its own room
    assert (await s.client.post("/v1/auth/api-keys", json={"kind": "secret"}, headers=s.admin)).status_code == 402


async def test_revoking_a_key_gives_its_place_back(client: httpx.AsyncClient) -> None:
    s = await free(client)
    spare = (await s.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=s.admin)).json()
    assert (await s.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=s.admin)).status_code == 402
    assert (await s.client.delete(f"/v1/auth/api-keys/{spare['id']}", headers=s.admin)).status_code == 200
    assert (await s.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=s.admin)).status_code == 201


async def test_a_new_key_belongs_to_the_businesss_site_unless_it_names_another_of_its_own(client: httpx.AsyncClient) -> None:
    s = await free(client)
    mine = str((await site_of(s)).id)
    made = (await s.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=s.admin)).json()
    assert next(k for k in await keys_of(s) if k["id"] == made["id"])["site_id"] == mine
    other = await free(client, domain="other.example")
    r = await s.client.post("/v1/auth/api-keys", json={"kind": "secret", "site_id": str((await site_of(other)).id)}, headers=s.admin)
    assert r.status_code == 404
    assert (await s.client.post("/v1/auth/api-keys", json={"kind": "secret", "site_id": mine}, headers=s.admin)).status_code == 201


async def test_a_key_made_for_the_site_is_held_to_the_sites_origins(client: httpx.AsyncClient) -> None:
    s = await free(client)
    made = (await s.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=s.admin)).json()
    spare = {"X-Site-Key": made["key"]}
    assert (await s.client.get("/v1/public/services", headers={**spare, "Origin": "https://glow.example"})).status_code == 200
    assert (await s.client.get("/v1/public/services", headers={**spare, "Origin": "https://other.example"})).status_code == 403


async def test_a_key_that_belongs_to_no_site_still_works_as_before_and_is_listed(client: httpx.AsyncClient) -> None:
    """Keys made before this had no site (and so no origin check); they are not broken, only no longer made."""
    from app.core.security import generate_api_key

    s = await free(client)
    plain, prefix, digest = generate_api_key("publishable")
    async with tenant_session(None) as db:
        db.add(ApiKey(tenant_id=s.tenant_id, site_id=None, kind="publishable", prefix=prefix, key_hash=digest))
    legacy = {"X-Site-Key": plain}
    assert (await s.client.get("/v1/public/services", headers={**legacy, "Origin": "https://anywhere.example"})).status_code == 200
    assert [k["site_id"] for k in await keys_of(s) if k["prefix"] == prefix] == [None]
    assert (await usage_of(s))["usage"]["live_keys_per_kind"] == 2


async def test_a_recovery_always_works_and_drops_the_oldest_key_beyond_the_limit(client: httpx.AsyncClient) -> None:
    s = await free(client)
    old = {k["kind"]: k["id"] for k in await keys_of(s)}
    one = await s.client.post("/v1/auth/api-keys/recover", json={}, headers=s.admin)
    assert one.status_code == 201 and one.json()["revoked"] == 0  # two of each: the limit, nothing dropped
    assert len(live(await keys_of(s), "secret")) == 2 and len(live(await keys_of(s), "publishable")) == 2
    two = await s.client.post("/v1/auth/api-keys/recover", json={}, headers=s.admin)
    assert two.status_code == 201 and two.json()["revoked"] == 2  # the oldest of each kind
    after = await keys_of(s)
    assert len(live(after, "secret")) == 2 and len(live(after, "publishable")) == 2
    gone = {k["id"] for k in after if k["revoked_at"] is not None}
    assert gone == set(old.values())
    new = two.json()
    assert (await s.client.post("/v1/auth/token", json={"user_id": "u", "role": "owner"}, headers={"X-API-Key": new["secret_key"]})).status_code == 200
    assert (await s.client.post("/v1/auth/token", json={"user_id": "u", "role": "owner"}, headers=s.sk)).status_code == 401  # the oldest secret key is gone
    assert (await s.client.get("/v1/public/services", headers=s.pk)).status_code == 401


async def test_a_recovery_with_revoke_existing_still_revokes_everything_else(client: httpx.AsyncClient) -> None:
    s = await free(client)
    await s.client.post("/v1/auth/api-keys/recover", json={}, headers=s.admin)
    r = await s.client.post("/v1/auth/api-keys/recover", json={"revoke_existing": True}, headers=s.admin)
    assert r.status_code == 201 and r.json()["revoked"] == 4  # the first pair and the second, counted once each
    after = await keys_of(s)
    assert len(live(after, "secret")) == 1 and len(live(after, "publishable")) == 1


async def test_the_last_working_secret_key_still_cannot_be_revoked(client: httpx.AsyncClient) -> None:
    s = await free(client)
    (only,) = live(await keys_of(s), "secret")
    r = await s.client.delete(f"/v1/auth/api-keys/{only['id']}", headers=s.admin)
    assert r.status_code == 409


async def test_a_paid_business_has_no_cap_on_keys_and_a_recovery_drops_nothing(client: httpx.AsyncClient) -> None:
    s = await business(client)
    for _ in range(4):
        assert (await s.client.post("/v1/auth/api-keys", json={"kind": "publishable"}, headers=s.admin)).status_code == 201
    r = await s.client.post("/v1/auth/api-keys/recover", json={}, headers=s.admin)
    assert r.status_code == 201 and r.json()["revoked"] == 0 and len(live(await keys_of(s), "publishable")) == 6


# --------------------------------------------------------------------- more websites
async def test_one_more_website_is_refused_on_the_free_plan_and_allowed_where_the_plan_says(client: httpx.AsyncClient) -> None:
    s = await free(client)
    async with tenant_session(s.tenant_id) as db:
        tenant = await db.get(Tenant, s.tenant_id)
        assert tenant is not None
        with pytest.raises(PlanLimitReached) as caught:
            await plans.ensure_can_add_site(db, tenant)
        assert caught.value.extra == {"limit": "max_sites", "allowed": 1, "used": 1, "plan": "free"}
        tenant.plan_overrides = {"sites": 2}
        await plans.ensure_can_add_site(db, tenant)
        tenant.plan_overrides = {"sites": None}
        await plans.ensure_can_add_site(db, tenant)
        tenant.plan_overrides = {"sites": 0}
        with pytest.raises(PlanLimitReached):
            await plans.ensure_can_add_site(db, tenant)
    paid = await business(client)
    async with tenant_session(paid.tenant_id) as db:
        tenant = await db.get(Tenant, paid.tenant_id)
        assert tenant is not None
        await plans.ensure_can_add_site(db, tenant)


async def test_two_sites_make_two_domains_and_the_usage_says_so(client: httpx.AsyncClient) -> None:
    s = await business(client, domain="one.example")
    async with tenant_session(None) as db:
        db.add(Site(tenant_id=s.tenant_id, name="Second", domain="two.example", allowed_origins=["https://www.two.example", "http://localhost:3000"]))
    got = (await usage_of(s))["usage"]
    assert (got["sites"], got["domains"]) == (2, 2)  # (the developer's own machine is not a domain)
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={"plan": "free"}, headers=PLATFORM)).status_code == 200
    assert (await usage_of(s))["over_limit"] == ["sites", "domains"]


# --------------------------------------------------------------------- the address the plugin reports
async def exchange(s: Seed, **extra: Any) -> httpx.Response:
    return await s.client.post("/v1/auth/token", json={"user_id": "wp-1", "role": "owner", **extra}, headers=s.sk)


async def signal(s: Seed) -> dict[str, Any]:
    r = await s.client.get(f"/v1/platform/tenants/{s.tenant_id}/plan", headers=PLATFORM)
    assert r.status_code == 200, r.text
    return r.json()["site"]  # type: ignore[no-any-return]


async def test_the_operator_sees_nothing_to_compare_until_the_plugin_reports(client: httpx.AsyncClient) -> None:
    s = await free(client)
    got = await signal(s)
    assert got["domain"] == "glow.example" and got["last_site_url"] is None and got["last_site_url_at"] is None and got["site_url_matches_domain"] is None
    assert got["allowed_origins"] == ["https://glow.example", "https://www.glow.example"] and got["domain_changed_at"] is None and uuid.UUID(got["site_id"])


@pytest.mark.parametrize(("reported", "matches"), [
    ("https://glow.example", True), ("https://www.glow.example/wordpress/", True), ("HTTP://GLOW.EXAMPLE", True), ("http://localhost:8080", True),
    ("https://other.example", False), ("https://glow.example.evil.example", False), ("https://notglow.example", False),
])
async def test_the_operator_sees_whether_the_reported_address_is_the_sites(client: httpx.AsyncClient, reported: str, matches: bool) -> None:
    s = await free(client)
    assert (await exchange(s, site_url=reported)).status_code == 200
    got = await signal(s)
    assert got["last_site_url"] == reported and got["site_url_matches_domain"] is matches and got["last_site_url_at"] is not None


async def test_a_reported_address_never_refuses_a_request(client: httpx.AsyncClient) -> None:
    s = await free(client)
    for odd in (None, "", "   ", "not a url", "http://[::1", 123, 4.5, True, ["a"], {"a": 1}, "x" * 5000, "https://other.example"):
        r = await exchange(s, site_url=odd)
        assert r.status_code == 200 and "access_token" in r.json(), odd
    assert (await exchange(s)).status_code == 200  # (and it is optional)
    assert len((await signal(s))["last_site_url"] or "") <= 300


async def test_the_reported_address_is_stored_only_when_it_changes(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await exchange(s, site_url="https://glow.example")).status_code == 200
    first = (await signal(s))["last_site_url_at"]
    assert (await exchange(s, site_url="https://glow.example")).status_code == 200
    assert (await signal(s))["last_site_url_at"] == first  # nothing was written
    assert (await exchange(s, site_url="https://moved.example")).status_code == 200
    got = await signal(s)
    assert got["last_site_url"] == "https://moved.example" and got["last_site_url_at"] != first
    assert (await exchange(s)).status_code == 200 and (await signal(s))["last_site_url"] == "https://moved.example"  # not sending it does not clear it
    assert (await exchange(s, site_url="")).status_code == 200 and (await signal(s))["last_site_url"] == "https://moved.example"


async def test_a_mismatch_is_logged_once_for_the_operator_and_blocks_nothing(client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    s = await free(client)
    with caplog.at_level(logging.WARNING, logger="app.services.sites"):
        for _ in range(3):
            assert (await exchange(s, site_url="https://copy.example/wp")).status_code == 200
        assert (await exchange(s, site_url="https://glow.example")).status_code == 200
    lines = [r.getMessage() for r in caplog.records if r.name == "app.services.sites"]
    assert len(lines) == 1 and "copy.example" in lines[0] and "glow.example" in lines[0] and str(s.tenant_id) in lines[0]
    assert "/wp" not in lines[0]  # (the host only)
    assert (await s.client.get("/v1/public/services", headers={**s.pk, "Origin": "https://glow.example"})).status_code == 200


async def test_a_secret_key_that_belongs_to_no_site_reports_nothing(client: httpx.AsyncClient) -> None:
    from app.core.security import generate_api_key

    s = await free(client)
    plain, prefix, digest = generate_api_key("secret")
    async with tenant_session(None) as db:
        db.add(ApiKey(tenant_id=s.tenant_id, site_id=None, kind="secret", prefix=prefix, key_hash=digest))
    r = await s.client.post("/v1/auth/token", json={"user_id": "wp-1", "role": "owner", "site_url": "https://x.example"}, headers={"X-API-Key": plain})
    assert r.status_code == 200
    assert (await signal(s))["last_site_url"] is None


async def test_only_the_operator_sees_the_signal(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await exchange(s, site_url="https://x.example")).status_code == 200
    r = await s.client.get(f"/v1/platform/tenants/{s.tenant_id}/plan", headers=s.admin)
    assert r.status_code == 401
    for path in ("/v1/admin/site", "/v1/admin/plan"):
        assert "last_site_url" not in (await s.client.get(path, headers=s.admin)).text


# --------------------------------------------------------------------- the pure rules
def plan_of(**over: Any) -> Plan:
    base: dict[str, Any] = {"code": "t", "name": "T", "max_domains": 1, "domain_change_days": 30}
    base.update(over)
    return Plan(**base)


def tenant_of(overrides: dict[str, Any] | None = None) -> Tenant:
    return Tenant(name="x", business_type="spa", timezone="UTC", plan_overrides=overrides or {})


def test_the_change_is_due_30_days_after_the_last_one_and_never_before_the_first() -> None:
    site = Site(name="s", domain="a.example", allowed_origins=[])
    assert plans.domain_change_due(plan_of(), tenant_of(), site) is None
    site.domain_changed_at = dt.datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert plans.domain_change_due(plan_of(), tenant_of(), site) == dt.datetime(2026, 10, 31, 12, tzinfo=UTC)
    assert plans.domain_change_due(plan_of(domain_change_days=None), tenant_of(), site) is None
    assert plans.domain_change_due(plan_of(), tenant_of({"domain_change_days": 7}), site) == dt.datetime(2026, 10, 8, 12, tzinfo=UTC)
    assert plans.domain_change_due(plan_of(), tenant_of({"domain_change_days": None}), site) is None
    assert plans.domain_change_due(plan_of(), tenant_of({"domain_change_days": 0}), site) == dt.datetime(2026, 10, 1, 12, tzinfo=UTC)


def test_the_next_change_is_known_only_while_it_is_in_the_future() -> None:
    site = Site(name="s", domain="a.example", allowed_origins=[], domain_changed_at=dt.datetime(2026, 10, 1, 12, tzinfo=UTC))
    assert sites.next_domain_change(plan_of(), tenant_of(), site, dt.datetime(2026, 10, 15, tzinfo=UTC)) == dt.datetime(2026, 10, 31, 12, tzinfo=UTC)
    assert sites.next_domain_change(plan_of(), tenant_of(), site, dt.datetime(2026, 10, 31, 12, tzinfo=UTC)) is None  # (due now)
    assert sites.next_domain_change(plan_of(), tenant_of(), site, dt.datetime(2026, 12, 1, tzinfo=UTC)) is None


def test_the_domain_count_is_checked_against_the_plan_and_the_businesss_own_limit() -> None:
    plans.ensure_domain_count(plan_of(), tenant_of(), 1)
    with pytest.raises(PlanLimitReached) as caught:
        plans.ensure_domain_count(plan_of(), tenant_of(), 2)
    assert caught.value.extra == {"limit": "max_domains", "allowed": 1, "used": 2, "plan": "t"}
    plans.ensure_domain_count(plan_of(), tenant_of({"domains": 2}), 2)
    plans.ensure_domain_count(plan_of(max_domains=None), tenant_of(), 500)
    with pytest.raises(PlanLimitReached):
        plans.ensure_domain_count(plan_of(), tenant_of({"domains": 0}), 1)


def test_the_new_limits_join_the_old_ones_and_the_rule_is_not_a_count() -> None:
    assert plans.LIMIT_NAMES[-3:] == ("sites", "domains", "live_keys_per_kind") and plans.RULE_NAMES == ("domain_change_days",)
    assert plans.WARNED == ("resources", "services", "bookings_per_month", "whatsapp_per_month", "sms_per_month")
    limits = plans.limits_of(plan_of(max_sites=1, max_live_keys_per_kind=2), tenant_of({"sites": 4}))
    assert (limits.sites, limits.domains, limits.live_keys_per_kind, limits.domain_change_days) == (4, 1, 2, 30)
    used = plans.Usage(resources=0, services=0, bookings_per_month=0, whatsapp_per_month=0, sms_per_month=0, sites=2, domains=1, live_keys_per_kind=3)
    assert plans.over_limit(limits, used) == ["live_keys_per_kind"]
    assert plans.warnings(plans.Limits(1, 1, 1, 1, 1, sites=1, domains=1, live_keys_per_kind=1), plans.Usage(1, 1, 1, 1, 1, sites=1, domains=1, live_keys_per_kind=1))[0]["limit"] == "resources"
    assert {w["limit"] for w in plans.warnings(plans.Limits(1, 1, 1, 1, 1, sites=1, domains=1, live_keys_per_kind=1), plans.Usage(1, 1, 1, 1, 1, sites=1, domains=1, live_keys_per_kind=1))} == set(plans.WARNED)
    assert plans.LABELS["max_sites"] and plans.LABELS["max_domains"] and plans.LABELS["max_live_keys_per_kind"]


async def test_the_database_plans_are_read_with_the_new_columns(client: httpx.AsyncClient) -> None:
    code = await new_plan(sites=3, domains=2, live_keys_per_kind=1)
    async with tenant_session(None) as db:
        row = await db.get(Plan, code)
        assert row is not None and (row.max_sites, row.max_domains, row.max_live_keys_per_kind, row.domain_change_days) == (3, 2, 1, None)


# --------------------------------------------------------------------- development hosts, and two changes at once
async def test_a_plan_without_a_domain_limit_may_record_a_development_host(client: httpx.AsyncClient) -> None:
    s = await business(client, domain="localhost:8080")
    assert (await site_of(s)).domain == "localhost"
    for typed, host in (("127.0.0.1:8000", "127.0.0.1"), ("http://dev", "dev"), ("Staging.Shop.Example", "staging.shop.example")):
        r = await put_site(s, domain=typed)
        assert r.status_code == 200 and r.json()["domain"] == host, (typed, r.text)
    for bad in ("not a host", "-bad.example", "a..b"):
        assert (await put_site(s, domain=bad)).status_code == 422, bad
    assert (await site_of(s)).domain == "staging.shop.example"
    assert origins.host_of("localhost:8080", public=False) == "localhost" and origins.host_of("[::1]", public=False) == "::1"
    with pytest.raises(origins.WebsiteProblem):
        origins.host_of("localhost:8080")


async def test_two_changes_of_address_at_once_cannot_both_happen(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from tests.test_customer_cancel import _Together

    s = await free(client)
    together = _Together(2)
    with monkeypatch.context() as m:
        m.setattr(sites, "update_site", together.wrap(sites.update_site))  # both arrive before either has changed anything
        results = await asyncio.gather(put_site(s, domain="first.example"), put_site(s, domain="second.example"))
    assert sorted(r.status_code for r in results) == [200, 402], [r.text for r in results]
    winner = "first.example" if results[0].status_code == 200 else "second.example"
    assert (await site_of(s)).domain == winner


# --------------------------------------------------------------------- odd corners
def test_www_is_dropped_from_a_name_only_when_something_is_left() -> None:
    assert origins.host_key("www.com") == "www.com" and origins.host_key("WWW.Shop.Example.") == "shop.example" and origins.host_key("shop.example") == "shop.example"


async def test_a_site_that_was_set_up_without_an_address_gets_one_for_the_first_time_without_starting_the_wait(client: httpx.AsyncClient) -> None:
    """A business set up by the operator (no address) and later moved to the free plan: giving it its address is not a *change*."""
    s = await business(client)
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/plan", json={"plan": "free"}, headers=PLATFORM)).status_code == 200
    assert (await site_of(s)).domain is None
    assert (await put_site(s, domain="https://www.late.example/")).status_code == 200
    site = await site_of(s)
    assert site.domain == "late.example" and site.allowed_origins == ["https://late.example", "https://www.late.example"] and site.domain_changed_at is None
    assert (await put_site(s, domain="later.example")).status_code == 200  # the first real change
    assert (await put_site(s, domain="latest.example")).status_code == 402


async def test_a_recovery_keeps_the_keys_it_just_made_however_low_the_limit(client: httpx.AsyncClient) -> None:
    s = await free(client)
    assert (await client.put(f"/v1/platform/tenants/{s.tenant_id}/overrides", headers=PLATFORM, json={"live_keys_per_kind": 0})).status_code == 200
    refused_for(await s.client.post("/v1/auth/api-keys", json={"kind": "secret"}, headers=s.admin), "max_live_keys_per_kind", allowed=0)
    r = await s.client.post("/v1/auth/api-keys/recover", json={}, headers=s.admin)
    assert r.status_code == 201 and r.json()["revoked"] == 2
    after = await keys_of(s)
    assert [k["kind"] for k in live(after, "secret")] == ["secret"] and [k["kind"] for k in live(after, "publishable")] == ["publishable"]
    assert (await s.client.post("/v1/auth/token", json={"user_id": "u", "role": "owner"}, headers={"X-API-Key": r.json()["secret_key"]})).status_code == 200


async def test_a_paid_business_that_reports_an_address_but_has_no_domain_has_nothing_to_compare(client: httpx.AsyncClient) -> None:
    s = await business(client)  # paid, no domain, no origins
    assert (await exchange(s, site_url="https://whatever.example")).status_code == 200
    got = await signal(s)
    assert got["last_site_url"] == "https://whatever.example" and got["domain"] is None and got["site_url_matches_domain"] is None


# --------------------------------------------------------------------- sign-up asks for the website
from tests.test_ratelimit import redis  # noqa: E402,F401  (fixture)
from tests.test_signup import ask, open_signup, request, sign_up, tenant_by_email  # noqa: E402,F401  (``open_signup`` is a fixture)


async def test_sign_up_needs_the_website_and_nothing_is_sent_without_it(client: httpx.AsyncClient, open_signup: Any) -> None:
    body = request(domain=None)
    assert "domain" not in body
    r = await client.post("/v1/signup", json=body)
    assert r.status_code == 422 and "website address (domain) is needed" in r.text
    r = await client.post("/v1/signup", json=request(domain="   "))
    assert r.status_code == 422 and "website address (domain) is needed" in r.text
    assert open_signup.sent == []  # no email, nothing kept


@pytest.mark.parametrize("bad", ["localhost", "10.0.0.1", "shop.example:8443", "ftp://shop.example", "not a site", "x" * 301])
async def test_sign_up_refuses_an_address_that_is_not_a_public_website(client: httpx.AsyncClient, open_signup: Any, bad: str) -> None:
    r = await ask(client, domain=bad)
    assert r.status_code == 422, r.text
    assert open_signup.sent == []


async def test_sign_up_does_not_take_a_list_of_origins(client: httpx.AsyncClient, open_signup: Any) -> None:
    r = await ask(client, allowed_origins=["https://other.example"])
    assert r.status_code == 422 and "allowed_origins cannot be sent on sign-up" in r.text
    r = await ask(client, allowed_origins=[])  # an empty list says nothing: fine
    assert r.status_code == 202, r.text


async def test_a_business_that_signs_itself_up_gets_its_website_and_derived_origins(client: httpx.AsyncClient, open_signup: Any) -> None:
    body, done = await sign_up(client, open_signup, domain="https://WWW.Glow-Salon.Example/book")
    s = Seed(client, done)
    site = await site_of(s)
    assert (site.domain, site.allowed_origins, site.domain_changed_at) == ("glow-salon.example", ["https://glow-salon.example", "https://www.glow-salon.example"], None)
    tenant = await tenant_by_email(body["owner_email"].strip().lower())
    assert tenant is not None and tenant.plan == "free"
    assert [await reach(s, o) for o in ("https://glow-salon.example", "https://www.glow-salon.example", "https://other.example")] == [200, 200, 403]


def test_a_host_name_cannot_be_longer_than_dns_allows() -> None:
    long_but_wellformed = ".".join(["a" * 62] * 4) + ".com"
    assert len(long_but_wellformed) > 253 and len(long_but_wellformed) <= 300
    with pytest.raises(origins.WebsiteProblem, match="public name"):
        origins.host_of(long_but_wellformed)
    assert origins.host_of(".".join(["a" * 60] * 4) + ".com").endswith(".com")
