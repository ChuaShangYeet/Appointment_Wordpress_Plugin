"""Who booked, in the admin booking views: ``customer_name``, ``customer_phone`` and the booking's own ``contact_email`` (2026-10-15).

Before this the admin list and detail carried only ``customer_id``, so a business dashboard could not show who had booked. Every admin role sees all three (decided
2026-10-15: staff call a customer who is late or missing); a staff member tied to a resource still sees only their own appointments (``tests/test_role_matrix.py``).
An erased customer shows the anonymised name and no phone; the list loads its customers in one query, however many rows it has.
"""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.models import Customer, Tenant
from app.services import compliance
from app.services.compliance import ERASED_NAME
from tests.conftest import Seed, at
from tests.test_automotive import gbook, make_garage
from tests.test_field_service import NEAR, make_practice, pbook, pin
from tests.test_field_service import status as pstatus
from tests.test_salon import make_salon, sbook, upsell
from tests.test_salon import status as sstatus
from tests.test_security_review import Statements


async def book(s: Seed, staff: uuid.UUID, hour: int, name: str, phone: str, email: str | None = None) -> str:
    customer: dict[str, Any] = {"name": name, "phone": phone}
    if email is not None:
        customer["email"] = email
    r = await s.client.post("/v1/public/bookings", headers=s.pk, json=s.booking_body(at(s.day, hour), staff, customer=customer))
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def listed(s: Seed, headers: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    r = await s.client.get("/v1/admin/bookings", headers=headers or s.admin)
    assert r.status_code == 200, r.text
    return {b["id"]: b for b in r.json()}


async def detail(s: Seed, bid: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = await s.client.get(f"/v1/admin/bookings/{bid}", headers=headers or s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def contact(b: dict[str, Any]) -> tuple[Any, Any, Any]:
    return b["customer_name"], b["customer_phone"], b["contact_email"]


# --------------------------------------------------------------------- what the list and the detail show
async def test_the_list_and_the_detail_show_who_booked_and_each_row_its_own_customer(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    a = await book(seeded, mei, 10, "Aisha", "+60123334444", "Aisha@Example.com")
    b = await book(seeded, mei, 11, "Ben", "+60125556666")
    rows = await listed(seeded)
    assert contact(rows[a]) == ("Aisha", "+60123334444", "aisha@example.com")  # the email as stored with the booking (lower-cased)
    assert contact(rows[b]) == ("Ben", "+60125556666", None)  # no email given: none shown
    assert contact(await detail(seeded, a)) == contact(rows[a]) and contact(await detail(seeded, b)) == contact(rows[b])


async def test_the_email_is_the_one_given_with_that_booking_not_the_customers(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    first = await book(seeded, mei, 10, "Aisha", "+60123334444", "aisha@example.com")
    second = await book(seeded, mei, 11, "Aisha", "+60123334444", "someone.else@example.com")  # same phone, so the same customer
    rows = await listed(seeded)
    assert rows[first]["customer_id"] == rows[second]["customer_id"]
    assert rows[first]["contact_email"] == "aisha@example.com" and rows[second]["contact_email"] == "someone.else@example.com"
    async with tenant_session(seeded.tenant_id) as db:
        assert (await db.scalar(select(Customer.email))) == "aisha@example.com"  # the customer's own email was not replaced, and is not what the booking shows


async def test_every_admin_role_sees_the_name_phone_and_email(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, 10, "Aisha", "+60123334444", "aisha@example.com")
    expected = ("Aisha", "+60123334444", "aisha@example.com")
    for role in ("staff", "manager", "owner"):
        headers = await seeded.token(role)
        assert contact((await listed(seeded, headers))[bid]) == expected, role
        assert contact(await detail(seeded, bid, headers)) == expected, role
    tied = await seeded.token("staff", mei)  # the stylist whose appointment it is
    assert contact((await listed(seeded, tied))[bid]) == expected and contact(await detail(seeded, bid, tied)) == expected


async def test_the_routes_that_change_a_booking_answer_with_who_booked_too(seeded: Seed) -> None:
    """A dashboard refreshes a row from the answer; a change must not blank the customer out."""
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, 10, "Aisha", "+60123334444", "aisha@example.com")
    expected = ("Aisha", "+60123334444", "aisha@example.com")
    moved = await seeded.client.post(f"/v1/admin/bookings/{bid}/reschedule", json={"start_at": at(seeded.day, 15).isoformat()}, headers=seeded.admin)
    assert moved.status_code == 200, moved.text
    assert contact(moved.json()) == expected
    cleared = await seeded.client.post(f"/v1/admin/bookings/{bid}/clear-flags", headers=seeded.admin)
    assert cleared.status_code == 200 and contact(cleared.json()) == expected
    status = await seeded.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "completed"}, headers=await seeded.token("staff"))
    assert status.status_code == 200, status.text
    assert contact(status.json()) == expected


# --------------------------------------------------------------------- erasure
async def test_an_erased_customer_shows_the_anonymised_name_and_no_phone_or_email(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    gone = await book(seeded, mei, 10, "Aisha", "+60123334444", "aisha@example.com")
    kept = await book(seeded, mei, 11, "Ben", "+60125556666", "ben@example.com")
    done = await seeded.client.patch(f"/v1/admin/bookings/{gone}/status", json={"status": "completed"}, headers=seeded.admin)
    assert done.status_code == 200, done.text
    async with tenant_session(seeded.tenant_id) as db:
        tenant = await db.get(Tenant, seeded.tenant_id)
        customer = await db.scalar(select(Customer).where(Customer.phone == "+60123334444"))
        assert tenant is not None and customer is not None
        await compliance.erase_customer(db, tenant, customer.id, dt.datetime.now(dt.UTC))
    rows = await listed(seeded)
    assert contact(rows[gone]) == (ERASED_NAME, None, None) == ("Erased customer", None, None)
    assert contact(await detail(seeded, gone)) == (ERASED_NAME, None, None)
    for role in ("staff", "manager"):
        assert contact(await detail(seeded, gone, await seeded.token(role))) == (ERASED_NAME, None, None), role
    assert contact(rows[kept]) == ("Ben", "+60125556666", "ben@example.com")  # the other customer is untouched
    assert all("erased-" not in str(b) for b in rows.values())  # the placeholder stored in place of the phone never shows


# --------------------------------------------------------------------- one query, however long the list
async def test_the_list_loads_its_customers_in_one_query(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    for hour, n in ((9, 1), (10, 2), (11, 3), (12, 4), (14, 5)):
        await book(seeded, mei, hour, f"Customer {n}", f"+6012000000{n}")
    with Statements() as sql:
        rows = await listed(seeded)
    assert sorted(b["customer_name"] for b in rows.values()) == [f"Customer {n}" for n in range(1, 6)]
    assert len([s for s, _ in sql.seen if "FROM customers" in s]) == 1


async def test_an_empty_list_is_still_an_empty_list(seeded: Seed) -> None:
    assert await listed(seeded) == {}


async def test_a_detail_view_still_shows_the_gender_from_the_same_customer(client: httpx.AsyncClient, seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
        at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": "+60123334444", "gender": "female"}))
    assert r.status_code == 201, r.text
    body = await detail(seeded, str(r.json()["id"]))
    assert body["customer_gender"] == "female" and body["customer_name"] == "Aisha"


# --------------------------------------------------------------------- the routes of the other business types answer with who booked too
WHO = {"name": "Aisha", "phone": "+60127778888"}
ANSWER = ("Aisha", "+60127778888", None)


async def test_an_add_on_answers_with_who_booked(client: httpx.AsyncClient) -> None:
    s = await make_salon(client)
    bid = (await sbook(s, at(s.day, 10), ["cut"], stylist="Mei", customer=WHO)).json()["id"]
    assert (await sstatus(s, bid, "in-progress")).status_code == 200
    r = await upsell(s, bid, ["gel"])
    assert r.status_code == 200, r.text
    assert contact(r.json()["booking"]) == ANSWER


async def test_a_meeting_link_an_overrun_and_a_resolved_conflict_answer_with_who_booked(client: httpx.AsyncClient) -> None:
    p = await make_practice(client)
    video = (await pbook(p, at(p.day, 15), "video", customer=WHO)).json()["id"]
    link = await p.client.patch(f"/v1/admin/bookings/{video}/meeting-link", json={"url": "https://meet.example/abc"}, headers=p.admin)
    assert link.status_code == 200, link.text
    assert contact(link.json()) == ANSWER
    first = (await pbook(p, at(p.day, 10), "office", customer=WHO)).json()["id"]
    assert (await pstatus(p, first, "in-progress")).status_code == 200
    assert (await pbook(p, at(p.day, 11), "office")).status_code == 201  # right behind it, so the overrun collides
    over = await p.client.patch(f"/v1/admin/bookings/{first}/actual-duration", json={"minutes": 90}, headers=p.admin)
    assert over.status_code == 200 and over.json()["booking"]["schedule_conflict"] is True
    assert contact(over.json()["booking"]) == ANSWER
    resolved = await p.client.post(f"/v1/admin/bookings/{first}/resolve-conflict", headers=p.admin)
    assert resolved.status_code == 200 and contact(resolved.json()) == ANSWER


@pytest.mark.usefixtures("postgis")
async def test_a_follow_up_answers_with_who_booked(client: httpx.AsyncClient) -> None:
    p = await make_practice(client)
    site = (await pbook(p, at(p.day, 9, 30), "site", address=pin(NEAR), customer=WHO)).json()["id"]
    assert (await pstatus(p, site, "in-progress")).status_code == 200
    r = await p.client.post(f"/v1/admin/bookings/{site}/follow-up", json={"service_id": str(p.svc["work"]), "start_at": at(p.day + dt.timedelta(days=1), 10).isoformat()},
                            headers=p.admin)
    assert r.status_code == 201, r.text
    assert contact(r.json()) == ANSWER


async def test_a_resumed_job_answers_with_who_booked(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    bid = (await gbook(g, at(g.day, 9), ["oil"], customer=WHO)).json()["id"]
    for step in ("in-progress", "waiting-for-parts"):
        assert (await pstatus(g, bid, step)).status_code == 200
    r = await g.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": at(g.day + dt.timedelta(days=2), 10).isoformat()}, headers=g.admin)
    assert r.status_code == 200, r.text
    assert contact(r.json()) == ANSWER
