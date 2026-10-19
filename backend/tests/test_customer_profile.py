"""A customer's saved addresses and vehicles (/public/me/addresses, /public/me/vehicles)."""
from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from tests.conftest import Seed, at
from tests.test_automotive import Garage, gbody, gbook, make_garage
from tests.test_customer_cancel import PHONE, book, login, otp  # noqa: F401
from tests.test_field_service import NEAR, make_practice, pbody

HOME = {"line1": "12 Jalan Bukit", "city": "Kuala Lumpur", "postcode": "50450", "latitude": 3.12, "longitude": 101.67}


async def sql(s: Seed, statement: str, **params: Any) -> list[Any]:
    async with tenant_session(s.tenant_id) as db:
        result = await db.execute(text(statement), params)
        return list(result.all()) if result.returns_rows else []


# --------------------------------------------------------------------- addresses
async def test_addresses_can_be_saved_listed_changed_and_removed(seeded: Seed, otp: Any) -> None:
    me = await login(seeded)
    created = await seeded.client.post("/v1/public/me/addresses", json=HOME, headers=me)
    assert created.status_code == 201, created.text
    aid = created.json()["id"]
    assert created.json() == {"id": aid, **HOME}
    work = await seeded.client.post("/v1/public/me/addresses", json={**HOME, "line1": "Office"}, headers=me)
    listing = (await seeded.client.get("/v1/public/me/addresses", headers=me)).json()
    assert [a["id"] for a in listing] == [aid, work.json()["id"]]  # oldest first
    patched = await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"line1": "14 Jalan Bukit", "postcode": None}, headers=me)
    assert patched.status_code == 200
    assert patched.json() == {"id": aid, **HOME, "line1": "14 Jalan Bukit", "postcode": None}  # only what was sent changed
    assert (await seeded.client.delete(f"/v1/public/me/addresses/{aid}", headers=me)).status_code == 204
    assert [a["id"] for a in (await seeded.client.get("/v1/public/me/addresses", headers=me)).json()] == [work.json()["id"]]
    assert await sql(seeded, "SELECT 1 FROM customer_addresses WHERE id = :i", i=uuid.UUID(aid)) == []  # really gone
    assert (await seeded.client.delete(f"/v1/public/me/addresses/{aid}", headers=me)).status_code == 404


async def test_addresses_are_private_to_their_owner(seeded: Seed, otp: Any) -> None:
    mine = await login(seeded)
    aid = (await seeded.client.post("/v1/public/me/addresses", json=HOME, headers=mine)).json()["id"]
    other = await login(seeded, "+60199990000")
    assert (await seeded.client.get("/v1/public/me/addresses", headers=other)).json() == []
    assert (await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"line1": "x"}, headers=other)).status_code == 404
    assert (await seeded.client.delete(f"/v1/public/me/addresses/{aid}", headers=other)).status_code == 404
    assert (await seeded.client.get("/v1/public/me/addresses", headers=seeded.pk)).status_code == 401  # a site key alone is no login
    assert (await seeded.client.get("/v1/public/me/addresses", headers={**seeded.pk, **seeded.admin})).status_code == 403  # nor is a staff token
    assert (await seeded.client.get("/v1/public/me/addresses", headers=mine)).json()[0]["line1"] == HOME["line1"]


async def test_addresses_are_private_to_their_business(client: httpx.AsyncClient, otp: Any) -> None:
    a, b = await make_seed_pair(client)
    await client.post("/v1/public/me/addresses", json=HOME, headers=await login(a))
    assert (await client.get("/v1/public/me/addresses", headers=await login(b))).json() == []  # the same phone, another business


async def make_seed_pair(client: httpx.AsyncClient) -> tuple[Seed, Seed]:
    from tests.conftest import make_seed

    return await make_seed(client), await make_seed(client)


async def test_address_input_is_validated(seeded: Seed, otp: Any) -> None:
    me = await login(seeded)
    for bad in ({**HOME, "latitude": 91}, {**HOME, "longitude": -181}, {**HOME, "line1": ""}, {k: v for k, v in HOME.items() if k != "latitude"}):
        assert (await seeded.client.post("/v1/public/me/addresses", json=bad, headers=me)).status_code == 422, bad
    aid = (await seeded.client.post("/v1/public/me/addresses", json=HOME, headers=me)).json()["id"]
    for bad in ({"line1": None}, {"line1": ""}, {"latitude": None}, {"longitude": 200}, {"latitude": -95}):
        assert (await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json=bad, headers=me)).status_code == 422, bad
    assert (await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={}, headers=me)).json()["line1"] == HOME["line1"]


async def test_the_number_of_saved_addresses_is_capped(seeded: Seed, otp: Any) -> None:
    from app.services.customer_profile import MAX_SAVED

    me = await login(seeded)
    for n in range(MAX_SAVED):
        assert (await seeded.client.post("/v1/public/me/addresses", json={**HOME, "line1": f"No {n}"}, headers=me)).status_code == 201
    over = await seeded.client.post("/v1/public/me/addresses", json=HOME, headers=me)
    assert over.status_code == 409
    other = await login(seeded, "+60199990000")
    assert (await seeded.client.post("/v1/public/me/addresses", json=HOME, headers=other)).status_code == 201  # the cap is per customer


async def test_an_address_in_use_by_an_open_booking_cannot_be_removed(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, at(seeded.day, 10))
    me = await login(seeded)
    aid = (await seeded.client.post("/v1/public/me/addresses", json=HOME, headers=me)).json()["id"]
    for status in ("pending", "confirmed", "in-progress", "waiting-for-parts"):
        await sql(seeded, "UPDATE bookings SET address_id = :a, status = :st WHERE id = :b", a=uuid.UUID(aid), st=status, b=uuid.UUID(bid))
        r = await seeded.client.delete(f"/v1/public/me/addresses/{aid}", headers=me)
        assert r.status_code == 409 and r.json()["open_bookings"] == 1, (status, r.text)
    assert len(await sql(seeded, "SELECT 1 FROM customer_addresses WHERE id = :i", i=uuid.UUID(aid))) == 1


async def test_removing_an_address_only_clears_it_from_finished_bookings(seeded: Seed, otp: Any) -> None:
    mei = await seeded.add_staff("Mei")
    done, cancelled = await book(seeded, mei, at(seeded.day, 10)), await book(seeded, mei, at(seeded.day, 12))
    me = await login(seeded)
    aid = uuid.UUID((await seeded.client.post("/v1/public/me/addresses", json=HOME, headers=me)).json()["id"])
    await sql(seeded, "UPDATE bookings SET address_id = :a, status = 'completed' WHERE id = :b", a=aid, b=uuid.UUID(done))
    await sql(seeded, "UPDATE bookings SET address_id = :a, status = 'cancelled' WHERE id = :b", a=aid, b=uuid.UUID(cancelled))
    keep = uuid.UUID((await seeded.client.post("/v1/public/me/addresses", json={**HOME, "line1": "Other"}, headers=me)).json()["id"])
    untouched = await book(seeded, mei, at(seeded.day, 15))
    await sql(seeded, "UPDATE bookings SET address_id = :a WHERE id = :b", a=keep, b=uuid.UUID(untouched))
    assert (await seeded.client.delete(f"/v1/public/me/addresses/{aid}", headers=me)).status_code == 204
    assert (await sql(seeded, "SELECT address_id FROM bookings WHERE id = :b", b=uuid.UUID(untouched)))[0][0] == keep  # other addresses are left alone
    rows = await sql(seeded, "SELECT status, address_id FROM bookings WHERE id IN (:a, :b) ORDER BY status", a=uuid.UUID(done), b=uuid.UUID(cancelled))
    assert [(r[0], r[1]) for r in rows] == [("cancelled", None), ("completed", None)]  # the bookings stay; only the link goes


@pytest.mark.usefixtures("postgis")
async def test_a_saved_address_can_be_picked_when_booking_an_on_site_visit(client: httpx.AsyncClient, otp: Any) -> None:
    practice = await make_practice(client)
    me = await login(practice)
    saved = await client.post("/v1/public/me/addresses", json={"line1": "Near", "latitude": NEAR[0], "longitude": NEAR[1]}, headers=me)
    body = pbody(practice, at(practice.day, 10), "site", address_id=saved.json()["id"], customer={"name": "Aisha", "phone": PHONE})
    r = await client.post("/v1/public/bookings", json=body, headers=practice.pk)
    assert r.status_code == 201, r.text
    assert (await client.delete(f"/v1/public/me/addresses/{saved.json()['id']}", headers=me)).status_code == 409  # now in use
    elsewhere = await login(practice, "+60199990000")
    body["customer"] = {"name": "Else", "phone": "+60199990000"}
    assert (await client.post("/v1/public/bookings", json=body, headers=practice.pk)).status_code == 404  # someone else's saved address
    assert elsewhere


# --------------------------------------------------------------------- vehicles
@pytest.fixture
async def garage(client: httpx.AsyncClient) -> Garage:
    return await make_garage(client)


def car(g: Garage, plate: str = "abc 123", model: str = "Vios", **extra: Any) -> dict[str, Any]:
    return {"plate_no": plate, "supported_vehicle_id": str(g.cars[model]), **extra}


async def test_vehicles_can_be_saved_listed_changed_and_removed(garage: Garage, otp: Any) -> None:
    me = await login(garage)
    created = await garage.client.post("/v1/public/me/vehicles", json=car(garage, year=2019), headers=me)
    assert created.status_code == 201, created.text
    vid = created.json()["id"]
    assert created.json() == {"id": vid, "plate_no": "ABC123", "make": "Toyota", "model": "Vios", "year": 2019,
                              "supported_vehicle_id": str(garage.cars["Vios"]), "serviceable": True}  # the plate is stored normalised
    listing = (await garage.client.get("/v1/public/me/vehicles", headers=me)).json()
    assert [v["id"] for v in listing] == [vid]
    patched = await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"plate_no": "xyz-9", "year": 2021}, headers=me)
    assert patched.status_code == 200 and patched.json()["plate_no"] == "XYZ9" and patched.json()["year"] == 2021
    assert patched.json()["model"] == "Vios"
    switched = await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"supported_vehicle_id": str(garage.cars["City"])}, headers=me)
    assert switched.status_code == 200 and (switched.json()["make"], switched.json()["model"]) == ("Honda", "City")
    assert (await garage.client.delete(f"/v1/public/me/vehicles/{vid}", headers=me)).status_code == 204
    assert (await garage.client.get("/v1/public/me/vehicles", headers=me)).json() == []
    assert (await garage.client.delete(f"/v1/public/me/vehicles/{vid}", headers=me)).status_code == 404


async def test_a_vehicle_must_be_one_the_workshop_services(garage: Garage, otp: Any) -> None:
    me = await login(garage)
    unknown = await garage.client.post("/v1/public/me/vehicles", json={"plate_no": "A1", "supported_vehicle_id": str(uuid.uuid4())}, headers=me)
    assert unknown.status_code == 422 and unknown.json()["error"] == "incompatible_vehicle"
    for year in (2010, 2023):  # the Honda City is serviced 2015-2022 only
        r = await garage.client.post("/v1/public/me/vehicles", json=car(garage, "C1", "City", year=year), headers=me)
        assert r.status_code == 422 and r.json()["error"] == "incompatible_vehicle", year
    assert (await garage.client.post("/v1/public/me/vehicles", json=car(garage, "C1", "City"), headers=me)).status_code == 422  # year unknown
    ok = await garage.client.post("/v1/public/me/vehicles", json=car(garage, "C1", "City", year=2018), headers=me)
    assert ok.status_code == 201
    vid = ok.json()["id"]
    assert (await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"year": 2030}, headers=me)).status_code == 422  # an edit must stay serviceable
    assert (await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"supported_vehicle_id": str(uuid.uuid4())}, headers=me)).status_code == 422
    assert (await garage.client.get("/v1/public/me/vehicles", headers=me)).json()[0]["year"] == 2018
    for bad in ({"plate_no": None}, {"supported_vehicle_id": None}, {"plate_no": ""}, {"year": 1800}):
        assert (await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json=bad, headers=me)).status_code == 422, bad
    for bad_plate in ("", "---"):
        r = await garage.client.post("/v1/public/me/vehicles", json=car(garage, bad_plate), headers=me)
        assert r.status_code == 422, bad_plate


async def test_the_same_plate_cannot_be_saved_twice_however_it_is_typed(garage: Garage, otp: Any) -> None:
    me = await login(garage)
    assert (await garage.client.post("/v1/public/me/vehicles", json=car(garage, "abc 123"), headers=me)).status_code == 201
    assert (await garage.client.post("/v1/public/me/vehicles", json=car(garage, "ABC-123"), headers=me)).status_code == 409
    second = await garage.client.post("/v1/public/me/vehicles", json=car(garage, "DEF 456"), headers=me)
    assert second.status_code == 201
    clash = await garage.client.patch(f"/v1/public/me/vehicles/{second.json()['id']}", json={"plate_no": "abc123"}, headers=me)
    assert clash.status_code == 409
    other = await login(garage, "+60199990000")
    assert (await garage.client.post("/v1/public/me/vehicles", json=car(garage, "ABC 123"), headers=other)).status_code == 201  # another owner may have it
    assert len((await garage.client.get("/v1/public/me/vehicles", headers=me)).json()) == 2  # the failed attempts left nothing behind


async def test_vehicles_are_private_to_their_owner(garage: Garage, otp: Any) -> None:
    mine = await login(garage)
    vid = (await garage.client.post("/v1/public/me/vehicles", json=car(garage), headers=mine)).json()["id"]
    other = await login(garage, "+60199990000")
    assert (await garage.client.get("/v1/public/me/vehicles", headers=other)).json() == []
    assert (await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"year": 2020}, headers=other)).status_code == 404
    assert (await garage.client.delete(f"/v1/public/me/vehicles/{vid}", headers=other)).status_code == 404
    assert (await garage.client.get("/v1/public/me/vehicles", headers=garage.pk)).status_code == 401
    assert len((await garage.client.get("/v1/public/me/vehicles", headers=mine)).json()) == 1


async def test_only_workshops_keep_vehicles(client: httpx.AsyncClient, seeded: Seed, otp: Any) -> None:
    me = await login(seeded)
    body = {"plate_no": "A1", "supported_vehicle_id": str(uuid.uuid4())}
    for call in (client.get("/v1/public/me/vehicles", headers=me), client.post("/v1/public/me/vehicles", json=body, headers=me)):
        r = await call
        assert r.status_code == 422 and "automotive" in r.json()["detail"]
    for call in (client.patch(f"/v1/public/me/vehicles/{uuid.uuid4()}", json={"year": 2020}, headers=me), client.delete(f"/v1/public/me/vehicles/{uuid.uuid4()}", headers=me)):
        assert (await call).status_code == 422


async def test_a_saved_vehicle_can_be_picked_when_booking_and_then_it_is_in_use(garage: Garage, otp: Any) -> None:
    me = await login(garage)
    vid = (await garage.client.post("/v1/public/me/vehicles", json=car(garage), headers=me)).json()["id"]
    body = gbody(garage, at(garage.day, 10), ["oil"], customer={"name": "Aisha", "phone": PHONE})
    del body["vehicle"]
    body["vehicle_id"] = vid
    r = await garage.client.post("/v1/public/bookings", json=body, headers=garage.pk)
    assert r.status_code == 201, r.text
    refused = await garage.client.delete(f"/v1/public/me/vehicles/{vid}", headers=me)
    assert refused.status_code == 409 and refused.json()["open_bookings"] == 1
    cancelled = await garage.client.post(f"/v1/public/bookings/{r.json()['id']}/cancel", headers=me)
    assert cancelled.status_code == 200
    assert (await garage.client.delete(f"/v1/public/me/vehicles/{vid}", headers=me)).status_code == 204  # the job order no longer needs it
    assert (await sql(garage, "SELECT vehicle_id FROM bookings WHERE id = :i", i=uuid.UUID(r.json()["id"])))[0][0] is None


async def test_vehicles_registered_while_booking_show_up_in_the_list(garage: Garage, otp: Any) -> None:
    r = await gbook(garage, at(garage.day, 10), ["oil"], customer={"name": "Aisha", "phone": PHONE}, vehicle=car(garage, "w 1234"))
    assert r.status_code == 201
    listing = (await garage.client.get("/v1/public/me/vehicles", headers=await login(garage))).json()
    assert [v["plate_no"] for v in listing] == ["W1234"]


async def test_a_car_whose_model_left_the_supported_list_is_marked_not_serviceable(garage: Garage, otp: Any) -> None:
    me = await login(garage)
    vios = (await garage.client.post("/v1/public/me/vehicles", json=car(garage, "V1"), headers=me)).json()["id"]
    city = (await garage.client.post("/v1/public/me/vehicles", json=car(garage, "C1", "City", year=2018), headers=me)).json()["id"]
    assert all(v["serviceable"] for v in (await garage.client.get("/v1/public/me/vehicles", headers=me)).json())
    assert (await garage.client.delete(f"/v1/admin/supported-vehicles/{garage.cars['Vios']}", headers=garage.admin)).status_code == 204
    await sql(garage, "UPDATE supported_vehicles SET year_to = 2017 WHERE id = :i", i=garage.cars["City"])  # and the City is now serviced to 2017 only
    by_id = {v["id"]: v for v in (await garage.client.get("/v1/public/me/vehicles", headers=me)).json()}
    assert by_id[vios]["serviceable"] is False and by_id[vios]["supported_vehicle_id"] is None
    assert by_id[city]["serviceable"] is False
    blocked = await garage.client.patch(f"/v1/public/me/vehicles/{vios}", json={"year": 2020}, headers=me)
    assert blocked.status_code == 422  # it has to be given a model the workshop services before it can be edited


async def test_the_number_of_saved_vehicles_is_capped(garage: Garage, otp: Any) -> None:
    from app.services.customer_profile import MAX_SAVED

    me = await login(garage)
    for n in range(MAX_SAVED):
        assert (await garage.client.post("/v1/public/me/vehicles", json=car(garage, f"P{n}"), headers=me)).status_code == 201
    assert (await garage.client.post("/v1/public/me/vehicles", json=car(garage, "ONE-MORE"), headers=me)).status_code == 409
