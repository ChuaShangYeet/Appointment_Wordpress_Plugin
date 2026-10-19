"""Parts on a workshop job order and their stock (blueprint §10.1 purchased_parts, §10.2 Parts Ordering Status, §11.1)."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select, text

from app.core.database import tenant_session
from app.models import BookingProduct
from app.services import booking as booking_module
from app.services import parts as parts_module
from tests.conftest import Seed, at
from tests.test_automotive import Garage, garage, gbook, make_garage  # noqa: F401  (``garage`` is a fixture)
from tests.test_customer_cancel import _Together
from tests.test_field_service import make_practice, pbook
from tests.test_restaurant import book as rbook
from tests.test_restaurant import make_restaurant
from tests.test_salon import make_salon, sbook


async def add_part(s: Seed, name: str, qty: int, price: str = "50.00", **extra: Any) -> uuid.UUID:
    r = await s.client.post("/v1/admin/products", json={"name": name, "stock_qty": qty, "price": price, **extra}, headers=s.admin)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def job(g: Garage, hour: int = 9, mechanic: str | None = None) -> str:
    extra = {"resource_id": str(g.mechanics[mechanic])} if mechanic else {}
    r = await gbook(g, at(g.day, hour), ["oil"], **extra)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


def lines(parts: list[tuple[uuid.UUID, int]]) -> dict[str, Any]:
    return {"parts": [{"product_id": str(p), "quantity": q} for p, q in parts]}


async def put_parts(s: Seed, bid: str, parts: list[tuple[uuid.UUID, int]], headers: dict[str, str] | None = None) -> httpx.Response:
    return await s.client.put(f"/v1/admin/bookings/{bid}/parts", json=lines(parts), headers=headers if headers is not None else s.admin)


async def set_status(s: Seed, bid: str, new: str) -> httpx.Response:
    return await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": new}, headers=s.admin)


async def shelf(s: Seed, product_id: uuid.UUID) -> tuple[int, str]:
    listing = (await s.client.get("/v1/admin/products", headers=s.admin)).json()
    item = next(p for p in listing if p["id"] == str(product_id))
    return item["stock_qty"], item["stock_status"]


# --------------------------------------------------------------------- the list
async def test_parts_go_on_the_job_order_at_todays_price(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8, "120.50", estimated_lead_time_days=3)
    oil = await add_part(garage, "Oil 5W-30", 20, "40.00")
    bid = await job(garage)
    r = await put_parts(garage, bid, [(pads, 2), (oil, 4)])
    assert r.status_code == 200, r.text
    body = r.json()
    assert [(p["name"], p["quantity"], Decimal(p["unit_price"]), p["in_stock"], p["needs_ordering"]) for p in body["parts"]] == [
        ("Brake pads", 2, Decimal("120.50"), 8, False), ("Oil 5W-30", 4, Decimal("40.00"), 20, False)]
    assert body["needs_ordering"] is False and Decimal(body["total"]) == Decimal("401.00")  # 2 x 120.50 + 4 x 40.00
    assert body["parts"][0]["estimated_lead_time_days"] == 3 and body["parts"][1]["estimated_lead_time_days"] is None
    again = (await garage.client.get(f"/v1/admin/bookings/{bid}/parts", headers=garage.admin)).json()
    assert again == body
    detail = (await garage.client.get(f"/v1/admin/bookings/{bid}", headers=garage.admin)).json()
    assert sorted((p["name"], p["quantity"]) for p in detail["products"]) == [("Brake pads", 2), ("Oil 5W-30", 4)]  # the ticket shows them too
    assert await shelf(garage, pads) == (8, "in_stock")  # nothing leaves the shelf yet


async def test_the_list_sent_replaces_the_list_and_old_lines_keep_their_price(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8, "120.50")
    oil = await add_part(garage, "Oil 5W-30", 20, "40.00")
    filt = await add_part(garage, "Oil filter", 5, "15.00")
    bid = await job(garage)
    assert (await put_parts(garage, bid, [(pads, 2), (oil, 1)])).status_code == 200
    await garage.client.put(f"/v1/admin/products/{pads}", json={"name": "Brake pads", "stock_qty": 8, "price": "200.00"}, headers=garage.admin)
    r = await put_parts(garage, bid, [(pads, 3), (filt, 1)])  # pads: more of them; oil: gone; filter: new
    body = r.json()
    assert [(p["name"], p["quantity"], Decimal(p["unit_price"])) for p in body["parts"]] == [("Brake pads", 3, Decimal("120.50")), ("Oil filter", 1, Decimal("15.00"))]
    assert Decimal(body["total"]) == Decimal("376.50")  # the pads stay at the price they were added at
    cleared = await put_parts(garage, bid, [])
    assert cleared.status_code == 200 and cleared.json()["parts"] == [] and cleared.json()["needs_ordering"] is False and Decimal(cleared.json()["total"]) == 0
    async with tenant_session(garage.tenant_id) as db:
        assert (await db.scalars(select(BookingProduct))).all() == []


async def test_a_bad_list_changes_nothing(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8)
    oil = await add_part(garage, "Oil 5W-30", 20)
    bid = await job(garage)
    assert (await put_parts(garage, bid, [(pads, 2)])).status_code == 200
    assert (await put_parts(garage, bid, [(pads, 1), (pads, 1)])).status_code == 422  # one line per part: give the total
    assert (await put_parts(garage, bid, [(oil, 1), (uuid.uuid4(), 1)])).status_code == 404  # unknown part: the valid one is not saved either
    for bad in (0, -1, 1001, "2", 1.5, None):
        r = await garage.client.put(f"/v1/admin/bookings/{bid}/parts", json={"parts": [{"product_id": str(oil), "quantity": bad}]}, headers=garage.admin)
        assert r.status_code == 422, bad
    assert (await garage.client.put(f"/v1/admin/bookings/{bid}/parts", json={"parts": [], "extra": 1}, headers=garage.admin)).status_code == 422
    assert (await garage.client.put(f"/v1/admin/bookings/{bid}/parts", json={}, headers=garage.admin)).status_code == 422
    too_many = [(await add_part(garage, f"Bolt {i}", 1), 1) for i in range(51)]
    assert (await put_parts(garage, bid, too_many)).status_code == 422
    assert (await put_parts(garage, bid, too_many[:50])).status_code == 200  # fifty is the limit
    after = (await garage.client.get(f"/v1/admin/bookings/{bid}/parts", headers=garage.admin)).json()
    assert len(after["parts"]) == 50 and "Brake pads" not in [p["name"] for p in after["parts"]]


async def test_a_part_the_shelf_cannot_cover_needs_ordering(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 2, "120.00", stock_status="available_on_order", estimated_lead_time_days=5)
    gone = await add_part(garage, "Rotor", 0, "300.00", stock_status="out_of_stock")
    enough = await add_part(garage, "Oil 5W-30", 4, "40.00")
    bid = await job(garage)
    body = (await put_parts(garage, bid, [(pads, 2), (gone, 1), (enough, 4)])).json()
    flags = {p["name"]: (p["needs_ordering"], p["in_stock"]) for p in body["parts"]}
    assert flags == {"Brake pads": (False, 2), "Oil 5W-30": (False, 4), "Rotor": (True, 0)}  # the shelf decides, not the label
    assert body["needs_ordering"] is True
    body = (await put_parts(garage, bid, [(pads, 3), (enough, 4)])).json()
    assert {p["name"]: p["needs_ordering"] for p in body["parts"]} == {"Brake pads": True, "Oil 5W-30": False} and body["needs_ordering"] is True
    assert (await shelf(garage, pads)) == (2, "available_on_order")
    await garage.client.put(f"/v1/admin/products/{pads}", json={"name": "Brake pads", "stock_qty": 6, "price": "120.00", "stock_status": "in_stock"}, headers=garage.admin)
    assert (await garage.client.get(f"/v1/admin/bookings/{bid}/parts", headers=garage.admin)).json()["needs_ordering"] is False  # the parts arrived


# --------------------------------------------------------------------- when it can be changed, and by whom
@pytest.mark.parametrize("status,allowed", [("confirmed", True), ("in-progress", True), ("waiting-for-parts", True), ("pending", False),
                                            ("completed", False), ("cancelled", False), ("rejected", False), ("no-show", False)])
async def test_parts_can_be_changed_while_the_job_is_accepted_and_unfinished(garage: Garage, status: str, allowed: bool) -> None:
    pads = await add_part(garage, "Brake pads", 8)
    bid = await job(garage)
    async with tenant_session(garage.tenant_id) as db:
        await db.execute(text("UPDATE bookings SET status = :s WHERE id = :i"), {"s": status, "i": uuid.UUID(bid)})
    r = await put_parts(garage, bid, [(pads, 1)])
    assert (r.status_code == 200) is allowed, (status, r.text)
    if not allowed:
        assert r.status_code == 409 and status in r.json()["detail"]
    assert (await garage.client.get(f"/v1/admin/bookings/{bid}/parts", headers=garage.admin)).status_code == 200  # reading is always fine


async def test_mechanics_work_on_their_own_jobs_only(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8)
    ali_job = await job(garage, 9, "Ali")
    ali = await garage.token("staff", resource_id=garage.mechanics["Ali"])
    bala = await garage.token("staff", resource_id=garage.mechanics["Bala"])
    assert (await put_parts(garage, ali_job, [(pads, 1)], headers=bala)).status_code == 403
    assert (await garage.client.get(f"/v1/admin/bookings/{ali_job}/parts", headers=bala)).status_code == 403
    assert (await put_parts(garage, ali_job, [(pads, 1)], headers=ali)).status_code == 200
    assert (await garage.client.get(f"/v1/admin/bookings/{ali_job}/parts", headers=ali)).json()["parts"][0]["quantity"] == 1
    for role in ("manager", "owner"):
        assert (await put_parts(garage, ali_job, [(pads, 2)], headers=await garage.token(role))).status_code == 200
    assert (await put_parts(garage, ali_job, [(pads, 3)], headers={})).status_code == 401
    assert (await garage.client.get(f"/v1/admin/bookings/{ali_job}/parts")).status_code == 401
    assert (await put_parts(garage, ali_job, [(pads, 3)], headers=garage.pk)).status_code == 401
    assert (await garage.client.get(f"/v1/admin/bookings/{ali_job}/parts", headers=garage.admin)).json()["parts"][0]["quantity"] == 2


async def test_another_businesss_job_and_parts_do_not_exist(client: httpx.AsyncClient) -> None:
    mine, other = await make_garage(client), await make_garage(client)
    theirs = await job(other)
    their_part = await add_part(other, "Secret part", 5)
    my_part = await add_part(mine, "Brake pads", 5)
    mine_job = await job(mine)
    assert (await put_parts(mine, theirs, [(my_part, 1)])).status_code == 404
    assert (await mine.client.get(f"/v1/admin/bookings/{theirs}/parts", headers=mine.admin)).status_code == 404
    assert (await put_parts(mine, mine_job, [(their_part, 1)])).status_code == 404
    assert (await put_parts(mine, str(uuid.uuid4()), [(my_part, 1)])).status_code == 404


async def test_the_change_is_in_the_audit_log(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8)
    bid = await job(garage)
    assert (await put_parts(garage, bid, [(pads, 1)])).status_code == 200
    entries = (await garage.client.get("/v1/admin/audit-log", params={"action": "parts"}, headers=garage.admin)).json()
    assert [(e["action"], e["target_id"], e["detail"]) for e in entries] == [("PUT /v1/admin/bookings/{booking_id}/parts", bid, {})]  # no body, so no part list


# --------------------------------------------------------------------- the kinds of business
async def test_only_workshops_and_professionals_have_a_parts_list(client: httpx.AsyncClient) -> None:
    salon = await make_salon(client)
    s_bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Lin", customer={"name": "Aisha", "phone": "+60123450001"})).json()["id"]
    rest = await make_restaurant(client)
    r_bid = (await rbook(rest, at(rest.day, 19), 2, customer={"name": "Aisha", "phone": "+60123450002"})).json()["id"]
    for s, bid in ((salon, s_bid), (rest, r_bid)):
        pid = await add_part(s, "Thing", 3)
        r = await put_parts(s, bid, [(pid, 1)])
        assert r.status_code == 422 and "automotive and professional" in r.json()["detail"]
        assert (await s.client.get(f"/v1/admin/bookings/{bid}/parts", headers=s.admin)).status_code == 422


async def test_a_spa_has_no_parts_list(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    bid = (await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 10), mei))).json()["id"]
    pid = await add_part(seeded, "Oil", 3)
    assert (await put_parts(seeded, bid, [(pid, 1)])).status_code == 422


async def test_professionals_keep_spare_parts_too(client: httpx.AsyncClient) -> None:
    practice = await make_practice(client)
    bid = (await pbook(practice, at(practice.day, 10), "office", customer={"name": "Aisha", "phone": "+60123450001"})).json()["id"]
    pipe = await add_part(practice, "Copper pipe", 10, "8.00")
    r = await put_parts(practice, bid, [(pipe, 3)])
    assert r.status_code == 200 and Decimal(r.json()["total"]) == Decimal("24.00")
    assert (await set_status(practice, bid, "in-progress")).status_code == 200
    minutes = await practice.client.patch(f"/v1/admin/bookings/{bid}/actual-duration", json={"minutes": 95}, headers=practice.admin)
    assert minutes.status_code == 200, minutes.text
    assert (await set_status(practice, bid, "completed")).status_code == 200
    assert await shelf(practice, pipe) == (7, "in_stock")
    done = (await practice.client.get(f"/v1/admin/bookings/{bid}", headers=practice.admin)).json()
    assert done["actual_duration_minutes"] == 95  # what the professional reported is not replaced by a computed figure


# --------------------------------------------------------------------- stock leaves the shelf when the job is completed
async def test_completing_the_job_takes_the_parts_off_the_shelf(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8)
    last = await add_part(garage, "Rotor", 2, stock_status="in_stock")
    bid = await job(garage)
    assert (await put_parts(garage, bid, [(pads, 3), (last, 2)])).status_code == 200
    assert (await set_status(garage, bid, "in-progress")).status_code == 200
    assert await shelf(garage, pads) == (8, "in_stock")  # still nothing while the work goes on
    assert (await set_status(garage, bid, "completed")).status_code == 200
    assert await shelf(garage, pads) == (5, "in_stock")
    assert await shelf(garage, last) == (0, "out_of_stock")  # the last ones went: the catalogue shows it
    catalog = {p["name"]: p["stock_status"] for p in (await garage.client.get("/v1/public/products", headers=garage.pk)).json()}
    assert catalog["Rotor"] == "out_of_stock" and catalog["Brake pads"] == "in_stock"
    done = (await garage.client.get(f"/v1/admin/bookings/{bid}", headers=garage.admin)).json()
    assert done["actual_duration_minutes"] is None  # a workshop job's time is not worked out from its services
    assert (await garage.client.get(f"/v1/admin/bookings/{bid}/parts", headers=garage.admin)).json()["parts"][0]["quantity"] == 3  # the record stays


async def test_a_job_that_never_completes_takes_nothing(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8)
    for end in ("cancelled", "no-show"):
        bid = await job(garage, 9 if end == "cancelled" else 11)
        assert (await put_parts(garage, bid, [(pads, 2)])).status_code == 200
        if end == "no-show":
            assert (await set_status(garage, bid, "in-progress")).status_code == 200  # (a no-show comes from confirmed; use the database)
            async with tenant_session(garage.tenant_id) as db:
                await db.execute(text("UPDATE bookings SET status = 'no-show' WHERE id = :i"), {"i": uuid.UUID(bid)})
        else:
            assert (await set_status(garage, bid, "cancelled")).status_code == 200
        assert await shelf(garage, pads) == (8, "in_stock")


async def test_stock_never_goes_negative_and_the_on_order_label_stays(garage: Garage) -> None:
    ordered = await add_part(garage, "Clutch kit", 1, stock_status="available_on_order", estimated_lead_time_days=4)
    plain = await add_part(garage, "Rotor", 1)
    bid = await job(garage)
    assert (await put_parts(garage, bid, [(ordered, 3), (plain, 3)])).json()["needs_ordering"] is True
    assert (await set_status(garage, bid, "in-progress")).status_code == 200
    assert (await set_status(garage, bid, "completed")).status_code == 200  # completing is not blocked by a stale stock count
    assert await shelf(garage, ordered) == (0, "available_on_order")  # clamped at zero; still orderable
    assert await shelf(garage, plain) == (0, "out_of_stock")


async def test_the_parts_waiting_job_flow_end_to_end(garage: Garage) -> None:
    rotor = await add_part(garage, "Rotor", 0, "300.00", stock_status="available_on_order", estimated_lead_time_days=3)
    bid = await job(garage)
    assert (await set_status(garage, bid, "in-progress")).status_code == 200
    r = await put_parts(garage, bid, [(rotor, 2)])
    assert r.json()["needs_ordering"] is True and Decimal(r.json()["total"]) == Decimal("600.00")
    assert (await set_status(garage, bid, "waiting-for-parts")).status_code == 200
    await garage.client.put(f"/v1/admin/products/{rotor}", json={"name": "Rotor", "stock_qty": 4, "price": "310.00", "stock_status": "in_stock"}, headers=garage.admin)
    again = (await garage.client.get(f"/v1/admin/bookings/{bid}/parts", headers=garage.admin)).json()
    assert again["needs_ordering"] is False and Decimal(again["parts"][0]["unit_price"]) == Decimal("300.00") and again["parts"][0]["in_stock"] == 4
    resume = await garage.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": at(garage.day + dt.timedelta(days=1), 10).isoformat()}, headers=garage.admin)
    assert resume.status_code == 200, resume.text
    assert (await set_status(garage, bid, "in-progress")).status_code == 200
    assert (await set_status(garage, bid, "completed")).status_code == 200
    assert await shelf(garage, rotor) == (2, "in_stock")  # taken off once, at the end


# --------------------------------------------------------------------- races
async def test_two_people_editing_one_job_apply_one_after_the_other(garage: Garage, monkeypatch: pytest.MonkeyPatch) -> None:
    a = await add_part(garage, "Part A", 9)
    b = await add_part(garage, "Part B", 9)
    c = await add_part(garage, "Part C", 9)
    bid = await job(garage)
    with monkeypatch.context() as m:
        m.setattr(parts_module, "set_parts", _Together(2).wrap(parts_module.set_parts))
        r1, r2 = await asyncio.gather(put_parts(garage, bid, [(a, 1), (b, 2)]), put_parts(garage, bid, [(c, 3)]))
    assert r1.status_code == 200 and r2.status_code == 200, (r1.text, r2.text)
    final = {p["name"]: p["quantity"] for p in (await garage.client.get(f"/v1/admin/bookings/{bid}/parts", headers=garage.admin)).json()["parts"]}
    assert final in ({"Part A": 1, "Part B": 2}, {"Part C": 3})  # exactly one of the two lists, never a mixture


async def test_completing_a_job_racing_a_parts_edit_never_loses_a_part(garage: Garage, monkeypatch: pytest.MonkeyPatch) -> None:
    pads = await add_part(garage, "Brake pads", 50)
    for round_ in range(3):
        bid = await job(garage, 9 + round_)
        assert (await set_status(garage, bid, "in-progress")).status_code == 200
        before = (await shelf(garage, pads))[0]
        with monkeypatch.context() as m:
            together = _Together(2)
            m.setattr(parts_module, "set_parts", together.wrap(parts_module.set_parts))
            m.setattr(booking_module, "transition_booking", together.wrap(booking_module.transition_booking))
            edited, completed = await asyncio.gather(put_parts(garage, bid, [(pads, 4)]), set_status(garage, bid, "completed"))
        assert completed.status_code == 200, completed.text
        after = (await shelf(garage, pads))[0]
        if edited.status_code == 200:
            assert after == before - 4  # the edit came first, so the completion took the part off the shelf
        else:
            assert edited.status_code == 409 and after == before  # the job was already finished: nothing was added, nothing taken
        async with tenant_session(garage.tenant_id) as db:
            on_ticket = [line.quantity for line in (await db.scalars(select(BookingProduct).where(BookingProduct.booking_id == uuid.UUID(bid)))).all()]
        assert on_ticket == ([4] if edited.status_code == 200 else [])


async def test_each_job_has_its_own_list(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8)
    oil = await add_part(garage, "Oil 5W-30", 20)
    first, second = await job(garage, 9), await job(garage, 11)
    assert (await put_parts(garage, first, [(pads, 2)])).status_code == 200
    assert (await put_parts(garage, second, [(oil, 5)])).status_code == 200
    one = (await garage.client.get(f"/v1/admin/bookings/{first}/parts", headers=garage.admin)).json()
    two = (await garage.client.get(f"/v1/admin/bookings/{second}/parts", headers=garage.admin)).json()
    assert [(p["name"], p["quantity"]) for p in one["parts"]] == [("Brake pads", 2)]
    assert [(p["name"], p["quantity"]) for p in two["parts"]] == [("Oil 5W-30", 5)]
    assert (await put_parts(garage, first, [])).status_code == 200  # clearing one job leaves the other alone
    assert len((await garage.client.get(f"/v1/admin/bookings/{second}/parts", headers=garage.admin)).json()["parts"]) == 1


async def test_only_staff_are_held_to_their_own_jobs(garage: Garage) -> None:
    pads = await add_part(garage, "Brake pads", 8)
    bala_job = await job(garage, 9, "Bala")
    managers_with_a_resource = await garage.token("manager", resource_id=garage.mechanics["Ali"])  # a manager who is also a mechanic
    assert (await put_parts(garage, bala_job, [(pads, 1)], headers=managers_with_a_resource)).status_code == 200
    assert (await garage.client.get(f"/v1/admin/bookings/{bala_job}/parts", headers=managers_with_a_resource)).status_code == 200


async def test_the_fifty_line_limit_holds_at_every_layer(garage: Garage) -> None:
    from pydantic import ValidationError

    from app.core.exceptions import ValidationFailed
    from app.models import Tenant
    from app.schemas.scheduling import PartsIn

    lines_ = [{"product_id": str(uuid.uuid4()), "quantity": 1} for _ in range(51)]
    with pytest.raises(ValidationError):
        PartsIn.model_validate({"parts": lines_})
    assert len(PartsIn.model_validate({"parts": lines_[:50]}).parts) == 50
    bid = await job(garage)
    async with tenant_session(garage.tenant_id) as db:
        tenant = await db.get(Tenant, garage.tenant_id)
        assert tenant is not None
        with pytest.raises(ValidationFailed, match="At most 50"):
            await parts_module.set_parts(db, tenant, uuid.UUID(bid), [(uuid.uuid4(), 1) for _ in range(51)])  # not only the API guards it
