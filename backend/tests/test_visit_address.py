"""The admin's booking detail says where an on-site visit goes (2026-10-21): the address to drive to and its map pin. The pin is what an admin
needs to look up free times when a job runs early or late and the next visit has to move."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from app.core.database import tenant_session
from app.services import compliance
from tests.conftest import at
from tests.test_field_service import NEAR, Practice, pbook, pin, practice, status  # noqa: F401  (practice: fixture)

pytestmark = pytest.mark.usefixtures("postgis")


async def visit(p: Practice, hour: int = 10, line1: str = "12 Jalan Bukit") -> dict[str, Any]:
    r = await pbook(p, at(p.day, hour), "site", address=pin(NEAR, line1) | {"city": "Kuala Lumpur", "postcode": "50450"})
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def detail(p: Practice, bid: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    r = await p.client.get(f"/v1/admin/bookings/{bid}", headers=headers or p.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def test_the_owner_sees_where_the_visit_goes(practice: Practice) -> None:
    b = await visit(practice)
    where = (await detail(practice, b["id"]))["visit_address"]
    assert where == {"line1": "12 Jalan Bukit", "city": "Kuala Lumpur", "postcode": "50450", "latitude": NEAR[0], "longitude": NEAR[1]}


async def test_the_technician_doing_the_job_sees_it_too(practice: Practice) -> None:
    b = await visit(practice)
    me = await practice.token("staff", resource_id=practice.pros["Dr Lim"])
    assert (await detail(practice, b["id"], me))["visit_address"]["line1"] == "12 Jalan Bukit"
    manager = await practice.token("manager")
    assert (await detail(practice, b["id"], manager))["visit_address"]["line1"] == "12 Jalan Bukit"


async def test_another_technician_does_not(practice: Practice) -> None:
    b = await visit(practice)
    other = await practice.add_staff("Dr Tan")
    r = await practice.client.get(f"/v1/admin/bookings/{b['id']}", headers=await practice.token("staff", resource_id=other))
    assert r.status_code in (403, 404)
    assert "Jalan Bukit" not in r.text


async def test_bookings_at_the_office_have_none(practice: Practice) -> None:
    r = await pbook(practice, at(practice.day, 10), "office")
    assert r.status_code == 201, r.text
    assert (await detail(practice, r.json()["id"]))["visit_address"] is None


async def test_the_pin_finds_free_times_to_move_the_visit_to(practice: Practice) -> None:
    """What the admin website does: read the pin from the detail, ask for that day's free times there, move the visit to one."""
    b = await visit(practice, hour=10)
    where = (await detail(practice, b["id"]))["visit_address"]
    q = {"date": practice.day.isoformat(), "service_ids": str(practice.svc["site"]), "latitude": where["latitude"], "longitude": where["longitude"]}
    r = await practice.client.get("/v1/public/availability", params=q, headers=practice.pk)
    assert r.status_code == 200, r.text
    later = [s["start_at"] for s in r.json()["slots"] if dt.datetime.fromisoformat(s["start_at"]) >= at(practice.day, 15)]
    assert later, r.json()
    moved = await practice.client.post(f"/v1/admin/bookings/{b['id']}/reschedule", json={"start_at": later[0], "any_available": True}, headers=practice.admin)
    assert moved.status_code == 200, moved.text
    after = await detail(practice, b["id"])
    assert dt.datetime.fromisoformat(after["start_at"]) == dt.datetime.fromisoformat(later[0])
    assert after["visit_address"] == where  # (the visit still goes to the same place)


async def test_an_erased_customer_leaves_no_address(practice: Practice) -> None:
    b = await visit(practice)
    customer = uuid.UUID((await detail(practice, b["id"]))["customer_id"])  # (the public answer does not name the customer)
    assert (await status(practice, b["id"], "cancelled")).status_code == 200
    async with tenant_session(practice.tenant_id) as db:
        tenant = await db.get(compliance.Tenant, practice.tenant_id)
        assert tenant is not None
        await compliance.erase_customer(db, tenant, customer, dt.datetime.now(dt.UTC))
        await db.commit()
    assert (await detail(practice, b["id"]))["visit_address"] is None
