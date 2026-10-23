"""Setting a business up in any country (Malaysia, Singapore and Australia in detail): country, time zone, phone code, address (migrations 0022,
0034), and what follows from them."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError

from app.core import countries, ratelimit
from app.core.database import tenant_session
from app.models import AuditLog, BusinessProfile, Tenant
from tests.conftest import PLATFORM, Seed, at, set_zone
from tests.test_availability_cache import assert_invalidates, avail, cache  # noqa: F401  (``cache`` is a fixture)
from tests.test_customer_cancel import login, otp  # noqa: F401  (``otp`` is a fixture)
from tests.test_customer_identity import customers_of
from tests.test_field_service import NEAR, Practice, make_practice, pbook, practice  # noqa: F401  (``practice`` is a fixture)
from tests.test_geocoding import HOME, FakeMaps, found, maps, typed  # noqa: F401  (``maps`` is a fixture)

POLICY = {"name": "Default", "default_status": "confirmed", "min_notice_booking": 60, "min_notice_cancel": 86400, "booking_window": 60 * 86400,
          "pending_timeout": 1800, "make_default": True}
KL_HOME = {"address_line1": "12 Jalan Bukit", "city": "Kuala Lumpur", "state": "KUL", "postcode": "50450"}


async def signup(client: httpx.AsyncClient, **body: Any) -> httpx.Response:
    if body.get("plan") == "free" and "domain" not in body:
        body["domain"] = "freebiz.example"  # (the free plan needs the business's website: one account, one business, one website)
    return await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Biz {uuid.uuid4().hex[:6]}", "business_type": "spa", **body})


async def business(client: httpx.AsyncClient, **body: Any) -> Seed:
    """A spa set up in whatever country the body names, with its owner's token, a policy and a one-hour service."""
    r = await signup(client, **body)
    assert r.status_code == 201, r.text
    s = Seed(client, r.json())
    s.admin = await s.token("owner")
    assert (await client.post("/v1/admin/policies", headers=s.admin, json=POLICY)).status_code == 201
    r = await client.post("/v1/admin/services", headers=s.admin, json={"name": "Massage", "duration_minutes": 60})
    assert r.status_code == 201, r.text
    s.service_id = uuid.UUID(r.json()["id"])
    return s


async def page(s: Seed) -> dict[str, Any]:
    r = await s.client.get("/v1/public/business", headers=s.pk)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def tenant_row(tenant_id: uuid.UUID) -> Tenant:
    async with tenant_session(None) as db:
        row = await db.get(Tenant, tenant_id)
    assert row is not None
    return row


# --------------------------------------------------------------------- the country settles the rest
@pytest.mark.parametrize(("country", "zone", "code", "currency"), [
    ("MY", "Asia/Kuala_Lumpur", "60", "MYR"), ("SG", "Asia/Singapore", "65", "SGD"), ("AU", "Australia/Sydney", "61", "AUD"),
    ("US", "America/New_York", "1", "USD"), ("GB", "Europe/London", "44", "GBP"), ("JP", "Asia/Tokyo", "81", "JPY"),
])
async def test_a_country_brings_its_zone_phone_code_and_currency(client: httpx.AsyncClient, country: str, zone: str, code: str, currency: str) -> None:
    s = await business(client, country=country)
    row = await tenant_row(s.tenant_id)
    assert (row.country, row.timezone, row.phone_country_code) == (country, zone, code)
    shown = await page(s)
    assert (shown["country"], shown["currency"], shown["timezone"]) == (country, currency, zone)


async def test_a_business_that_names_no_country_is_malaysian(client: httpx.AsyncClient) -> None:
    s = await business(client)
    row = await tenant_row(s.tenant_id)
    assert (row.country, row.timezone, row.phone_country_code) == ("MY", "Asia/Kuala_Lumpur", "60")


async def test_an_australian_business_may_pick_any_australian_zone(client: httpx.AsyncClient) -> None:
    for zone in ("Australia/Perth", "Australia/Brisbane", "Australia/Lord_Howe"):
        s = await business(client, country="AU", timezone=zone)
        assert (await tenant_row(s.tenant_id)).timezone == zone


async def test_a_business_elsewhere_picks_from_its_own_countrys_zones(client: httpx.AsyncClient) -> None:
    s = await business(client, country="US", timezone="America/Los_Angeles")
    assert (await tenant_row(s.tenant_id)).timezone == "America/Los_Angeles"
    r = await signup(client, country="US", timezone="Europe/London")
    assert r.status_code == 422 and "not one of United States's time zones" in r.text, r.text


@pytest.mark.parametrize(("body", "words"), [
    ({"country": "AU", "timezone": "Asia/Kuala_Lumpur"}, "not one of Australia's time zones"),
    ({"country": "SG", "timezone": "Asia/Kuala_Lumpur"}, "Asia/Singapore"),
    ({"timezone": "Asia/Singapore"}, "set country if the business is elsewhere"),  # no country: it is Malaysia, and Singapore's zone is not
    ({"country": "AU", "timezone": "australia/sydney"}, "time zones"),
    ({"country": "AU", "timezone": "Australia/Canberra"}, "time zones"),
    ({"timezone": "Mars/Olympus"}, "time zones"),
    ({"timezone": ""}, "time zones"),
])
async def test_a_time_zone_the_country_does_not_have_is_refused(client: httpx.AsyncClient, body: dict[str, Any], words: str) -> None:
    r = await signup(client, **body)
    assert r.status_code == 422 and words in r.text, r.text


@pytest.mark.parametrize("country", ["XX", "AQ", "my", "", "MYS", "Malaysia", None])
async def test_only_a_countrys_two_letter_code_is_taken(client: httpx.AsyncClient, country: str | None) -> None:
    assert (await signup(client, country=country)).status_code == 422


async def test_a_phone_code_alone_still_names_the_country(client: httpx.AsyncClient) -> None:
    for code, country, zone in (("60", "MY", "Asia/Kuala_Lumpur"), ("65", "SG", "Asia/Singapore"), ("61", "AU", "Australia/Sydney")):
        row = await tenant_row(uuid.UUID((await signup(client, phone_country_code=code)).json()["tenant_id"]))
        assert (row.country, row.timezone, row.phone_country_code) == (country, zone, code)


async def test_a_phone_code_of_another_country_is_refused(client: httpx.AsyncClient) -> None:
    other = await signup(client, phone_country_code="44")
    assert other.status_code == 422 and "none of Malaysia (60), Singapore (65) and Australia (61)" in other.text
    clash = await signup(client, country="SG", phone_country_code="60")
    assert clash.status_code == 422 and "not Singapore's (65)" in clash.text
    assert (await signup(client, country="SG", phone_country_code="65")).status_code == 201  # the right one is fine
    assert (await signup(client, country="GB", phone_country_code="44")).status_code == 201  # (44 alone is not enough: the country is named)


async def test_numbers_typed_locally_get_the_country_code_of_the_business(client: httpx.AsyncClient, otp: Any) -> None:
    s = await business(client, country="AU")
    staff = await s.add_staff("Mei")
    day = (dt.datetime.now(ZoneInfo("Australia/Sydney")) + dt.timedelta(days=3)).date()
    start = dt.datetime.combine(day, dt.time(10), tzinfo=ZoneInfo("Australia/Sydney"))
    r = await client.post("/v1/public/bookings", json=s.booking_body(start, staff, customer={"name": "Mia", "phone": "0412 345 678"}), headers=s.pk)
    assert r.status_code == 201, r.text
    assert await customers_of(s) == [("+61412345678", None)]


async def test_the_database_itself_refuses_what_is_not_a_country_code(client: httpx.AsyncClient) -> None:
    s = await business(client)
    for wrong in ("nz", "N"):  # (three letters do not fit the column at all)
        with pytest.raises(IntegrityError):
            async with tenant_session(None) as db:
                await db.execute(update(Tenant).where(Tenant.id == s.tenant_id).values(country=wrong))
    async with tenant_session(None) as db:  # (which two capitals are a country is the service's to say, not the database's)
        await db.execute(update(Tenant).where(Tenant.id == s.tenant_id).values(country="NZ"))


# --------------------------------------------------------------------- the address given at sign-up
async def test_an_address_given_at_sign_up_becomes_the_profile(client: httpx.AsyncClient) -> None:
    s = await business(client, address_line2="Taman Maju", **{**KL_HOME, "state": "kuala lumpur", "postcode": "50 450"})
    shown = await page(s)
    assert shown["address"] == "12 Jalan Bukit, Taman Maju, 50450 Kuala Lumpur, Kuala Lumpur"
    assert (shown["address_line1"], shown["address_line2"], shown["city"], shown["state"], shown["postcode"]) == (
        "12 Jalan Bukit", "Taman Maju", "Kuala Lumpur", "KUL", "50450")
    assert shown["business_name"].startswith("Biz ")
    async with tenant_session(s.tenant_id) as db:
        assert (await db.scalar(select(BusinessProfile.postcode))) == "50450"


async def test_an_australian_and_a_singaporean_address(client: httpx.AsyncClient) -> None:
    au = await business(client, country="AU", address_line1="1 George St", city="Sydney", state="New South Wales", postcode="2000")
    assert (await page(au))["address"] == "1 George St, Sydney NSW 2000" and (await page(au))["state"] == "NSW"
    sg = await business(client, country="SG", address_line1="1 Raffles Place", postcode="048616")
    shown = await page(sg)
    assert shown["address"] == "1 Raffles Place, Singapore 048616" and shown["state"] is None and shown["city"] == "Singapore"


async def test_an_address_elsewhere_keeps_its_region_as_written_and_may_have_no_postcode(client: httpx.AsyncClient) -> None:
    ca = await business(client, country="CA", timezone="America/Toronto", address_line1="290 Bremner Blvd", city="Toronto", state="Ontario", postcode="m5v 3l9")
    shown = await page(ca)
    assert (shown["address"], shown["state"], shown["postcode"]) == ("290 Bremner Blvd, Toronto, Ontario M5V 3L9", "Ontario", "M5V 3L9")
    ae = await business(client, country="AE", address_line1="1 Sheikh Mohammed bin Rashid Blvd", city="Dubai")
    shown = await page(ae)
    assert (shown["address"], shown["state"], shown["postcode"], shown["currency"]) == ("1 Sheikh Mohammed bin Rashid Blvd, Dubai", None, None, "AED")
    long_region = "Île-de-France " + "x" * 86  # (the longest a region may be: 100 characters, kept in the database as written)
    fr = await business(client, country="FR", timezone="Europe/Paris", address_line1="5 Avenue Anatole France", city="Paris", state=long_region)
    assert (await page(fr))["state"] == long_region
    r = await signup(client, country="GB", address_line1="10 Downing St", postcode="SW1A 2AA")
    assert r.status_code == 422 and "city or town is needed" in r.text, r.text


async def test_without_an_address_there_is_no_profile_yet(client: httpx.AsyncClient) -> None:
    s = await business(client)
    shown = await page(s)
    assert shown["address"] is None and shown["address_line1"] is None and shown["business_name"].startswith("Biz ")
    async with tenant_session(s.tenant_id) as db:
        assert await db.scalar(select(BusinessProfile)) is None


@pytest.mark.parametrize(("body", "words"), [
    ({"country": "AU", **KL_HOME}, "not a state or territory of Australia"),
    ({"address_line1": "12 Jalan Bukit", "city": "Kuala Lumpur", "state": "KUL"}, "postcode is needed"),
    ({**KL_HOME, "postcode": "5045"}, "5 digits"),
    ({**KL_HOME, "state": None}, "state is needed"),
    ({**KL_HOME, "city": None}, "city or suburb is needed"),
    ({"city": "Kuala Lumpur", "state": "KUL", "postcode": "50450"}, "street"),
    ({"country": "SG", "address_line1": "1 Raffles Place", "state": "Central", "postcode": "048616"}, "no states"),
    ({"country": "SG", "address_line1": "1 Raffles Place", "postcode": "50450"}, "6 digits"),
])
async def test_an_address_that_does_not_fit_its_country_stops_the_sign_up(client: httpx.AsyncClient, body: dict[str, Any], words: str) -> None:
    name = f"Refused {uuid.uuid4().hex[:6]}"
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": name, "business_type": "spa", **body})
    assert r.status_code == 422 and words in r.text, r.text
    async with tenant_session(None) as db:
        assert await db.scalar(select(Tenant.id).where(Tenant.name == name)) is None  # nothing was created


# --------------------------------------------------------------------- GET /v1/countries
async def test_the_sign_up_page_can_list_the_countries_without_a_key(client: httpx.AsyncClient) -> None:
    r = await client.get("/v1/countries")
    assert r.status_code == 200, r.text
    names = [c["name"] for c in r.json()]
    assert len(names) == len(countries.COUNTRIES) and names[:2] == ["Afghanistan", "Åland Islands"] and names[-1] == "Zimbabwe"  # (every country, by name, accents aside)
    body = {c["code"]: c for c in r.json()}
    assert len(body["MY"]["states"]) == 16 and body["SG"]["states"] == [] and len(body["AU"]["states"]) == 8
    assert [z["timezone"] for z in body["MY"]["timezones"]] == ["Asia/Kuala_Lumpur"] and len(body["AU"]["timezones"]) == 10
    for c in body.values():
        assert c["default_timezone"] == c["timezones"][0]["timezone"]
        assert all(ZoneInfo(z["timezone"]) and z["label"] for z in c["timezones"])
        if c["exact_address"]:
            assert len(c["postcode_example"]) == c["postcode_digits"] and c["privacy_law"]
        else:
            assert (c["postcode_digits"], c["postcode_example"], c["states"], c["privacy_law"]) == (None, None, [], None)
    assert [code for code, c in body.items() if c["exact_address"]] == ["AU", "MY", "SG"]
    assert (body["AU"]["calling_code"], body["AU"]["currency"], body["AU"]["states"][0]) == ("61", "AUD", {"code": "NSW", "name": "New South Wales"})
    assert (body["US"]["calling_code"], body["US"]["currency"], body["US"]["default_timezone"]) == ("1", "USD", "America/New_York")
    assert ratelimit.classify("GET", "/v1/countries") is None  # a fixed list, read from memory


def test_the_listed_examples_are_valid_postcodes_and_every_state_can_be_found() -> None:
    for c in countries.DETAILED:
        assert c.postcode_example and countries.normalise_postcode(c, c.postcode_example) == c.postcode_example
        for code, name in c.states:
            assert countries.find_state(c, code) == countries.find_state(c, name) == code


# --------------------------------------------------------------------- the profile's address
async def put_profile(s: Seed, **body: Any) -> httpx.Response:
    return await s.client.put("/v1/admin/business-profile", json={"business_name": "Glow", **body}, headers=s.admin)


async def test_the_profile_address_can_be_saved_in_parts(client: httpx.AsyncClient) -> None:
    s = await business(client)
    r = await put_profile(s, address="typed by hand", address_line1="12 Jalan Bukit", city="Petaling Jaya", state="Selangor", postcode="47800")
    assert r.status_code == 200, r.text
    assert r.json()["address"] == "12 Jalan Bukit, 47800 Petaling Jaya, Selangor" and r.json()["state"] == "SGR"  # written from the parts, not the typed text
    assert (await s.client.get("/v1/admin/business-profile", headers=s.admin)).json() == r.json()
    assert (await page(s))["postcode"] == "47800"


async def test_saving_the_profile_again_without_parts_keeps_the_typed_address_and_clears_the_parts(client: httpx.AsyncClient) -> None:
    s = await business(client)
    await put_profile(s, address_line1="12 Jalan Bukit", city="Ipoh", state="PRK", postcode="30000")
    r = await put_profile(s, address="Near the old station")
    assert r.status_code == 200 and r.json()["address"] == "Near the old station"
    assert (r.json()["address_line1"], r.json()["city"], r.json()["state"], r.json()["postcode"]) == (None, None, None, None)


async def test_parts_sent_blank_are_no_parts_at_all(client: httpx.AsyncClient) -> None:
    s = await business(client)
    r = await put_profile(s, address="Near the old station", address_line1="", address_line2=" ", city="", state="", postcode="")
    assert r.status_code == 200, r.text
    assert (r.json()["address"], r.json()["address_line1"], r.json()["address_line2"], r.json()["city"], r.json()["state"], r.json()["postcode"]) == (
        "Near the old station", None, None, None, None, None)  # not empty strings kept in the database


@pytest.mark.parametrize(("body", "words"), [
    ({"address_line1": "12 Jalan Bukit", "city": "Ipoh", "state": "PRK"}, "postcode is needed"),
    ({"address_line1": "12 Jalan Bukit", "city": "Ipoh", "state": "PRK", "postcode": "300"}, "5 digits"),
    ({"address_line1": "12 Jalan Bukit", "city": "Ipoh", "state": "NSW", "postcode": "30000"}, "not a state or territory of Malaysia"),
    ({"postcode": "30000"}, "street"),
])
async def test_a_profile_address_in_parts_is_checked_against_the_businesss_country(client: httpx.AsyncClient, body: dict[str, Any], words: str) -> None:
    s = await business(client)
    r = await put_profile(s, **body)
    assert r.status_code == 422 and words in r.text, r.text
    assert (await s.client.get("/v1/admin/business-profile", headers=s.admin)).json()["address"] is None  # nothing was saved


async def test_the_same_parts_mean_different_things_in_another_country(client: httpx.AsyncClient) -> None:
    sg = await business(client, country="SG")
    assert (await put_profile(sg, address_line1="1 Raffles Place", postcode="048616")).status_code == 200
    assert (await put_profile(sg, address_line1="1 Raffles Place", postcode="50450")).status_code == 422  # a Malaysian postcode
    au = await business(client, country="AU")
    assert (await put_profile(au, address_line1="1 George St", city="Sydney", state="NSW", postcode="2000")).json()["address"] == "1 George St, Sydney NSW 2000"
    assert (await put_profile(au, address_line1="1 George St", city="Sydney", state="NSW", postcode="20000")).status_code == 422


# --------------------------------------------------------------------- business settings
async def test_managers_read_the_settings_and_only_owners_change_the_time_zone(client: httpx.AsyncClient) -> None:
    s = await business(client, country="AU")
    manager = await s.token("manager")
    seen = await client.get("/v1/admin/business-settings", headers=manager)
    assert seen.status_code == 200
    body = seen.json()
    assert (body["country"], body["country_name"], body["currency"], body["phone_country_code"], body["timezone"]) == ("AU", "Australia", "AUD", "61", "Australia/Sydney")
    assert len(body["timezone_choices"]) == 10 and body["timezone_choices"][0]["timezone"] == "Australia/Sydney"
    assert (await client.get("/v1/admin/business-settings", headers=await s.token("staff"))).status_code == 403
    assert (await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Perth"}, headers=manager)).status_code == 403
    assert (await tenant_row(s.tenant_id)).timezone == "Australia/Sydney"
    done = await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Perth"}, headers=s.admin)
    assert done.status_code == 200 and done.json()["timezone"] == "Australia/Perth"
    assert (await tenant_row(s.tenant_id)).timezone == "Australia/Perth" and (await page(s))["timezone"] == "Australia/Perth"


async def test_the_time_zone_must_be_one_the_country_has(client: httpx.AsyncClient) -> None:
    s = await business(client, country="AU")
    for zone in ("Asia/Kuala_Lumpur", "Pacific/Auckland", "Australia/Nowhere", "", "australia/perth"):
        r = await client.put("/v1/admin/business-settings", json={"timezone": zone}, headers=s.admin)
        assert r.status_code == 422, zone
    assert (await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Perth", "country": "MY"}, headers=s.admin)).status_code == 422  # the country is fixed
    assert (await client.put("/v1/admin/business-settings", json={}, headers=s.admin)).status_code == 422
    assert (await tenant_row(s.tenant_id)).timezone == "Australia/Sydney"


async def test_moving_to_another_zone_moves_the_working_day(client: httpx.AsyncClient) -> None:
    """Shifts are clock times: 09:00 in Perth is not 09:00 in Sydney."""
    s = await business(client, country="AU")
    await s.add_staff("Mei")
    s.day = (dt.datetime.now(ZoneInfo("Australia/Sydney")) + dt.timedelta(days=5)).date()

    def first_start(r: httpx.Response) -> dt.datetime:
        return dt.datetime.fromisoformat(r.json()["slots"][0]["start_at"])

    sydney = first_start(await avail(s))
    assert sydney.astimezone(ZoneInfo("Australia/Sydney")).strftime("%H:%M") == "09:00"
    assert (await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Perth"}, headers=s.admin)).status_code == 200
    perth = first_start(await avail(s))
    assert perth.astimezone(ZoneInfo("Australia/Perth")).strftime("%H:%M") == "09:00"
    assert perth != sydney  # a different moment: 09:00 on the Perth clock (two or three hours later than Sydney's)
    assert (await avail(s)).json()["timezone"] == "Australia/Perth"


async def test_the_answers_kept_in_the_cache_do_not_outlive_a_change_of_zone(client: httpx.AsyncClient, cache: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    s = await business(client, country="AU")
    await s.add_staff("Mei")
    s.day = (dt.datetime.now(ZoneInfo("Australia/Sydney")) + dt.timedelta(days=5)).date()

    async def write() -> None:
        assert (await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Perth"}, headers=s.admin)).status_code == 200

    await assert_invalidates(s, monkeypatch, write)


async def test_a_change_of_zone_that_still_covers_the_bookings_flags_nothing(client: httpx.AsyncClient) -> None:
    s = await business(client, country="AU")
    mei = await s.add_staff("Mei")
    day = (dt.datetime.now(ZoneInfo("Australia/Sydney")) + dt.timedelta(days=5)).date()
    start = dt.datetime.combine(day, dt.time(11), tzinfo=ZoneInfo("Australia/Sydney"))  # 10:00 or 11:00 on the Brisbane clock: still inside 09:00-18:00
    assert (await client.post("/v1/public/bookings", json=s.booking_body(start, mei), headers=s.pk)).status_code == 201
    moved = await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Brisbane"}, headers=s.admin)
    assert moved.status_code == 200 and moved.headers["X-Bookings-Flagged"] == "0"
    (booking,) = (await client.get("/v1/admin/bookings", headers=s.admin)).json()
    assert booking["status"] == "confirmed" and booking["flags"] == []


async def test_a_change_of_zone_that_pushes_a_booking_outside_the_shift_flags_it(client: httpx.AsyncClient) -> None:
    s = await business(client, country="AU", timezone="Australia/Perth")
    mei = await s.add_staff("Mei")
    day = (dt.datetime.now(ZoneInfo("Australia/Perth")) + dt.timedelta(days=5)).date()
    start = dt.datetime.combine(day, dt.time(16), tzinfo=ZoneInfo("Australia/Perth"))
    assert (await client.post("/v1/public/bookings", json=s.booking_body(start, mei), headers=s.pk)).status_code == 201
    moved = await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Sydney"}, headers=s.admin)  # 16:00 Perth is 19:00 Sydney: after the shift ends
    assert moved.status_code == 200 and moved.headers["X-Bookings-Flagged"] == "1"
    (booking,) = (await client.get("/v1/admin/bookings", headers=s.admin)).json()
    assert booking["status"] == "confirmed" and "outside_shift" in booking["flags"]  # flagged for a person to look at, never cancelled


async def test_choosing_the_zone_it_already_has_changes_nothing(client: httpx.AsyncClient) -> None:
    s = await business(client, country="AU")
    r = await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Sydney"}, headers=s.admin)
    assert r.status_code == 200 and "X-Bookings-Flagged" not in r.headers


async def test_a_change_of_zone_is_in_the_audit_log(client: httpx.AsyncClient) -> None:
    s = await business(client, country="AU")
    assert (await client.put("/v1/admin/business-settings", json={"timezone": "Australia/Perth"}, headers=s.admin)).status_code == 200
    async with tenant_session(s.tenant_id) as db:
        actions = (await db.scalars(select(AuditLog.action))).all()
    assert "PUT /v1/admin/business-settings" in actions


# --------------------------------------------------------------------- customers' addresses follow the business's country
async def test_a_customers_postcode_is_kept_in_the_countrys_shape(seeded: Seed, otp: Any, maps: FakeMaps) -> None:
    me = await login(seeded)
    created = await seeded.client.post("/v1/public/me/addresses", json={**typed(postcode="50 450"), "latitude": 3.1, "longitude": 101.7}, headers=me)
    assert created.status_code == 201 and created.json()["postcode"] == "50450"
    bad = await seeded.client.post("/v1/public/me/addresses", json={**typed(postcode="5045"), "latitude": 3.1, "longitude": 101.7}, headers=me)
    assert bad.status_code == 422 and "5 digits" in bad.text
    none = await seeded.client.post("/v1/public/me/addresses", json={**typed(postcode=None), "latitude": 3.1, "longitude": 101.7}, headers=me)
    assert none.status_code == 201 and none.json()["postcode"] is None
    blank = await seeded.client.post("/v1/public/me/addresses", json={**typed(postcode="  "), "latitude": 3.1, "longitude": 101.7}, headers=me)
    assert blank.status_code == 201 and blank.json()["postcode"] is None
    assert maps.requests == []


async def test_changing_a_saved_postcode_is_checked_too(seeded: Seed, otp: Any) -> None:
    me = await login(seeded)
    aid = (await seeded.client.post("/v1/public/me/addresses", json={**typed(), "latitude": 3.1, "longitude": 101.7}, headers=me)).json()["id"]
    assert (await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"postcode": "ABC12"}, headers=me)).status_code == 422
    ok = await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"postcode": "47 800", "latitude": 3.2, "longitude": 101.6}, headers=me)
    assert ok.status_code == 200 and ok.json()["postcode"] == "47800"
    cleared = await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"postcode": None}, headers=me)
    assert cleared.status_code == 200 and cleared.json()["postcode"] is None


async def test_a_singapore_business_takes_six_digit_postcodes_and_an_australian_one_four(client: httpx.AsyncClient, otp: Any) -> None:
    sg, au = await business(client, country="SG"), await business(client, country="AU")
    pin = {"latitude": 1.3, "longitude": 103.8}
    me = await login(sg)
    assert (await sg.client.post("/v1/public/me/addresses", json={**typed(postcode="50450"), **pin}, headers=me)).status_code == 422
    assert (await sg.client.post("/v1/public/me/addresses", json={**typed(postcode="048616"), **pin}, headers=me)).status_code == 201
    me = await login(au)
    assert (await au.client.post("/v1/public/me/addresses", json={**typed(postcode="50450"), **pin}, headers=me)).status_code == 422
    assert (await au.client.post("/v1/public/me/addresses", json={**typed(postcode="2000"), **pin}, headers=me)).status_code == 201


async def test_an_on_site_booking_with_a_postcode_of_another_country_is_refused_before_any_lookup(practice: Practice, maps: FakeMaps, postgis: None) -> None:
    r = await pbook(practice, at(practice.day, 10), "site", address=typed(postcode="5045"))
    assert r.status_code == 422 and "5 digits" in r.text
    assert maps.requests == []  # a lookup is paid for: nothing is sent for an address that cannot be right
    maps.places[HOME] = found(*NEAR)
    assert (await pbook(practice, at(practice.day, 10), "site", address=typed(postcode="50 450"))).status_code == 201
    assert maps.asked() == [HOME]  # the postcode went out without its space


@pytest.mark.parametrize(("country", "region"), [("MY", "my"), ("SG", "sg"), ("AU", "au")])
async def test_every_lookup_leans_towards_the_businesss_country(client: httpx.AsyncClient, otp: Any, maps: FakeMaps, country: str, region: str) -> None:
    p = await make_practice(client)
    await set_zone(p.tenant_id, countries.get(country).default_timezone, country=country)
    maps.places["12 Main St"] = found(*NEAR)
    assert (await client.post("/v1/public/geocode", json={"address": "12 Main St"}, headers=p.pk)).status_code == 200
    assert maps.requests[-1].url.params["region"] == region
    business_location = await client.put("/v1/admin/business-location", json={"address": "12 Main St", "service_radius_km": 5}, headers=p.admin)
    assert business_location.status_code == 200, business_location.text
    assert maps.requests[-1].url.params["region"] == region
    me = await login(p)
    pinless = {"line1": "12 Main St"}
    saved = await client.post("/v1/public/me/addresses", json=pinless, headers=me)
    assert saved.status_code == 201
    assert maps.requests[-1].url.params["region"] == region
    asked = len(maps.requests)
    await client.patch(f"/v1/public/me/addresses/{saved.json()['id']}", json={"line1": "99 Other St"}, headers=me)  # new words, no new pin: looked up again
    assert len(maps.requests) == asked + 1 and maps.requests[-1].url.params["region"] == region
    await pbook(p, at(p.day, 10), "site", address={"line1": "77 Third St"})  # (not found: the booking is refused, but the question was asked)
    assert len(maps.requests) == asked + 2 and maps.requests[-1].url.params["region"] == region
