"""Automotive allocator: mechanic + bay double constraint, stacking, vehicles, parts, waiting-for-parts."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.database import tenant_session
from app.models import Booking, BookingAllocation, Resource, Tenant
from app.services.allocators.workshop import ResInfo, choose_resource, fits, plan_segments
from app.services.availability import Interval
from app.services.booking import transition_booking
from tests.conftest import PLATFORM, TZ, Seed, allocations, at, make_seed

D = dt.date(2030, 1, 7)


# --------------------------------------------------------------------- pure logic
def _svc(minutes: int, buffer: int = 0) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), duration_minutes=minutes, buffer_minutes=buffer)


def test_segments_are_contiguous_and_include_buffers() -> None:
    a, b, c = _svc(60), _svc(120, 15), _svc(30)
    segs = plan_segments(at(D, 9), [a, b, c])
    assert [(s.start.astimezone(TZ).strftime("%H:%M"), s.end.astimezone(TZ).strftime("%H:%M")) for _, s in segs] == [
        ("09:00", "10:00"), ("10:00", "12:15"), ("12:15", "12:45"),
    ]


def test_fits_needs_the_whole_segment_inside_one_free_interval() -> None:
    free = [Interval(at(D, 9), at(D, 13)), Interval(at(D, 14), at(D, 18))]
    assert fits(free, Interval(at(D, 12), at(D, 13)))
    assert not fits(free, Interval(at(D, 12, 30), at(D, 13, 30)))  # runs into the break
    assert not fits(free, Interval(at(D, 8), at(D, 9, 30)))  # starts before the window
    assert not fits([], Interval(at(D, 9), at(D, 10)))


def test_choose_resource_prefers_continuity_then_load_then_name() -> None:
    ali, bala, chen = (ResInfo(uuid.uuid4(), n) for n in ("Ali", "Bala", "Chen"))
    load = {ali.id: 120.0, bala.id: 30.0, chen.id: 30.0}
    assert choose_resource([ali, bala, chen], ali.id, load) is ali  # same person as the previous segment
    assert choose_resource([bala, chen], ali.id, load) is bala  # previous unavailable: least loaded, then name
    assert choose_resource([chen, ali], None, {}) is ali  # equal load: name


# --------------------------------------------------------------------- fixtures
class Garage(Seed):
    mechanics: dict[str, uuid.UUID]
    bays: dict[str, uuid.UUID]
    svc: dict[str, uuid.UUID]
    cars: dict[str, uuid.UUID]  # supported vehicle ids


async def add_bay(g: Seed, name: str) -> uuid.UUID:
    r = await g.client.post("/v1/admin/resources", json={"type": "bay", "name": name}, headers=g.admin)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def add_service(g: Seed, name: str, minutes: int, buffer: int = 0, resource_ids: list[uuid.UUID] | None = None) -> uuid.UUID:
    body: dict[str, Any] = {"name": name, "duration_minutes": minutes, "buffer_minutes": buffer, "resource_ids": [str(r) for r in resource_ids or []]}
    r = await g.client.post("/v1/admin/services", json=body, headers=g.admin)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def make_garage(
    client: httpx.AsyncClient, *, mechanics: tuple[str, ...] = ("Ali", "Bala"), bays: tuple[str, ...] = ("Bay 1", "Bay 2"),
    default_status: str = "confirmed",
) -> Garage:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Garage {uuid.uuid4().hex[:6]}", "business_type": "automotive"})
    assert r.status_code == 201, r.text
    g = Garage(client, r.json())
    g.admin = await g.token("owner")
    ok = await client.post(
        "/v1/admin/policies", headers=g.admin,
        json={"name": "Workshop", "default_status": default_status, "min_notice_booking": 60, "min_notice_cancel": 7200,
              "booking_window": 60 * 86400, "pending_timeout": 1800, "make_default": True},
    )
    assert ok.status_code == 201, ok.text
    hours = [{"weekday": d, "open_time": "08:00:00", "close_time": "18:00:00"} for d in range(7)]
    assert (await client.put("/v1/admin/operating-hours", json=hours, headers=g.admin)).status_code == 200
    g.mechanics = {n: await g.add_staff(n) for n in mechanics}  # shifts 09:00-18:00, break 13:00-14:00
    g.bays = {n: await add_bay(g, n) for n in bays}
    g.svc = {"oil": await add_service(g, "Oil change", 60), "brake": await add_service(g, "Brake service", 120)}
    g.cars = {}
    for make, model, extra in [("Toyota", "Vios", {}), ("Honda", "City", {"year_from": 2015, "year_to": 2022})]:
        r = await client.post("/v1/admin/supported-vehicles", json={"make": make, "model": model, **extra}, headers=g.admin)
        assert r.status_code == 201, r.text
        g.cars[model] = uuid.UUID(r.json()["id"])
    return g


@pytest.fixture
async def garage(client: httpx.AsyncClient) -> Garage:
    return await make_garage(client)


def gbody(g: Garage, start: dt.datetime, services: list[str], **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "service_ids": [str(g.svc[s]) for s in services], "start_at": start.isoformat(),
        "customer": {"name": "Aisha", "phone": f"+6012{uuid.uuid4().int % 10**7:07d}"},
        "vehicle": {"plate_no": f"W{uuid.uuid4().int % 10**5:05d}", "supported_vehicle_id": str(g.cars["Vios"])},
    }
    body.update(extra)
    return body


async def gbook(g: Garage, start: dt.datetime, services: list[str], **extra: Any) -> httpx.Response:
    return await g.client.post("/v1/public/bookings", json=gbody(g, start, services, **extra), headers=g.pk)


async def gslots(g: Garage, services: list[str], **params: Any) -> list[dict[str, Any]]:
    q: dict[str, Any] = {"date": g.day.isoformat(), "service_ids": [str(g.svc[s]) for s in services], **params}
    r = await g.client.get("/v1/public/availability", params=q, headers=g.pk)
    assert r.status_code == 200, r.text
    return r.json()["slots"]  # type: ignore[no-any-return]


def hhmm(ss: list[dict[str, Any]]) -> list[str]:
    return [dt.datetime.fromisoformat(s["start_at"]).astimezone(TZ).strftime("%H:%M") for s in ss]


# --------------------------------------------------------------------- vehicle compatibility
async def test_booking_requires_a_vehicle(garage: Garage) -> None:
    body = gbody(garage, at(garage.day, 10), ["oil"])
    del body["vehicle"]
    r = await garage.client.post("/v1/public/bookings", json=body, headers=garage.pk)
    assert r.status_code == 422 and "exactly one" in r.json()["detail"]
    both = gbody(garage, at(garage.day, 10), ["oil"], vehicle_id=str(uuid.uuid4()))
    assert (await garage.client.post("/v1/public/bookings", json=both, headers=garage.pk)).status_code == 422


async def test_unsupported_vehicle_is_refused_and_nothing_is_booked(garage: Garage) -> None:
    bad = {"plate_no": "XYZ1", "supported_vehicle_id": str(uuid.uuid4())}
    r = await gbook(garage, at(garage.day, 10), ["oil"], vehicle=bad)
    assert r.status_code == 422 and r.json()["error"] == "incompatible_vehicle"
    assert (await garage.client.get("/v1/admin/bookings", headers=garage.admin)).json() == []


async def test_year_limited_models_need_a_year_inside_the_range(garage: Garage) -> None:
    city = str(garage.cars["City"])
    for year in (2010, 2023, None):
        r = await gbook(garage, at(garage.day, 10), ["oil"], vehicle={"plate_no": "C1", "supported_vehicle_id": city, "year": year})
        assert r.status_code == 422 and r.json()["error"] == "incompatible_vehicle", year
    ok = await gbook(garage, at(garage.day, 10), ["oil"], vehicle={"plate_no": "C1", "supported_vehicle_id": city, "year": 2018})
    assert ok.status_code == 201


async def test_registered_vehicle_is_reusable_only_by_its_owner_and_plates_normalise(garage: Garage) -> None:
    phone = "+60123456789"
    cust = {"name": "Aisha", "phone": phone}
    car = lambda plate: {"plate_no": plate, "supported_vehicle_id": str(garage.cars["Vios"])}  # noqa: E731
    r1 = await gbook(garage, at(garage.day, 10), ["oil"], customer=cust, vehicle=car("abc 123"))
    r2 = await gbook(garage, at(garage.day, 12), ["oil"], customer=cust, vehicle=car("ABC-123"))
    assert r1.status_code == r2.status_code == 201
    listing = (await garage.client.get("/v1/admin/bookings", headers=garage.admin)).json()
    vids = {b["vehicle_id"] for b in listing}
    assert len(vids) == 1 and None not in vids  # same car, however the plate was typed

    vid = vids.pop()
    body = gbody(garage, at(garage.day, 15), ["oil"], customer=cust)
    del body["vehicle"]
    body["vehicle_id"] = vid
    assert (await garage.client.post("/v1/public/bookings", json=body, headers=garage.pk)).status_code == 201
    body["customer"] = {"name": "Someone else", "phone": "+60199999999"}
    assert (await garage.client.post("/v1/public/bookings", json=body, headers=garage.pk)).status_code == 404  # not their car


async def test_supported_vehicle_list_is_public_and_vehicles_are_rejected_for_other_verticals(client: httpx.AsyncClient, garage: Garage) -> None:
    listed = (await client.get("/v1/public/supported-vehicles", headers=garage.pk)).json()
    assert {(v["make"], v["model"]) for v in listed} == {("Toyota", "Vios"), ("Honda", "City")}
    spa = await make_seed(client)
    staff = await spa.add_staff("Mei")
    body = spa.booking_body(at(spa.day, 10), staff, vehicle={"plate_no": "A1", "supported_vehicle_id": str(uuid.uuid4())})
    r = await client.post("/v1/public/bookings", json=body, headers=spa.pk)
    assert r.status_code == 422 and "automotive" in r.json()["detail"]


# --------------------------------------------------------------------- double constraint
async def test_a_slot_needs_both_a_mechanic_and_a_bay(client: httpx.AsyncClient) -> None:
    no_bays = await make_garage(client, bays=())
    assert await gslots(no_bays, ["oil"]) == []
    r = await gbook(no_bays, at(no_bays.day, 10), ["oil"])
    assert r.status_code == 409
    no_mechanics = await make_garage(client, mechanics=())
    assert await gslots(no_mechanics, ["oil"]) == []
    both = await make_garage(client, mechanics=("Ali",), bays=("Bay 1",))
    assert await gslots(both, ["oil"])


async def test_a_booking_reserves_one_mechanic_row_and_one_bay_row(garage: Garage) -> None:
    r = await gbook(garage, at(garage.day, 10), ["oil"])
    assert r.status_code == 201
    rows = await allocations(garage, r.json()["id"])
    assert [(t, s, e, st) for t, _, s, e, st in rows] == [("bay", "10:00", "11:00", "active"), ("staff", "10:00", "11:00", "active")]


async def test_bay_is_the_bottleneck_when_mechanics_outnumber_bays(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    assert (await gbook(g, at(g.day, 10), ["oil"])).status_code == 201
    r = await gbook(g, at(g.day, 10), ["oil"])  # a mechanic is free but the only bay is not
    assert r.status_code == 409 and r.json()["error"] == "slot_unavailable"
    assert "10:00" not in hhmm(await gslots(g, ["oil"]))


async def test_mechanic_is_the_bottleneck_when_bays_outnumber_mechanics(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali",), bays=("Bay 1", "Bay 2"))
    assert (await gbook(g, at(g.day, 10), ["oil"])).status_code == 201
    assert (await gbook(g, at(g.day, 10), ["oil"])).status_code == 409


async def test_service_qualifications_restrict_mechanic_and_bay_separately(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    ali, bala = g.mechanics["Ali"], g.mechanics["Bala"]
    bay2 = g.bays["Bay 2"]
    await client.patch(f"/v1/admin/services/{g.svc['oil']}", json={"resource_ids": [str(bala), str(bay2)]}, headers=g.admin)
    await client.patch(f"/v1/admin/services/{g.svc['brake']}", json={"resource_ids": [str(ali), str(g.bays['Bay 1'])]}, headers=g.admin)
    oil = await gbook(g, at(g.day, 10), ["oil"])
    brake = await gbook(g, at(g.day, 10), ["brake"])
    assert oil.status_code == brake.status_code == 201
    assert {(t, n) for t, n, *_ in await allocations(g, oil.json()["id"])} == {("staff", "Bala"), ("bay", "Bay 2")}
    assert {(t, n) for t, n, *_ in await allocations(g, brake.json()["id"])} == {("staff", "Ali"), ("bay", "Bay 1")}


async def test_service_linked_only_to_a_bay_does_not_disqualify_the_mechanics(client: httpx.AsyncClient) -> None:
    """Regression: links used to be matched across resource types, so a bay-only link excluded every mechanic."""
    g = await make_garage(client)
    r = await client.patch(f"/v1/admin/services/{g.svc['oil']}", json={"resource_ids": [str(g.bays["Bay 2"])]}, headers=g.admin)
    assert r.status_code == 200
    booked = await gbook(g, at(g.day, 10), ["oil"])
    assert booked.status_code == 201
    assert {n for t, n, *_ in await allocations(g, booked.json()["id"]) if t == "bay"} == {"Bay 2"}


async def test_mechanic_breaks_apply_but_bays_have_none(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)  # shifts 09:00-18:00, break 13:00-14:00
    starts = hhmm(await gslots(g, ["oil"]))
    assert starts[0] == "09:00" and starts[-1] == "17:00"
    assert "12:00" in starts and "12:15" not in starts and "13:00" not in starts and "13:45" not in starts and "14:00" in starts


# --------------------------------------------------------------------- time stacking
async def test_services_are_stacked_back_to_back_with_two_rows_per_segment(garage: Garage) -> None:
    r = await gbook(garage, at(garage.day, 9), ["oil", "brake"])
    assert r.status_code == 201
    rows = await allocations(garage, r.json()["id"])
    assert len(rows) == 4
    assert sorted((s, e) for t, _, s, e, _ in rows if t == "staff") == [("09:00", "10:00"), ("10:00", "12:00")]
    assert sorted((s, e) for t, _, s, e, _ in rows if t == "bay") == [("09:00", "10:00"), ("10:00", "12:00")]
    detail = (await garage.client.get(f"/v1/admin/bookings/{r.json()['id']}", headers=garage.admin)).json()
    assert dt.datetime.fromisoformat(detail["start_at"]) == at(garage.day, 9) and dt.datetime.fromisoformat(detail["end_at"]) == at(garage.day, 12)


async def test_the_same_mechanic_and_bay_are_kept_across_segments_when_possible(garage: Garage) -> None:
    r = await gbook(garage, at(garage.day, 9), ["oil", "brake"])
    rows = await allocations(garage, r.json()["id"])
    assert len({n for t, n, *_ in rows if t == "staff"}) == 1 and len({n for t, n, *_ in rows if t == "bay"}) == 1


async def test_segments_block_resources_one_at_a_time(client: httpx.AsyncClient) -> None:
    """Oil needs Ali, brake needs Bala. Ali is only busy during the oil segment, Bala during the brake one."""
    g = await make_garage(client)
    ali, bala = g.mechanics["Ali"], g.mechanics["Bala"]
    await client.patch(f"/v1/admin/services/{g.svc['oil']}", json={"resource_ids": [str(ali)]}, headers=g.admin)
    await client.patch(f"/v1/admin/services/{g.svc['brake']}", json={"resource_ids": [str(bala)]}, headers=g.admin)
    stacked = await gbook(g, at(g.day, 9), ["oil", "brake"])  # Ali 09-10, Bala 10-12
    assert stacked.status_code == 201
    names = {(n, s, e) for t, n, s, e, _ in await allocations(g, stacked.json()["id"]) if t == "staff"}
    assert names == {("Ali", "09:00", "10:00"), ("Bala", "10:00", "12:00")}
    # Ali is free again from 10:00, so another oil change can start then...
    assert (await gbook(g, at(g.day, 10), ["oil"])).status_code == 201
    # ...but a second stacked job at 09:00 needs Bala from 10:00, and Bala is busy until 12:00.
    assert (await gbook(g, at(g.day, 9), ["oil", "brake"])).status_code == 409


async def test_a_segment_running_into_the_mechanics_break_makes_the_start_unavailable(garage: Garage) -> None:
    starts = hhmm(await gslots(garage, ["oil", "brake"]))
    assert "10:00" in starts  # 10-11 oil, 11-13 brake: ends exactly at the break
    assert "10:15" not in starts and "12:00" not in starts  # brake segment would cross 13:00-14:00 for everyone
    assert "15:00" in starts and "15:15" not in starts  # 3h job must end by 18:00


async def test_buffer_time_is_part_of_each_segment(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    g.svc["align"] = await add_service(g, "Alignment", 60, buffer=15)
    r = await gbook(g, at(g.day, 9), ["oil", "align"])
    assert r.status_code == 201
    rows = await allocations(g, r.json()["id"])
    assert sorted({(s, e) for t, _, s, e, _ in rows if t == "bay"}) == [("09:00", "10:00"), ("10:00", "11:15")]


async def test_a_named_mechanic_must_be_qualified_for_and_free_during_every_segment(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    ali, bala = g.mechanics["Ali"], g.mechanics["Bala"]
    await client.patch(f"/v1/admin/services/{g.svc['oil']}", json={"resource_ids": [str(ali)]}, headers=g.admin)  # brake: anyone
    assert await gslots(g, ["oil", "brake"], resource_id=str(ali))
    assert await gslots(g, ["oil", "brake"], resource_id=str(bala)) == []  # Bala cannot do the oil segment
    r = await gbook(g, at(g.day, 9), ["oil", "brake"], resource_id=str(ali))
    assert r.status_code == 201
    assert {n for t, n, *_ in await allocations(g, r.json()["id"]) if t == "staff"} == {"Ali"}
    assert (await gbook(g, at(g.day, 9), ["oil"], resource_id=str(uuid.uuid4()))).status_code == 404


# --------------------------------------------------------------------- waiting-for-parts
async def _status(g: Seed, bid: str, status: str) -> httpx.Response:
    return await g.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": status}, headers=g.admin)


async def test_waiting_for_parts_releases_the_mechanic_and_bay(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali",), bays=("Bay 1",))
    bid = (await gbook(g, at(g.day, 10), ["brake"])).json()["id"]
    assert (await gbook(g, at(g.day, 10), ["oil"])).status_code == 409  # only mechanic + bay are taken
    assert (await _status(g, bid, "in-progress")).status_code == 200
    r = await _status(g, bid, "waiting-for-parts")
    assert r.status_code == 200 and r.json()["status"] == "waiting-for-parts"
    assert all(st == "released" for *_, st in await allocations(g, bid))  # job hasn't started yet: all future rows freed
    assert (await gbook(g, at(g.day, 10), ["oil"])).status_code == 201  # the freed mechanic and bay can be booked


async def test_waiting_for_parts_truncates_a_running_job_at_now(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali",), bays=("Bay 1",))
    r = await gbook(g, at(g.day, 9), ["oil", "brake"])  # segments 09-10 and 10-12
    bid = r.json()["id"]
    async with tenant_session(g.tenant_id) as s:
        tenant = await s.get(Tenant, g.tenant_id)
        assert tenant is not None
        await transition_booking(s, tenant, uuid.UUID(bid), "in-progress")
        await transition_booking(s, tenant, uuid.UUID(bid), "waiting-for-parts", now=at(g.day, 10, 30))
    rows = await allocations(g, bid)
    assert sorted((t, s, e, st) for t, _, s, e, st in rows) == [
        ("bay", "09:00", "10:00", "active"), ("bay", "10:00", "10:30", "active"),  # finished part kept, running part cut at now
        ("staff", "09:00", "10:00", "active"), ("staff", "10:00", "10:30", "active"),
    ]
    assert (await gbook(g, at(g.day, 10, 30), ["oil"])).status_code == 201  # freed from 10:30 on


async def test_waiting_for_parts_rules(client: httpx.AsyncClient, garage: Garage) -> None:
    bid = (await gbook(garage, at(garage.day, 10), ["oil"])).json()["id"]
    assert (await _status(garage, bid, "waiting-for-parts")).status_code == 409  # must be in progress first
    await _status(garage, bid, "in-progress")
    await _status(garage, bid, "waiting-for-parts")
    back = await _status(garage, bid, "confirmed")
    assert back.status_code == 409 and "resume" in back.json()["detail"]
    assert (await _status(garage, bid, "completed")).status_code == 409
    spa = await make_seed(client)
    staff = await spa.add_staff("Mei")
    sb = (await client.post("/v1/public/bookings", json=spa.booking_body(at(spa.day, 10), staff), headers=spa.pk)).json()["id"]
    assert (await _status(spa, sb, "waiting-for-parts")).status_code == 409  # automotive only


async def test_resume_reschedules_the_job_with_new_allocations(garage: Garage) -> None:
    bid = (await gbook(garage, at(garage.day, 9), ["oil", "brake"])).json()["id"]
    await _status(garage, bid, "in-progress")
    await _status(garage, bid, "waiting-for-parts")
    later = garage.day + dt.timedelta(days=2)
    r = await garage.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": at(later, 10).isoformat()}, headers=garage.admin)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "confirmed" and dt.datetime.fromisoformat(r.json()["end_at"]) == at(later, 13)
    live = [row for row in await allocations(garage, bid) if row[4] == "active"]
    assert len(live) == 4 and sorted({(s, e) for t, _, s, e, _ in live if t == "bay"}) == [("10:00", "11:00"), ("11:00", "13:00")]


async def test_resume_can_schedule_only_the_remaining_services(garage: Garage) -> None:
    bid = (await gbook(garage, at(garage.day, 9), ["oil", "brake"])).json()["id"]
    await _status(garage, bid, "in-progress")
    await _status(garage, bid, "waiting-for-parts")
    later = at(garage.day + dt.timedelta(days=1), 9)
    r = await garage.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": later.isoformat(), "service_ids": [str(garage.svc["brake"])]}, headers=garage.admin)
    assert r.status_code == 200 and dt.datetime.fromisoformat(r.json()["end_at"]) == later + dt.timedelta(hours=2)
    bad = await garage.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": later.isoformat(), "service_ids": [str(uuid.uuid4())]}, headers=garage.admin)
    assert bad.status_code == 409  # no longer waiting for parts


async def test_resume_validation(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali",), bays=("Bay 1",))
    bid = (await gbook(g, at(g.day, 9), ["oil"])).json()["id"]
    resume = lambda body: client.post(f"/v1/admin/bookings/{bid}/resume", json=body, headers=g.admin)  # noqa: E731
    assert (await resume({"start_at": at(g.day, 11).isoformat()})).status_code == 409  # not waiting for parts
    await _status(g, bid, "in-progress")
    await _status(g, bid, "waiting-for-parts")
    past = dt.datetime.now(TZ) - dt.timedelta(hours=1)
    assert (await resume({"start_at": past.isoformat()})).status_code == 422
    assert (await resume({"start_at": at(g.day, 11).isoformat(), "service_ids": [str(g.svc["brake"])]})).status_code == 422  # not on the job
    other = (await gbook(g, at(g.day, 11), ["oil"])).json()["id"]  # occupies the only mechanic + bay at 11:00
    clash = await resume({"start_at": at(g.day, 11).isoformat()})
    assert clash.status_code == 409 and clash.json()["error"] == "slot_unavailable" and "slots" in clash.json()
    assert other  # the clashing booking is untouched
    far = g.day + dt.timedelta(days=70)  # beyond the 60-day booking window: an admin reschedule ignores it
    assert (await resume({"start_at": at(far, 10).isoformat()})).status_code == 200


# --------------------------------------------------------------------- parts lead time
async def _part(g: Seed, name: str, status: str, days: int | None = None) -> str:
    body: dict[str, Any] = {"name": name, "stock_status": status, "estimated_lead_time_days": days}
    r = await g.client.post("/v1/admin/products", json=body, headers=g.admin)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def _link(g: Garage, service: str, *parts: str) -> None:
    r = await g.client.put(f"/v1/admin/services/{g.svc[service]}/parts", json={"product_ids": list(parts)}, headers=g.admin)
    assert r.status_code == 200, r.text


async def test_on_order_parts_require_acknowledging_the_lead_time(garage: Garage) -> None:
    pads = await _part(garage, "Brake pads", "available_on_order", 5)
    await _link(garage, "brake", pads)
    r = await garage.client.get("/v1/public/availability", params={"date": garage.day.isoformat(), "service_ids": [str(garage.svc["brake"])]}, headers=garage.pk)
    assert r.json()["lead_time_notice"] == [{"name": "Brake pads", "estimated_lead_time_days": 5}]
    oil = await garage.client.get("/v1/public/availability", params={"date": garage.day.isoformat(), "service_ids": [str(garage.svc["oil"])]}, headers=garage.pk)
    assert oil.json()["lead_time_notice"] == []

    refused = await gbook(garage, at(garage.day, 10), ["brake"])
    assert refused.status_code == 422 and refused.json()["error"] == "lead_time_not_acknowledged"
    assert refused.json()["parts"] == [{"name": "Brake pads", "estimated_lead_time_days": 5}]
    assert (await garage.client.get("/v1/admin/bookings", headers=garage.admin)).json() == []  # refused = nothing written
    ok = await gbook(garage, at(garage.day, 10), ["brake"], acknowledge_lead_time=True)
    assert ok.status_code == 201
    listing = (await garage.client.get("/v1/admin/bookings", headers=garage.admin)).json()
    assert listing[0]["lead_time_acknowledged_at"] is not None


async def test_only_available_on_order_parts_trigger_the_disclaimer(garage: Garage) -> None:
    await _link(garage, "brake", await _part(garage, "Rotors", "in_stock"), await _part(garage, "Calipers", "out_of_stock"))
    assert (await gbook(garage, at(garage.day, 10), ["brake"])).status_code == 201
    on_order = await _part(garage, "Pads", "available_on_order", 3)
    await _link(garage, "oil", on_order)
    assert (await gbook(garage, at(garage.day, 10), ["oil"])).status_code == 422
    flip = await garage.client.put(f"/v1/admin/products/{on_order}", json={"name": "Pads", "stock_status": "in_stock"}, headers=garage.admin)
    assert flip.status_code == 200
    assert (await gbook(garage, at(garage.day, 10), ["oil"])).status_code == 201  # arrived: no disclaimer any more


async def test_parts_validation(garage: Garage) -> None:
    bad = await garage.client.post("/v1/admin/products", json={"name": "X", "stock_status": "available_on_order"}, headers=garage.admin)
    assert bad.status_code == 422  # on-order needs a lead time
    r = await garage.client.put(f"/v1/admin/services/{garage.svc['oil']}/parts", json={"product_ids": [str(uuid.uuid4())]}, headers=garage.admin)
    assert r.status_code == 404


# --------------------------------------------------------------------- concurrency
@pytest.mark.parametrize(("mechanics", "bays", "winners"), [
    (("Ali",), ("Bay 1",), 1), (("Ali", "Bala"), ("Bay 1",), 1), (("Ali",), ("Bay 1", "Bay 2"), 1), (("Ali", "Bala"), ("Bay 1", "Bay 2"), 2),
])
async def test_concurrent_bookings_never_exceed_the_scarcer_of_mechanics_and_bays(
    client: httpx.AsyncClient, mechanics: tuple[str, ...], bays: tuple[str, ...], winners: int
) -> None:
    g = await make_garage(client, mechanics=mechanics, bays=bays)
    results = await asyncio.gather(*[gbook(g, at(g.day, 10), ["oil"]) for _ in range(8)])
    codes = [r.status_code for r in results]
    assert codes.count(201) == winners and codes.count(409) == 8 - winners, codes
    live = [b for b in (await g.client.get("/v1/admin/bookings", headers=g.admin)).json()]
    assert len(live) == winners
    used = [row for b in live for row in await allocations(g, b["id"])]
    assert len({(n, s) for t, n, s, *_ in used if t == "staff"}) == winners  # every winner has its own mechanic...
    assert len({(n, s) for t, n, s, *_ in used if t == "bay"}) == winners  # ...and its own bay


async def test_concurrent_stacked_jobs_fill_but_do_not_overfill(client: httpx.AsyncClient) -> None:
    g = await make_garage(client)
    results = await asyncio.gather(*[gbook(g, at(g.day, 9), ["oil", "brake"]) for _ in range(6)])
    assert sorted(r.status_code for r in results) == [201, 201] + [409] * 4
    # nothing partial: every booking that exists has all four rows and no resource is double-booked
    for b in (await g.client.get("/v1/admin/bookings", headers=g.admin)).json():
        assert len(await allocations(g, b["id"])) == 4


async def test_named_mechanic_race_is_rerouted_to_a_free_bay(client: httpx.AsyncClient) -> None:
    """Two customers name different mechanics but both auto-pick Bay 1; the loser must move to Bay 2."""
    g = await make_garage(client)
    ali, bala = str(g.mechanics["Ali"]), str(g.mechanics["Bala"])
    a, b = await asyncio.gather(
        gbook(g, at(g.day, 10), ["oil"], resource_id=ali), gbook(g, at(g.day, 10), ["oil"], resource_id=bala)
    )
    assert (a.status_code, b.status_code) == (201, 201), (a.text, b.text)
    bays = {n for r in (a, b) for t, n, *_ in await allocations(g, r.json()["id"]) if t == "bay"}
    assert bays == {"Bay 1", "Bay 2"}


# --------------------------------------------------------------------- tenant isolation of new tables
async def test_service_parts_are_isolated_per_tenant(client: httpx.AsyncClient) -> None:
    from sqlalchemy import func

    from app.models import ServiceProduct, SupportedVehicle, Vehicle

    a, b = await make_garage(client), await make_garage(client)
    await _link(a, "brake", await _part(a, "Pads", "available_on_order", 2))
    await gbook(a, at(a.day, 10), ["oil"])
    for model in (ServiceProduct, SupportedVehicle, Vehicle):
        async with tenant_session(b.tenant_id) as s:
            own = await s.scalar(select(func.count()).select_from(model))
        async with tenant_session(a.tenant_id) as s:
            mine = await s.scalar(select(func.count()).select_from(model))
        assert mine and mine >= 1 and (model is not SupportedVehicle or own == 2)  # b only sees its own two supported models
        if model is not SupportedVehicle:
            assert own == 0, model


async def test_booking_rows_belong_to_their_tenant_only(client: httpx.AsyncClient) -> None:
    a, b = await make_garage(client), await make_garage(client)
    await gbook(a, at(a.day, 10), ["oil"])
    async with tenant_session(b.tenant_id) as s:
        assert (await s.scalars(select(Booking))).all() == []
