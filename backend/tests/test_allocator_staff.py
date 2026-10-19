"""Staff assignment, "Any Available" routing, leave and break tests."""
from __future__ import annotations

import uuid

from app.services.allocators.staff import pick_least_loaded
from tests.conftest import Seed, at


# --------------------------------------------------------------------- pure logic
def test_least_loaded_wins() -> None:
    a, b, c = (uuid.uuid4() for _ in range(3))
    assert pick_least_loaded([a, b, c], {a: 120.0, b: 30.0, c: 60.0}) == b


def test_unloaded_staff_counts_as_zero_and_ties_are_deterministic() -> None:
    a, b = sorted([uuid.uuid4(), uuid.uuid4()], key=str)
    assert pick_least_loaded([b, a], {}) == a
    assert pick_least_loaded([a, b], {a: 60.0}) == b


# --------------------------------------------------------------------- end to end (needs PostgreSQL)
async def test_any_available_routes_to_least_loaded(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    lin = await seeded.add_staff("Lin")
    # Give Mei a morning booking so Lin is the less loaded of the two.
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 9), mei), headers=seeded.pk)
    assert r.status_code == 201, r.text
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 15)), headers=seeded.pk)
    assert r.status_code == 201, r.text
    assert r.json()["resource_ids"] == [str(lin)]


async def test_any_available_falls_back_to_the_free_one(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    lin = await seeded.add_staff("Lin")
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert r.status_code == 201
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10)), headers=seeded.pk)
    assert r.status_code == 201 and r.json()["resource_ids"] == [str(lin)]


async def test_specific_staff_cannot_be_double_booked_even_if_others_are_free(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    await seeded.add_staff("Lin")
    ok = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert ok.status_code == 201
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei), headers=seeded.pk)
    assert r.status_code == 409


async def test_break_time_cannot_be_booked(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 13), mei), headers=seeded.pk)
    assert r.status_code == 409 and r.json()["error"] == "slot_unavailable"
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 12, 30), mei), headers=seeded.pk)
    assert r.status_code == 409  # 12:30-13:30 runs into the break


async def test_staff_on_leave_is_excluded(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    lin = await seeded.add_staff("Lin")
    r = await seeded.client.post(
        "/v1/admin/days-off", headers=seeded.admin,
        json={"resource_id": str(mei), "name": "Leave", "start_date": seeded.day.isoformat(), "end_date": seeded.day.isoformat()},
    )
    assert r.status_code == 201
    r = await seeded.client.get(
        "/v1/public/availability", params={"date": seeded.day.isoformat(), "service_ids": str(seeded.service_id)}, headers=seeded.pk
    )
    assert all(s["resource_ids"] == [str(lin)] for s in r.json()["slots"]) and r.json()["slots"]


async def test_service_qualification_restricts_staff(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    await seeded.add_staff("Lin")
    r = await seeded.client.patch(
        f"/v1/admin/services/{seeded.service_id}", json={"resource_ids": [str(mei)]}, headers=seeded.admin
    )
    assert r.status_code == 200, r.text
    r = await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 11)), headers=seeded.pk)
    assert r.status_code == 201 and r.json()["resource_ids"] == [str(mei)]
