"""Customer gender for spas and salons (blueprint §8-9, "Customer Extensions: customer_gender"): optional personal data."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from app.core.database import tenant_session
from app.models import Customer, OutboxEvent, Tenant
from app.services import compliance
from app.services.customer_profile import GENDER_BUSINESS_TYPES, ensure_gender_allowed
from tests.conftest import Seed, at
from tests.test_automotive import gbook, make_garage
from tests.test_customer_cancel import PHONE, login, otp  # noqa: F401  (``otp`` is a fixture)
from tests.test_customer_reschedule import admin_view
from tests.test_field_service import make_practice, pbook
from tests.test_restaurant import book as rbook
from tests.test_restaurant import make_restaurant
from tests.test_salon import make_salon, sbook


def who(phone: str = PHONE, **extra: Any) -> dict[str, Any]:
    return {"name": "Aisha", "phone": phone, **extra}


async def book_with(s: Seed, staff: uuid.UUID, hour: int, **customer: Any) -> httpx.Response:
    return await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, hour), staff, customer=who(**customer)))


async def stored_gender(s: Seed, phone: str = PHONE) -> str | None:
    async with tenant_session(s.tenant_id) as db:
        return await db.scalar(select(Customer.gender).where(Customer.phone == phone))


# --------------------------------------------------------------------- the rule
def test_only_spas_and_salons_collect_gender() -> None:
    assert GENDER_BUSINESS_TYPES == {"spa", "salon"}
    ensure_gender_allowed(Tenant(business_type="spa"), "female")
    ensure_gender_allowed(Tenant(business_type="salon"), "other")
    for kind in ("restaurant", "automotive", "professional"):
        ensure_gender_allowed(Tenant(business_type=kind), None)  # saying nothing (or removing it) is always fine
        from app.core.exceptions import ValidationFailed

        with pytest.raises(ValidationFailed, match="spa and salon"):
            ensure_gender_allowed(Tenant(business_type=kind), "male")


# --------------------------------------------------------------------- at booking time
async def test_a_spa_booking_records_the_gender_and_the_admin_sees_it(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await book_with(seeded, mei, 10, gender="female")
    assert r.status_code == 201, r.text
    assert "gender" not in r.json()  # the confirmation says nothing about it
    assert await stored_gender(seeded) == "female"
    assert (await admin_view(seeded, r.json()["id"]))["customer_gender"] == "female"
    listing = (await seeded.client.get("/v1/admin/bookings", headers=seeded.admin)).json()
    assert all("customer_gender" not in b and "gender" not in b for b in listing)  # only the detail view carries it


async def test_every_admin_role_sees_it_in_the_detail_view(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = (await book_with(seeded, mei, 10, gender="male")).json()["id"]
    for role in ("staff", "manager", "owner"):
        r = await seeded.client.get(f"/v1/admin/bookings/{bid}", headers=await seeded.token(role))
        assert r.status_code == 200 and r.json()["customer_gender"] == "male", role


async def test_a_later_booking_never_changes_what_is_known(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await book_with(seeded, mei, 10, gender="female")).status_code == 201
    assert (await book_with(seeded, mei, 12)).status_code == 201  # nothing said: nothing lost
    assert await stored_gender(seeded) == "female"
    assert (await book_with(seeded, mei, 14, gender=None)).status_code == 201  # an explicit null in a booking does not erase it either
    assert await stored_gender(seeded) == "female"
    assert (await book_with(seeded, mei, 16, gender="other")).status_code == 201  # anyone can book with anyone's phone number: only the customer (signed in) changes it
    assert await stored_gender(seeded) == "female"
    bid = (await book_with(seeded, mei, 9, phone="+60198887777")).json()["id"]  # a different customer has none
    assert await stored_gender(seeded, "+60198887777") is None and (await admin_view(seeded, bid))["customer_gender"] is None


async def test_a_later_booking_fills_in_a_gender_nobody_had_given(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await book_with(seeded, mei, 10)).status_code == 201 and await stored_gender(seeded) is None
    assert (await book_with(seeded, mei, 12, gender="male")).status_code == 201
    assert await stored_gender(seeded) == "male"


@pytest.mark.parametrize("bad", ["", "Female", "nonbinary", "x", 1, True, ["female"]])
async def test_only_female_male_or_other_is_accepted(seeded: Seed, bad: Any) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await book_with(seeded, mei, 10, gender=bad)).status_code == 422
    assert await stored_gender(seeded) is None


async def test_a_salon_collects_it_too(client: httpx.AsyncClient) -> None:
    salon = await make_salon(client)
    r = await sbook(salon, at(salon.day, 10), ["cut"], stylist="Lin", customer=who(gender="male"))
    assert r.status_code == 201, r.text
    assert await stored_gender(salon) == "male" and (await admin_view(salon, r.json()["id"]))["customer_gender"] == "male"


async def test_other_kinds_of_business_do_not_collect_it(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    refused = await rbook(rest, at(rest.day, 19), 2, customer=who(gender="female"))
    assert refused.status_code == 422 and "spa and salon" in refused.json()["detail"]
    ok = await rbook(rest, at(rest.day, 19), 2, customer=who(gender=None))
    assert ok.status_code == 201 and await stored_gender(rest) is None
    garage = await make_garage(client)
    assert (await gbook(garage, at(garage.day, 9), ["oil"], customer=who(gender="male"))).status_code == 422
    practice = await make_practice(client)
    assert (await pbook(practice, at(practice.day, 10), "office", customer=who(gender="other"))).status_code == 422
    for s in (rest, garage, practice):
        async with tenant_session(s.tenant_id) as db:
            assert (await db.scalars(select(Customer.gender).where(Customer.gender.is_not(None)))).all() == []


async def test_a_refused_gender_leaves_no_booking_behind(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    assert (await rbook(rest, at(rest.day, 19), 2, customer=who(gender="female"))).status_code == 422
    assert (await rest.client.get("/v1/admin/bookings", headers=rest.admin)).json() == []
    async with tenant_session(rest.tenant_id) as db:
        assert (await db.scalars(select(Customer.id))).all() == []  # not even the customer row


# --------------------------------------------------------------------- the signed-in customer's own record
async def test_a_customer_sees_and_changes_their_gender(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await book_with(seeded, mei, 10, email="aisha@example.com")).status_code == 201
    me = await login(seeded)
    first = await seeded.client.get("/v1/public/me/profile", headers=me)
    assert first.status_code == 200 and first.json() == {"name": "Aisha", "phone": PHONE, "email": "aisha@example.com", "email_verified_at": None, "gender": None}
    r = await seeded.client.patch("/v1/public/me/profile", json={"gender": "female"}, headers=me)
    assert r.status_code == 200 and r.json()["gender"] == "female" and await stored_gender(seeded) == "female"
    assert (await seeded.client.get("/v1/public/me/profile", headers=me)).json()["gender"] == "female"
    cleared = await seeded.client.patch("/v1/public/me/profile", json={"gender": None}, headers=me)
    assert cleared.status_code == 200 and cleared.json()["gender"] is None and await stored_gender(seeded) is None


async def test_the_profile_refuses_what_it_does_not_take(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await book_with(seeded, mei, 10)).status_code == 201
    me = await login(seeded)
    for body in ({}, {"gender": "robot"}, {"name": "Someone Else"}, {"gender": "male", "email": "x@example.com"}, {"phone": "+60123459990"}):
        assert (await seeded.client.patch("/v1/public/me/profile", json=body, headers=me)).status_code == 422, body
    assert await stored_gender(seeded) is None
    assert (await seeded.client.get("/v1/public/me/profile", headers=seeded.pk)).status_code == 401
    assert (await seeded.client.patch("/v1/public/me/profile", json={"gender": "male"}, headers=seeded.pk)).status_code == 401
    assert (await seeded.client.get("/v1/public/me/profile")).status_code == 401


async def test_a_restaurant_customer_cannot_set_it_but_can_clear_it(client: httpx.AsyncClient, otp: Any) -> None:
    rest = await make_restaurant(client)
    assert (await rbook(rest, at(rest.day, 19), 2, customer=who())).status_code == 201
    me = await login(rest)
    refused = await rest.client.patch("/v1/public/me/profile", json={"gender": "male"}, headers=me)
    assert refused.status_code == 422 and "spa and salon" in refused.json()["detail"]
    assert (await rest.client.patch("/v1/public/me/profile", json={"gender": None}, headers=me)).status_code == 200
    assert (await rest.client.get("/v1/public/me/profile", headers=me)).json()["gender"] is None


async def test_one_customers_profile_is_not_anothers(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await book_with(seeded, mei, 10, gender="female")).status_code == 201
    assert (await book_with(seeded, mei, 12, phone="+60177776666")).status_code == 201
    other = await login(seeded, "+60177776666")
    assert (await seeded.client.get("/v1/public/me/profile", headers=other)).json()["gender"] is None
    assert (await seeded.client.patch("/v1/public/me/profile", json={"gender": "male"}, headers=other)).status_code == 200
    assert await stored_gender(seeded) == "female" and await stored_gender(seeded, "+60177776666") == "male"


# --------------------------------------------------------------------- privacy
async def test_gender_is_in_the_access_export_and_gone_after_erasure(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = (await book_with(seeded, mei, 10, gender="female")).json()["id"]
    done = await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "completed"}, headers=seeded.admin)
    assert done.status_code == 200
    async with tenant_session(seeded.tenant_id) as db:
        tenant = await db.get(Tenant, seeded.tenant_id)
        customer = await db.scalar(select(Customer))
        assert tenant is not None and customer is not None
        export = await compliance.export_customer(db, tenant, customer.id)
        assert export["customer"]["gender"] == "female"
        await compliance.erase_customer(db, tenant, customer.id, dt.datetime.now(dt.UTC))
    assert await stored_gender(seeded, "erased-" + customer.id.hex[:24]) is None
    async with tenant_session(seeded.tenant_id) as db:
        assert (await db.scalars(select(Customer.gender))).all() == [None]


async def test_gender_never_reaches_events_webhooks_or_messages(seeded: Seed, mail: Any) -> None:
    from tests.test_notifications import run_outbox

    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
        at(seeded.day, 10), mei, customer=who(email="aisha@example.com", gender="female")))
    assert r.status_code == 201
    bid = r.json()["id"]
    assert (await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "completed"}, headers=seeded.admin)).status_code == 200
    await run_outbox(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        payloads = [str(e.payload) for e in (await db.scalars(select(OutboxEvent))).all()]
    assert payloads and not any("female" in p or "gender" in p for p in payloads)
    assert mail.sent and not any("female" in m.body or "gender" in m.body.lower() for m in mail.sent)
    me_view = await seeded.client.get("/v1/public/me/bookings", headers=seeded.pk)
    assert me_view.status_code == 401  # (the customer's own booking list carries no gender field either: see the schema)
    from app.schemas.public import BookingConfirmation, CustomerBookingOut

    assert "gender" not in BookingConfirmation.model_fields and "gender" not in CustomerBookingOut.model_fields


async def test_the_database_itself_refuses_other_values(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    assert (await book_with(seeded, mei, 10)).status_code == 201
    with pytest.raises(IntegrityError, match="ck_customers_gender"):
        async with tenant_session(seeded.tenant_id) as db:
            await db.execute(text("UPDATE customers SET gender = 'robot'"))
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE customers SET gender = 'other'"))  # an allowed value still goes in
        await db.execute(text("UPDATE customers SET gender = NULL"))
