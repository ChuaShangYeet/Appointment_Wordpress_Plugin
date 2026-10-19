"""Customer identity: phone-number normalisation and email magic-link login."""
from __future__ import annotations

import importlib.util
import itertools
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import ValidationFailed
from app.core.phone import normalise_email, normalise_phone
from app.core.security import decode_jwt
from tests.conftest import PLATFORM, Seed, at, make_seed
from tests.test_customer_cancel import PHONE, book, login, otp, request_code, verify  # noqa: F401


# --------------------------------------------------------------------- pure: the normaliser
@pytest.mark.parametrize("raw", [
    "+60123450001", "+60 12-345 0001", "+60 (12) 345.0001", "0123450001", "012-345 0001", "(012) 345 0001",
    "0060123450001", "60123450001", "  +60123450001  ",
])
def test_every_spelling_of_one_malaysian_number_is_one_number(raw: str) -> None:
    assert normalise_phone(raw, "60") == "+60123450001"


@pytest.mark.parametrize(("raw", "code", "expected"), [
    ("91234567", "65", "+6591234567"),  # Singapore has no trunk 0
    ("+65 9123 4567", "65", "+6591234567"),
    ("6591234567", "65", "+6591234567"),
    ("07911 123456", "44", "+447911123456"),
    ("+44 7911 123456", "60", "+447911123456"),  # an explicit + wins over the business's country
    ("004479 11123456", "60", "+447911123456"),
])
def test_other_countries(raw: str, code: str, expected: str) -> None:
    assert normalise_phone(raw, code) == expected


@pytest.mark.parametrize("raw", [
    "", " ", "+", "abc", "12", "+60-12-345-xx", "+0123456789", "+601234500012345678", "012 345 0001 ext 5",
    "٠١٢٣٤٥٦٧٨٩", "+60123450001;", "erased-0123456789abcdef", "0",
])
def test_things_that_cannot_be_a_phone_number_are_refused(raw: str) -> None:
    with pytest.raises(ValidationFailed):
        normalise_phone(raw, "60")


def test_normalising_twice_changes_nothing() -> None:
    for raw, code in itertools.product(["012-3450001", "+65 9123 4567", "60123450001", "0060123450001"], ["60", "65"]):
        once = normalise_phone(raw, code)
        assert normalise_phone(once, code) == once and normalise_phone(once, "1") == once


def test_emails_match_without_regard_to_case_or_padding() -> None:
    assert normalise_email("  Aisha@Example.COM ") == "aisha@example.com"


def test_the_migration_applies_the_same_rule_as_the_application() -> None:
    """The migration carries its own copy of the rule (a migration must never change meaning); the two must agree today."""
    path = Path(__file__).parent.parent / "alembic" / "versions" / "0014_customer_identity.py"
    spec = importlib.util.spec_from_file_location("m0014", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    samples = ["012-3450001", "+60 12 3450001", "0060123450001", "60123450001", "91234567", "abc", "12", "+0123", "0", "٠١٢٣٤٥٦٧٨٩", "erased-0123"]
    for raw, code in itertools.product(samples, ["60", "65"]):
        try:
            expected: str | None = normalise_phone(raw, code)
        except ValidationFailed:
            expected = None
        assert module._normalise(raw, code) == expected, (raw, code)


# --------------------------------------------------------------------- through the API
async def customers_of(s: Seed) -> list[tuple[str, str | None]]:
    async with tenant_session(s.tenant_id) as db:
        return [(p, e) for p, e in (await db.execute(text("SELECT phone, email FROM customers ORDER BY created_at"))).all()]


async def test_one_person_typing_their_number_differently_is_one_customer(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    first = await book(seeded, mei, at(seeded.day, 10), phone="012-345 0001")
    second = await book(seeded, mei, at(seeded.day, 12), phone="+60 12 3450001")
    assert await customers_of(seeded) == [("+60123450001", None)]
    mine = (await seeded.client.get("/v1/public/me/bookings", headers=await login(seeded, "0060123450001"))).json()
    assert sorted(b["id"] for b in mine) == sorted([first, second])  # however it is typed at login, too


async def test_a_code_belongs_to_the_number_not_its_spelling(seeded: Seed, otp: Any) -> None:
    assert (await request_code(seeded, "012-345 0001")).status_code == 202
    assert (await verify(seeded, "+60123450001")).status_code == 200
    token = (await verify(seeded, "+60123450001"))  # used up
    assert token.status_code == 401


async def test_unusable_numbers_are_refused_everywhere(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "A", "phone": "not a phone"}), headers=seeded.pk)
    assert r.status_code == 422 and "phone" in r.json()["detail"]
    assert (await request_code(seeded, "abcdefgh")).status_code == 422
    assert (await verify(seeded, "abcdefgh")).status_code == 422
    assert await customers_of(seeded) == []


async def test_a_business_in_another_country_completes_local_numbers_with_its_own_code(client: httpx.AsyncClient, otp: Any) -> None:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "Singapore Spa", "business_type": "spa", "phone_country_code": "65"})
    assert r.status_code == 201, r.text
    s = Seed(client, r.json())
    s.admin = await s.token("owner")
    assert (await client.post("/v1/admin/policies", headers=s.admin, json={"name": "D", "default_status": "confirmed", "min_notice_booking": 60, "min_notice_cancel": 3600, "booking_window": 86400 * 60, "pending_timeout": 1800, "make_default": True})).status_code == 201
    svc = await client.post("/v1/admin/services", headers=s.admin, json={"name": "Massage", "duration_minutes": 60}, )
    s.service_id = uuid.UUID(svc.json()["id"])
    mei = await s.add_staff("Mei")
    ok = await client.post("/v1/public/bookings", json=s.booking_body(at(s.day, 10), mei, customer={"name": "Wei", "phone": "9123 4567"}), headers=s.pk)
    assert ok.status_code == 201, ok.text
    assert await customers_of(s) == [("+6591234567", None)]
    assert (await login(s, "91234567")).get("Authorization")


async def test_the_country_code_given_at_onboarding_must_be_digits(client: httpx.AsyncClient) -> None:
    for bad in ("", "+60", "0", "6o", "1234"):
        r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": "X", "phone_country_code": bad})
        assert r.status_code == 422, bad


# --------------------------------------------------------------------- magic link
EMAIL = "aisha@example.com"


@pytest.fixture
def links(otp: Any, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Predictable link tokens, in the order issued."""
    from app.api.v1 import auth as auth_module

    issued: list[str] = []

    def fake() -> str:
        issued.append(f"magic-token-{len(issued):02d}-" + "x" * 30)
        return issued[-1]

    monkeypatch.setattr(auth_module, "generate_magic_token", fake)
    return issued


async def ask(s: Seed, email: str = EMAIL, **extra: Any) -> httpx.Response:
    return await s.client.post("/v1/public/auth/magic-link", json={"email": email, **extra}, headers=s.pk)


async def redeem(s: Seed, token: str) -> httpx.Response:
    return await s.client.post("/v1/public/auth/magic-link/verify", json={"token": token}, headers=s.pk)


async def booked_with_email(s: Seed, phone: str = PHONE, email: str = EMAIL, hour: int = 10) -> str:
    staff = await s.add_staff(f"Mei{uuid.uuid4().hex[:4]}")
    r = await s.client.post(
        "/v1/public/bookings", headers=s.pk,
        json=s.booking_body(at(s.day, hour), staff, customer={"name": "Aisha", "phone": phone, "email": email}),
    )
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_the_emailed_link_signs_the_customer_in(seeded: Seed, links: list[str]) -> None:
    bid = await booked_with_email(seeded)
    r = await ask(seeded)
    assert r.status_code == 202 and r.json() == {"status": "sent"} and len(links) == 1
    token = (await redeem(seeded, links[0]))
    assert token.status_code == 200 and token.json()["token_type"] == "bearer"
    claims = decode_jwt(token.json()["access_token"])
    assert claims["role"] == "customer" and claims["tid"] == str(seeded.tenant_id)
    mine = (await seeded.client.get("/v1/public/me/bookings", headers={**seeded.pk, "Authorization": f"Bearer {token.json()['access_token']}"})).json()
    assert [b["id"] for b in mine] == [bid]  # the very same customer the phone login reaches


async def test_a_link_works_once(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded)
    await ask(seeded)
    assert (await redeem(seeded, links[0])).status_code == 200
    assert (await redeem(seeded, links[0])).status_code == 401


async def test_emails_match_in_any_case(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded, email="Aisha.Karim@Example.com")
    assert (await ask(seeded, "AISHA.KARIM@example.COM")).status_code == 202
    assert len(links) == 1 and (await redeem(seeded, links[0])).status_code == 200


async def test_the_answer_does_not_reveal_whether_an_email_is_registered(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded)
    known, unknown = await ask(seeded), await ask(seeded, "nobody@example.com")
    assert (known.status_code, known.json()) == (unknown.status_code, unknown.json()) == (202, {"status": "sent"})
    assert len(links) == 1  # a link was made for the known address only
    assert (await redeem(seeded, "never-issued-" + "y" * 30)).status_code == 401


async def test_the_link_expires(seeded: Seed, links: list[str], otp: Any) -> None:
    await booked_with_email(seeded)
    await ask(seeded)
    ttl = [await otp.ttl(k) for k in await otp.keys("magic:*:t:*")]
    assert ttl and all(0 < t <= get_settings().magic_link_ttl_seconds for t in ttl)
    await otp.flushall()  # time passed
    assert (await redeem(seeded, links[0])).status_code == 401


async def test_the_token_is_not_kept_in_clear_and_belongs_to_one_business(client: httpx.AsyncClient, links: list[str], otp: Any) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await booked_with_email(a)
    await ask(a)
    stored = [k for k in await otp.keys("*")] + [str(await otp.get(k)) for k in await otp.keys("*") if await otp.type(k) == "string"]
    assert not any(links[0] in item for item in stored)
    assert (await redeem(b, links[0])).status_code == 401  # a link for business A means nothing at business B
    assert (await redeem(a, links[0])).status_code == 200


async def test_a_business_cannot_send_links_for_another_businesss_customers(client: httpx.AsyncClient, links: list[str]) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await booked_with_email(a)
    assert (await ask(b)).status_code == 202 and links == []


async def test_requests_are_rate_limited_per_address(seeded: Seed, links: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    await booked_with_email(seeded)
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 60)
    assert (await ask(seeded)).status_code == 202
    assert (await ask(seeded)).status_code == 429
    assert (await ask(seeded, "AISHA@example.com")).status_code == 429  # spelling does not dodge it
    assert (await ask(seeded, "someone.else@example.com")).status_code == 202  # others are independent
    assert (await ask(seeded, "nobody@example.com")).status_code == 202 and len(links) == 1
    assert (await ask(seeded, "nobody@example.com")).status_code == 429  # and unknown addresses are limited the same way


async def test_only_the_newest_link_works(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded)
    await ask(seeded)
    await ask(seeded)  # the test settings have no cool-down
    assert len(links) == 2
    assert (await redeem(seeded, links[0])).status_code == 401
    assert (await redeem(seeded, links[1])).status_code == 200


async def test_two_customers_sharing_an_email_are_told_apart_by_phone(seeded: Seed, links: list[str]) -> None:
    mine = await booked_with_email(seeded, "+60111110001", EMAIL, hour=10)
    theirs = await booked_with_email(seeded, "+60111110002", EMAIL, hour=12)
    await ask(seeded)
    assert links == []  # ambiguous: no link rather than a guess
    await ask(seeded, phone="+60199999999")
    assert links == []  # a phone that matches nobody with this email
    await ask(seeded, phone="0111-110002")
    assert len(links) == 1
    token = (await redeem(seeded, links[0])).json()["access_token"]
    listing = (await seeded.client.get("/v1/public/me/bookings", headers={**seeded.pk, "Authorization": f"Bearer {token}"})).json()
    assert [b["id"] for b in listing] == [theirs] and mine != theirs


async def test_customers_without_an_email_and_erased_customers_get_no_link(seeded: Seed, links: list[str]) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await book(seeded, mei, at(seeded.day, 10))) and (await ask(seeded)).status_code == 202 and links == []
    await booked_with_email(seeded, "+60122220001", "gone@example.com", hour=12)
    await ask(seeded, "gone@example.com")
    assert len(links) == 1
    async with tenant_session(seeded.tenant_id) as db:  # erased after the link was sent (the email column is left alone: the flag itself must be enough)
        await db.execute(text("UPDATE customers SET erased_at = now() WHERE phone = '+60122220001'"))
    assert (await redeem(seeded, links[0])).status_code == 401
    assert (await ask(seeded, "gone@example.com")).status_code == 202 and len(links) == 1


async def test_the_link_gives_a_customer_token_not_a_staff_one(seeded: Seed, links: list[str]) -> None:
    await booked_with_email(seeded)
    await ask(seeded)
    token = (await redeem(seeded, links[0])).json()["access_token"]
    admin = await seeded.client.get("/v1/admin/bookings", headers={"Authorization": f"Bearer {token}", **seeded.sk})
    assert admin.status_code == 403
    assert (await seeded.client.get("/v1/public/me/bookings", headers={"Authorization": f"Bearer {token}"})).status_code == 401  # and still needs the site key


async def test_malformed_requests_are_rejected(seeded: Seed, links: list[str]) -> None:
    assert (await ask(seeded, "not-an-email")).status_code == 422
    assert (await seeded.client.post("/v1/public/auth/magic-link", json={}, headers=seeded.pk)).status_code == 422
    assert (await redeem(seeded, "short")).status_code == 422
    assert (await seeded.client.post("/v1/public/auth/magic-link/verify", json={"token": "x" * 300}, headers=seeded.pk)).status_code == 422
    assert (await seeded.client.post("/v1/public/auth/magic-link", json={"email": EMAIL})).status_code == 401  # no site key
