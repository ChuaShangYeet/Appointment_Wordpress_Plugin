"""A customer's mobile number: the country code they choose in front of it, and the length and kind it must have there.

Numbers new to a business must be mobiles of the chosen country (Google's numbering plans, ``phonenumbers``); numbers a
business already has keep working.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from app.core.countries import COUNTRIES
from app.core.exceptions import InvalidPhone
from app.core.phone import mobile_example, mobile_lengths, mobile_number, trunk_prefix
from app.services import phones
from tests.conftest import Seed, at, make_seed
from tests.test_customer_cancel import CODE, otp  # noqa: F401


# --------------------------------------------------------------------- pure: the check
@pytest.mark.parametrize(("raw", "expected"), [
    ("012-345 6789", "+60123456789"),  # 9 digits after +60
    ("011-2345 6789", "+601123456789"),  # 10 digits after +60
    ("0123456789", "+60123456789"),
    ("+60 12-345 6789", "+60123456789"),
    ("0060 12 345 6789", "+60123456789"),
    ("12 345 6789", "+60123456789"),
    ("  (012) 345.6789 ", "+60123456789"),
])
def test_a_malaysian_mobile_of_9_or_10_digits_is_one_number_however_it_is_typed(raw: str, expected: str) -> None:
    assert mobile_number(raw, "MY") == expected


@pytest.mark.parametrize("raw", ["012-345 678", "12345678", "011-2345 67890", "+60 12 345 67890"])
def test_a_malaysian_mobile_of_another_length_is_refused_with_what_it_should_look_like(raw: str) -> None:
    with pytest.raises(InvalidPhone) as e:
        mobile_number(raw, "MY")
    assert str(e.value) == "That is not a mobile number in Malaysia: it has 9 or 10 digits after +60, such as 012-345 6789"


@pytest.mark.parametrize(("raw", "region"), [("03-2282 4410", "MY"), ("6123 4567", "SG"), ("02 9374 4000", "AU")])
def test_a_landline_is_refused_because_codes_go_by_whatsapp_or_sms(raw: str, region: str) -> None:
    with pytest.raises(InvalidPhone) as e:
        mobile_number(raw, region)
    assert "landline" in str(e.value)


@pytest.mark.parametrize(("raw", "region", "expected"), [
    ("9123 4567", "SG", "+6591234567"),
    ("8123 4567", "SG", "+6581234567"),
    ("0412 345 678", "AU", "+61412345678"),
    ("(201) 555-0123", "US", "+12015550123"),  # the United States cannot tell a mobile by its number: accepted
    ("07400 123456", "GB", "+447400123456"),
])
def test_other_countries_read_numbers_their_own_way(raw: str, region: str, expected: str) -> None:
    assert mobile_number(raw, region) == expected


def test_a_number_typed_with_its_country_code_keeps_that_country_whatever_was_chosen() -> None:
    assert mobile_number("+65 9123 4567", "MY") == "+6591234567"
    assert mobile_number("0065 9123 4567", "MY") == "+6591234567"
    with pytest.raises(InvalidPhone) as e:
        mobile_number("+44 7400 12", "MY")  # the message is about the United Kingdom's numbers, not Malaysia's
    assert str(e.value).startswith("That is not a mobile number in United Kingdom: it has 10 digits after +44")


def test_00_in_front_means_international_even_where_people_dial_011_abroad() -> None:
    assert mobile_number("0060 12 345 6789", "US") == "+60123456789"


def test_a_code_that_is_no_country_gets_the_chosen_countrys_message_not_a_crash() -> None:
    with pytest.raises(InvalidPhone) as e:
        mobile_number("+800 12", "MY")  # (+800 is a worldwide freephone code, not a country)
    assert "Malaysia" in str(e.value)


def test_a_number_chosen_for_one_country_is_not_read_as_another() -> None:
    """The bug this fixes: a Singapore number typed on a Malaysian business's page used to become +60 9123 4567."""
    with pytest.raises(InvalidPhone):
        mobile_number("9123 4567", "MY")


@pytest.mark.parametrize("raw", ["", "abc", "+", "+60-12-345-xx", "012 345 6789 ext 5"])
def test_what_is_no_number_at_all_is_refused(raw: str) -> None:
    with pytest.raises(InvalidPhone):
        mobile_number(raw, "MY")


def test_the_numbering_plans_describe_the_mobiles_of_every_country_a_business_can_be_in() -> None:
    for code in COUNTRIES:
        assert mobile_lengths(code) and mobile_example(code), code
        assert mobile_number(mobile_example(code) or "", code).startswith("+"), code  # (each country's own example passes its own check)


def test_checking_twice_changes_nothing() -> None:
    for raw, region in [("012-345 6789", "MY"), ("9123 4567", "SG"), ("0412 345 678", "AU")]:
        once = mobile_number(raw, region)
        assert mobile_number(once, region) == once and mobile_number(once, "US") == once


def test_lengths_and_examples_for_the_form() -> None:
    assert mobile_lengths("MY") == [9, 10] and mobile_lengths("SG") == [8] and mobile_lengths("AU") == [9]
    assert mobile_example("MY") == "012-345 6789" and mobile_example("SG") == "8123 4567"
    assert (trunk_prefix("MY"), trunk_prefix("SG"), trunk_prefix("RU"), trunk_prefix("AU")) == ("0", None, "8", "0")  # (the 0 dialled at home in Malaysia; nothing in Singapore)


def test_a_number_is_looked_up_as_it_was_stored() -> None:
    """The email link's ``phone`` finds a customer by the number as booking stored it: strictly where it is a mobile (Cote d'Ivoire keeps its leading 0), else spelled the
    old way with the chosen country's code."""
    assert phones.spelled("01 23 45 6789", "CI") == "+2250123456789"
    assert phones.spelled("9123 456", "SG") == "+659123456"


# --------------------------------------------------------------------- through the API
async def customers_of(s: Seed) -> list[str]:
    async with tenant_session(s.tenant_id) as db:
        return [p for (p,) in (await db.execute(text("SELECT phone FROM customers ORDER BY created_at"))).all()]


async def book(s: Seed, phone: str, hour: int = 10, **customer: Any) -> httpx.Response:
    staff = await s.add_staff(f"Mei{hour}")
    body = s.booking_body(at(s.day, hour), staff, customer={"name": "Wei", "phone": phone, **customer})
    return await s.client.post("/v1/public/bookings", json=body, headers=s.pk)


async def code_for(s: Seed, phone: str, **extra: Any) -> httpx.Response:
    return await s.client.post("/v1/public/auth/otp", json={"phone": phone, **extra}, headers=s.pk)


async def sign_in(s: Seed, phone: str, **extra: Any) -> httpx.Response:
    return await s.client.post("/v1/public/auth/otp/verify", json={"phone": phone, "code": CODE, **extra}, headers=s.pk)


async def on_file(s: Seed, phone: str) -> None:
    """A customer saved before numbers were checked per country, with a number the check refuses today."""
    async with tenant_session(s.tenant_id) as db:
        await db.execute(text("INSERT INTO customers (tenant_id, name, phone) VALUES (:t, 'Old', :p)"), {"t": s.tenant_id, "p": phone})


async def test_the_customer_chooses_the_country_in_front_of_the_number(seeded: Seed, otp: Any) -> None:
    r = await book(seeded, "9123 4567", phone_country="SG")
    assert r.status_code == 201, r.text
    assert await customers_of(seeded) == ["+6591234567"]
    assert (await code_for(seeded, "9123 4567", phone_country="SG")).status_code == 202
    assert (await sign_in(seeded, "9123 4567", phone_country="SG")).status_code == 200
    assert await customers_of(seeded) == ["+6591234567"]  # (signing in found the customer who booked, not a new one)
    assert (await code_for(seeded, "9123 4567", phone_country="SG")).status_code == 202
    assert (await sign_in(seeded, "+65 9123 4567")).status_code == 200  # the same number however it is given


async def test_left_out_the_country_is_the_businesss_and_a_number_of_another_country_is_refused(seeded: Seed, otp: Any) -> None:
    r = await book(seeded, "9123 4567")  # a Singapore number on a Malaysian business's page, without choosing Singapore
    assert r.status_code == 422 and r.json()["error"] == "invalid_phone"
    assert "Malaysia" in r.json()["detail"] and "9 or 10 digits after +60" in r.json()["detail"]
    assert (await code_for(seeded, "9123 4567")).status_code == 422
    assert await customers_of(seeded) == []


async def test_a_new_number_of_the_wrong_length_or_a_landline_is_refused_everywhere(seeded: Seed, otp: Any) -> None:
    for phone in ("012-345 678", "03-2282 4410"):
        r = await book(seeded, phone)
        assert r.status_code == 422 and r.json()["error"] == "invalid_phone", phone
        assert (await code_for(seeded, phone)).status_code == 422
        assert (await sign_in(seeded, phone)).status_code == 422
    assert await customers_of(seeded) == []


async def test_an_unknown_or_badly_written_country_is_refused(seeded: Seed, otp: Any) -> None:
    for wrong in ("ZZ", "sg", "SGP", ""):
        assert (await book(seeded, "9123 4567", phone_country=wrong)).status_code == 422, wrong
        assert (await code_for(seeded, "9123 4567", phone_country=wrong)).status_code == 422, wrong
    assert await customers_of(seeded) == []


async def test_a_number_already_on_file_keeps_working(seeded: Seed, otp: Any) -> None:
    await on_file(seeded, "+6012345678")  # 8 digits after +60: refused for a new customer
    r = await book(seeded, "012-345 678")
    assert r.status_code == 201, r.text
    assert await customers_of(seeded) == ["+6012345678"]  # the same customer, not a new one
    assert (await code_for(seeded, "012-345 678")).status_code == 202
    assert (await sign_in(seeded, "+60 12-345 678")).status_code == 200


async def test_a_number_on_file_at_another_business_does_not_count(seeded: Seed, client: httpx.AsyncClient, otp: Any) -> None:
    other = await make_seed(client)
    await on_file(other, "+6012345678")
    assert (await book(seeded, "012-345 678")).status_code == 422
    assert (await code_for(seeded, "012-345 678")).status_code == 422


async def test_the_email_link_reads_the_phone_in_the_chosen_country(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api.v1 import auth as auth_module

    assert (await book(seeded, "9123 4567", phone_country="SG", email="wei@example.com")).status_code == 201
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE customers SET email = 'wei@example.com', email_verified_at = now()"))
    sent: list[str] = []
    monkeypatch.setattr(auth_module, "generate_magic_token", lambda: sent.append("x") or "m" * 40)
    for country, found in (("SG", True), (None, False)):  # read as Malaysian, 9123 4567 is nobody's number (no link is made)
        sent.clear()
        body = {"email": "wei@example.com", "phone": "9123 4567", **({"phone_country": country} if country else {})}
        assert (await seeded.client.post("/v1/public/auth/magic-link", json=body, headers=seeded.pk)).status_code == 202
        assert bool(sent) is found, country


async def test_the_countries_list_says_what_a_mobile_looks_like(client: httpx.AsyncClient) -> None:
    by_code = {c["code"]: c for c in (await client.get("/v1/countries")).json()}
    assert by_code["MY"]["mobile_example"] == "012-345 6789" and by_code["MY"]["mobile_lengths"] == [9, 10]
    assert by_code["SG"]["mobile_lengths"] == [8] and (by_code["MY"]["trunk_prefix"], by_code["SG"]["trunk_prefix"]) == ("0", None)
    assert all(c["mobile_example"] and c["mobile_lengths"] for c in by_code.values())
