"""Field-service allocator: PostGIS radius, travel buffers, virtual links, two-step workflow, overruns."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.database import tenant_session
from app.services import geo, meetings
from app.services.allocators.field_service import travel_pad
from tests.conftest import PLATFORM, TZ, Seed, allocations, at, make_seed

KL = (3.1390, 101.6869)  # the business
NEAR = (3.1200, 101.6700)  # ~3 km away
SUBANG = (3.0738, 101.5183)  # ~20 km away
SEREMBAN = (2.7297, 101.9381)  # ~55 km away


def test_only_on_site_visits_reserve_travel_time() -> None:
    assert travel_pad("on_site", 30) == dt.timedelta(minutes=30)
    assert travel_pad("in_office", 30) == dt.timedelta(0)
    assert travel_pad("virtual", 30) == dt.timedelta(0)
    assert travel_pad("on_site", 0) == dt.timedelta(0)


# --------------------------------------------------------------------- fixtures
class Practice(Seed):
    pros: dict[str, uuid.UUID]
    svc: dict[str, uuid.UUID]


async def add_service(p: Seed, name: str, minutes: int, *, category: str | None, location: str, travel: int = 0) -> uuid.UUID:
    body = {"name": name, "duration_minutes": minutes, "service_category": category, "location_type": location, "travel_buffer_minutes": travel}
    r = await p.client.post("/v1/admin/services", json=body, headers=p.admin)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def make_practice(
    client: httpx.AsyncClient, *, pros: tuple[str, ...] = ("Dr Lim",), radius: float | None = 25.0, default_status: str = "confirmed",
) -> Practice:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Pro {uuid.uuid4().hex[:6]}", "business_type": "professional"})
    assert r.status_code == 201, r.text
    p = Practice(client, r.json())
    p.admin = await p.token("owner")
    ok = await client.post(
        "/v1/admin/policies", headers=p.admin,
        json={"name": "Pro", "default_status": default_status, "min_notice_booking": 60, "min_notice_cancel": 7200,
              "booking_window": 60 * 86400, "pending_timeout": 1800, "make_default": True},
    )
    assert ok.status_code == 201, ok.text
    hours = [{"weekday": d, "open_time": "08:00:00", "close_time": "18:00:00"} for d in range(7)]
    assert (await client.put("/v1/admin/operating-hours", json=hours, headers=p.admin)).status_code == 200
    if radius is not None:
        loc = await client.put("/v1/admin/business-location", json={"latitude": KL[0], "longitude": KL[1], "service_radius_km": radius}, headers=p.admin)
        assert loc.status_code == 200, loc.text
    p.pros = {n: await p.add_staff(n) for n in pros}  # shifts 09:00-18:00, break 13:00-14:00
    p.svc = {
        "site": await add_service(p, "Site inspection", 60, category="inspection", location="on_site", travel=30),
        "work": await add_service(p, "Repair work", 120, category="standard", location="on_site", travel=30),
        "office": await add_service(p, "Office consult", 60, category="inspection", location="in_office", travel=30),
        "video": await add_service(p, "Video call", 30, category="inspection", location="virtual"),
    }
    return p


@pytest.fixture
async def practice(client: httpx.AsyncClient) -> Practice:
    return await make_practice(client)


def pin(point: tuple[float, float], line1: str = "1 Jalan Test") -> dict[str, Any]:
    return {"line1": line1, "latitude": point[0], "longitude": point[1]}


def pbody(p: Practice, start: dt.datetime, service: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "service_ids": [str(p.svc[service])], "start_at": start.isoformat(),
        "customer": {"name": "Aisha", "phone": f"+6012{uuid.uuid4().int % 10**7:07d}"},
    }
    body.update(extra)
    return body


async def pbook(p: Practice, start: dt.datetime, service: str, **extra: Any) -> httpx.Response:
    return await p.client.post("/v1/public/bookings", json=pbody(p, start, service, **extra), headers=p.pk)


async def pslots(p: Practice, service: str, **params: Any) -> httpx.Response:
    q = {"date": p.day.isoformat(), "service_ids": str(p.svc[service]), **params}
    return await p.client.get("/v1/public/availability", params=q, headers=p.pk)


def hhmm(r: httpx.Response) -> list[str]:
    return [dt.datetime.fromisoformat(s["start_at"]).astimezone(TZ).strftime("%H:%M") for s in r.json()["slots"]]


async def status(p: Seed, bid: str, new: str) -> httpx.Response:
    return await p.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": new}, headers=p.admin)


# --------------------------------------------------------------------- service radius (PostGIS)
@pytest.mark.usefixtures("postgis")
async def test_on_site_availability_needs_coordinates_and_offers_slots_inside_the_radius(practice: Practice) -> None:
    missing = await pslots(practice, "site")
    assert missing.status_code == 422 and "latitude" in missing.json()["detail"]
    ok = await pslots(practice, "site", latitude=NEAR[0], longitude=NEAR[1])
    assert ok.status_code == 200 and ok.json()["slots"]
    subang = await pslots(practice, "site", latitude=SUBANG[0], longitude=SUBANG[1])
    assert subang.status_code == 200  # ~20 km is inside the 25 km radius


@pytest.mark.usefixtures("postgis")
async def test_addresses_outside_the_radius_are_refused_everywhere_and_nothing_is_written(practice: Practice) -> None:
    far = await pslots(practice, "site", latitude=SEREMBAN[0], longitude=SEREMBAN[1])
    assert far.status_code == 422 and far.json()["error"] == "outside_service_area"
    assert far.json()["service_radius_km"] == 25.0 and far.json()["distance_km"] > 50
    booked = await pbook(practice, at(practice.day, 10), "site", address=pin(SEREMBAN))
    assert booked.status_code == 422 and booked.json()["error"] == "outside_service_area"
    assert (await practice.client.get("/v1/admin/bookings", headers=practice.admin)).json() == []


@pytest.mark.usefixtures("postgis")
async def test_radius_boundary_is_exact(practice: Practice) -> None:
    """Set the radius a hair above/below the measured PostGIS distance and check both sides."""
    async with tenant_session(None) as s:
        km = float((await s.execute(text(
            "SELECT ST_Distance(ST_SetSRID(ST_MakePoint(:a,:b),4326)::geography, ST_SetSRID(ST_MakePoint(:c,:d),4326)::geography)/1000.0"
        ), {"a": KL[1], "b": KL[0], "c": SUBANG[1], "d": SUBANG[0]})).scalar_one())
    assert 19.5 < km < 20.6  # sanity: Kuala Lumpur to Subang
    put = lambda r: practice.client.put("/v1/admin/business-location", json={"latitude": KL[0], "longitude": KL[1], "service_radius_km": r}, headers=practice.admin)  # noqa: E731
    assert (await put(km + 0.01)).status_code == 200
    assert (await pslots(practice, "site", latitude=SUBANG[0], longitude=SUBANG[1])).status_code == 200
    assert (await put(km - 0.01)).status_code == 200
    assert (await pslots(practice, "site", latitude=SUBANG[0], longitude=SUBANG[1])).json()["error"] == "outside_service_area"


@pytest.mark.usefixtures("postgis")
async def test_business_location_must_be_configured_and_is_validated(client: httpx.AsyncClient) -> None:
    bare = await make_practice(client, radius=None)
    r = await pslots(bare, "site", latitude=NEAR[0], longitude=NEAR[1])
    assert r.status_code == 422 and "service radius" in r.json()["detail"]
    for bad in ({"latitude": 91, "longitude": 0, "service_radius_km": 5}, {"latitude": 0, "longitude": 181, "service_radius_km": 5},
                {"latitude": 0, "longitude": 0, "service_radius_km": 0}):
        assert (await client.put("/v1/admin/business-location", json=bad, headers=bare.admin)).status_code == 422
    got = await client.get("/v1/admin/business-location", headers=bare.admin)
    assert got.json() == {"latitude": None, "longitude": None, "service_radius_km": None}


async def test_missing_postgis_is_reported_clearly_and_does_not_break_the_request(
    practice: Practice, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(geo, "_MEASURE", text("SELECT st_this_function_does_not_exist(1)"))
    r = await pslots(practice, "site", latitude=NEAR[0], longitude=NEAR[1])
    assert r.status_code == 503 and r.json()["error"] == "postgis_unavailable"
    # in-office services never need PostGIS
    assert (await pslots(practice, "office")).status_code == 200


# --------------------------------------------------------------------- travel buffers
@pytest.mark.usefixtures("postgis")
async def test_on_site_visits_block_travel_time_before_and_after(practice: Practice) -> None:
    starts = hhmm(await pslots(practice, "site", latitude=NEAR[0], longitude=NEAR[1]))
    assert starts[0] == "09:30" and starts[-1] == "16:30"  # the 30-minute drive must also fit in the 09:00-18:00 shift
    assert hhmm(await pslots(practice, "office"))[0] == "09:00" and hhmm(await pslots(practice, "office"))[-1] == "17:00"  # no travel

    r = await pbook(practice, at(practice.day, 10), "site", address=pin(NEAR))
    assert r.status_code == 201, r.text
    rows = await allocations(practice, r.json()["id"])
    assert [(s, e, st) for _, _, s, e, st in rows] == [("09:30", "11:30", "active")]  # 10:00-11:00 plus 30 min each side
    detail = (await practice.client.get(f"/v1/admin/bookings/{r.json()['id']}", headers=practice.admin)).json()
    assert dt.datetime.fromisoformat(detail["start_at"]) == at(practice.day, 10) and dt.datetime.fromisoformat(detail["end_at"]) == at(practice.day, 11)


@pytest.mark.usefixtures("postgis")
async def test_travel_buffers_shape_what_can_be_booked_next(practice: Practice) -> None:
    assert (await pbook(practice, at(practice.day, 10), "site", address=pin(NEAR))).status_code == 201  # holds 09:30-11:30
    on_site = hhmm(await pslots(practice, "site", latitude=NEAR[0], longitude=NEAR[1]))
    assert "11:30" not in on_site and "12:00" not in on_site  # drive time would overlap / run into the lunch break
    assert on_site[0] == "14:30"  # first visit whose padded block fits after lunch
    office = hhmm(await pslots(practice, "office"))
    assert "11:30" in office and "11:15" not in office  # an in-office consult only needs the trailing drive time to be over


@pytest.mark.usefixtures("postgis")
async def test_on_site_needs_exactly_one_address_and_other_services_refuse_one(practice: Practice) -> None:
    none = await pbook(practice, at(practice.day, 10), "site")
    assert none.status_code == 422 and "exactly one" in none.json()["detail"]
    both = await pbook(practice, at(practice.day, 10), "site", address=pin(NEAR), address_id=str(uuid.uuid4()))
    assert both.status_code == 422
    office = await pbook(practice, at(practice.day, 10), "office", address=pin(NEAR))
    assert office.status_code == 422 and "on-site" in office.json()["detail"]
    assert (await pbook(practice, at(practice.day, 10), "office")).status_code == 201


@pytest.mark.usefixtures("postgis")
async def test_saved_addresses_are_reusable_only_by_their_owner(practice: Practice) -> None:
    phone = "+60123450000"
    cust = {"name": "Aisha", "phone": phone}
    assert (await pbook(practice, at(practice.day, 10), "site", customer=cust, address=pin(NEAR))).status_code == 201
    listing = (await practice.client.get("/v1/admin/bookings", headers=practice.admin)).json()
    address_id = listing[0]["address_id"]
    assert address_id is not None
    again = await pbook(practice, at(practice.day, 15), "site", customer=cust, address_id=address_id)
    assert again.status_code == 201
    other = await pbook(practice, at(practice.day, 16), "site", customer={"name": "Else", "phone": "+60199990000"}, address_id=address_id)
    assert other.status_code == 404


async def test_a_field_service_booking_takes_exactly_one_service(practice: Practice) -> None:
    body = pbody(practice, at(practice.day, 10), "office")
    body["service_ids"].append(str(practice.svc["video"]))
    assert (await practice.client.post("/v1/public/bookings", json=body, headers=practice.pk)).status_code == 422


@pytest.mark.usefixtures("postgis")
async def test_auto_assignment_prefers_the_less_loaded_professional_and_respects_qualification(client: httpx.AsyncClient) -> None:
    p = await make_practice(client, pros=("Ali", "Bala"))
    first = await pbook(p, at(p.day, 10), "office")
    second = await pbook(p, at(p.day, 15), "office")
    assert first.json()["resource_ids"] != second.json()["resource_ids"]  # the second goes to whoever is less loaded
    ali, bala = p.pros["Ali"], p.pros["Bala"]
    r = await client.patch(f"/v1/admin/services/{p.svc['video']}", json={"resource_ids": [str(bala)]}, headers=p.admin)
    assert r.status_code == 200
    booked = await pbook(p, at(p.day, 11), "video")
    assert booked.status_code == 201 and booked.json()["resource_ids"] == [str(bala)] and str(ali) not in booked.text


# --------------------------------------------------------------------- virtual appointments
class _FakeMeet:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[str] = []
        self.fail = fail

    async def create_meeting(self, *, booking: Any, tenant: Any) -> str | None:
        self.calls.append(str(booking.id))
        if self.fail:
            raise RuntimeError("calendar API down")
        return f"https://meet.example/{booking.id}"


@pytest.fixture
def meet(monkeypatch: pytest.MonkeyPatch) -> _FakeMeet:
    fake = _FakeMeet()
    monkeypatch.setattr(meetings, "_provider", fake)
    return fake


async def test_confirmed_virtual_appointments_get_a_meeting_link(practice: Practice, meet: _FakeMeet) -> None:
    r = await pbook(practice, at(practice.day, 10), "video")
    assert r.status_code == 201 and r.json()["meeting_link"] == f"https://meet.example/{r.json()['id']}"
    assert len(meet.calls) == 1
    await pbook(practice, at(practice.day, 12), "office")  # not virtual: the provider is not involved
    assert len(meet.calls) == 1


async def test_pending_virtual_appointments_get_the_link_when_approved(client: httpx.AsyncClient, meet: _FakeMeet) -> None:
    p = await make_practice(client, default_status="pending")
    r = await pbook(p, at(p.day, 10), "video")
    assert r.json()["status"] == "pending" and r.json()["meeting_link"] is None and meet.calls == []
    approved = await status(p, r.json()["id"], "confirmed")
    assert approved.status_code == 200 and approved.json()["meeting_link"] == f"https://meet.example/{r.json()['id']}"


async def test_a_failing_meeting_provider_never_fails_the_booking(practice: Practice, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeMeet(fail=True)
    monkeypatch.setattr(meetings, "_provider", fake)
    r = await pbook(practice, at(practice.day, 10), "video")
    assert r.status_code == 201 and r.json()["meeting_link"] is None and len(fake.calls) == 1


async def test_without_a_provider_the_link_can_be_set_by_hand(practice: Practice) -> None:
    r = await pbook(practice, at(practice.day, 10), "video")
    assert r.status_code == 201 and r.json()["meeting_link"] is None  # default provider creates nothing
    bid = r.json()["id"]
    put = lambda url: practice.client.patch(f"/v1/admin/bookings/{bid}/meeting-link", json={"url": url}, headers=practice.admin)  # noqa: E731
    assert (await put("javascript:alert(1)")).status_code == 422  # only http(s) links
    ok = await put("https://meet.example/abc")
    assert ok.status_code == 200 and ok.json()["meeting_link"] == "https://meet.example/abc"


# --------------------------------------------------------------------- two-step workflow
@pytest.mark.usefixtures("postgis")
async def test_follow_up_service_is_linked_to_its_inspection(practice: Practice) -> None:
    site = (await pbook(practice, at(practice.day, 9, 30), "site", address=pin(NEAR))).json()["id"]
    later = practice.day + dt.timedelta(days=1)
    fu = lambda **b: practice.client.post(f"/v1/admin/bookings/{site}/follow-up", json={"service_id": str(practice.svc["work"]), "start_at": at(later, 10).isoformat(), **b}, headers=practice.admin)  # noqa: E731
    early = await fu()
    assert early.status_code == 409  # the inspection has not started
    await status(practice, site, "in-progress")
    r = await fu()
    assert r.status_code == 201, r.text
    child = r.json()
    assert child["parent_booking_id"] == site and child["status"] == "confirmed" and child["source"] == "admin"
    parent = (await practice.client.get(f"/v1/admin/bookings/{site}", headers=practice.admin)).json()
    assert child["customer_id"] == parent["customer_id"] and child["address_id"] == parent["address_id"]
    rows = await allocations(practice, child["id"])
    assert [(s, e) for _, _, s, e, _ in rows] == [("09:30", "12:30")]  # 10:00-12:00 plus travel either side


@pytest.mark.usefixtures("postgis")
async def test_follow_up_rules(practice: Practice, client: httpx.AsyncClient) -> None:
    site = (await pbook(practice, at(practice.day, 9, 30), "site", address=pin(NEAR))).json()["id"]
    await status(practice, site, "in-progress")
    later = at(practice.day + dt.timedelta(days=1), 10)
    post = lambda bid, service, when=later: client.post(f"/v1/admin/bookings/{bid}/follow-up", json={"service_id": str(practice.svc[service]), "start_at": when.isoformat()}, headers=practice.admin)  # noqa: E731
    assert (await post(site, "site")).status_code == 422  # a follow-up must be a standard service
    past = dt.datetime.now(TZ) - dt.timedelta(hours=2)
    assert (await post(site, "work", past)).status_code == 422
    standard_parent = (await pbook(practice, at(practice.day, 15), "office")).json()["id"]
    await status(practice, standard_parent, "in-progress")
    ok = await client.post(f"/v1/admin/bookings/{standard_parent}/follow-up", json={"service_id": str(practice.svc["work"]), "start_at": later.isoformat()}, headers=practice.admin)
    assert ok.status_code == 422 and "address" in ok.json()["detail"]  # in-office inspection has no address to visit
    # shrink the service area: the on-site follow-up is checked against the radius again
    await client.put("/v1/admin/business-location", json={"latitude": SEREMBAN[0], "longitude": SEREMBAN[1], "service_radius_km": 5}, headers=practice.admin)
    far = await post(site, "work")
    assert far.status_code == 422 and far.json()["error"] == "outside_service_area"


async def test_follow_ups_and_overruns_are_for_professional_tenants_only(client: httpx.AsyncClient) -> None:
    spa = await make_seed(client)
    staff = await spa.add_staff("Mei")
    bid = (await client.post("/v1/public/bookings", json=spa.booking_body(at(spa.day, 10), staff), headers=spa.pk)).json()["id"]
    r = await client.post(f"/v1/admin/bookings/{bid}/follow-up", json={"service_id": str(spa.service_id), "start_at": at(spa.day, 15).isoformat()}, headers=spa.admin)
    assert r.status_code == 409
    r = await client.patch(f"/v1/admin/bookings/{bid}/actual-duration", json={"minutes": 90}, headers=spa.admin)
    assert r.status_code == 409


# --------------------------------------------------------------------- overruns
async def _in_progress(p: Practice, service: str, hour: int, **extra: Any) -> str:
    bid = (await pbook(p, at(p.day, hour), service, **extra)).json()["id"]
    assert (await status(p, bid, "in-progress")).status_code == 200
    return bid


async def _actual(p: Practice, bid: str, minutes: int) -> httpx.Response:
    return await p.client.patch(f"/v1/admin/bookings/{bid}/actual-duration", json={"minutes": minutes}, headers=p.admin)


async def test_a_free_overrun_stretches_the_reservation(practice: Practice) -> None:
    bid = await _in_progress(practice, "office", 10)  # 10:00-11:00, in office so no travel padding
    r = await _actual(practice, bid, 90)
    assert r.status_code == 200 and r.json()["conflicting_booking_ids"] == []
    b = r.json()["booking"]
    assert b["actual_duration_minutes"] == 90 and b["schedule_conflict"] is False
    assert dt.datetime.fromisoformat(b["end_at"]) == at(practice.day, 11, 30)
    assert [(s, e) for _, _, s, e, _ in await allocations(practice, bid)] == [("10:00", "11:30")]
    assert "11:00" not in hhmm(await pslots(practice, "office"))  # the calendar now shows the longer job


async def test_a_shorter_job_only_records_the_figure(practice: Practice) -> None:
    bid = await _in_progress(practice, "office", 10)
    r = await _actual(practice, bid, 40)
    assert r.status_code == 200 and r.json()["booking"]["actual_duration_minutes"] == 40 and r.json()["conflicting_booking_ids"] == []
    assert [(s, e) for _, _, s, e, _ in await allocations(practice, bid)] == [("10:00", "11:00")]


async def test_an_overrun_into_the_next_appointment_is_flagged_not_forced(practice: Practice) -> None:
    first = await _in_progress(practice, "office", 10)  # 10:00-11:00
    nxt = (await pbook(practice, at(practice.day, 11), "office")).json()["id"]  # 11:00-12:00, right behind it
    r = await _actual(practice, first, 90)
    assert r.status_code == 200 and r.json()["conflicting_booking_ids"] == [nxt]
    assert r.json()["booking"]["schedule_conflict"] is True
    assert [(s, e) for _, _, s, e, _ in await allocations(practice, first)] == [("10:00", "11:00")]  # not stretched into the other job
    assert [(s, e) for _, _, s, e, _ in await allocations(practice, nxt)] == [("11:00", "12:00")]  # and the other job is untouched
    flagged = (await practice.client.get("/v1/admin/bookings", params={"schedule_conflict": "true"}, headers=practice.admin)).json()
    assert {b["id"] for b in flagged} == {first, nxt}
    assert (await practice.client.post(f"/v1/admin/bookings/{first}/resolve-conflict", headers=practice.admin)).json()["schedule_conflict"] is False
    still = (await practice.client.get("/v1/admin/bookings", params={"schedule_conflict": "true"}, headers=practice.admin)).json()
    assert {b["id"] for b in still} == {nxt}


async def test_overrun_rules(practice: Practice) -> None:
    confirmed = (await pbook(practice, at(practice.day, 10), "office")).json()["id"]
    assert (await _actual(practice, confirmed, 90)).status_code == 409  # only a job in progress can overrun
    await status(practice, confirmed, "in-progress")
    assert (await _actual(practice, confirmed, 0)).status_code == 422
    assert (await _actual(practice, confirmed, 90)).status_code == 200


# --------------------------------------------------------------------- concurrency and isolation
@pytest.mark.usefixtures("postgis")
async def test_one_professional_cannot_be_double_booked_including_travel_time(client: httpx.AsyncClient) -> None:
    p = await make_practice(client)
    same = await asyncio.gather(*[pbook(p, at(p.day, 10), "site", address=pin(NEAR)) for _ in range(6)])
    assert sorted(r.status_code for r in same) == [201] + [409] * 5
    q = await make_practice(client)
    a, b = await asyncio.gather(  # 10:00 and 11:00 look apart, but their padded blocks (09:30-11:30 / 10:30-12:30) overlap
        pbook(q, at(q.day, 10), "site", address=pin(NEAR)), pbook(q, at(q.day, 11), "site", address=pin(NEAR))
    )
    assert sorted([a.status_code, b.status_code]) == [201, 409]


@pytest.mark.usefixtures("postgis")
async def test_concurrent_requests_spread_across_professionals_and_never_exceed_them(client: httpx.AsyncClient) -> None:
    p = await make_practice(client, pros=("Ali", "Bala"))
    results = await asyncio.gather(*[pbook(p, at(p.day, 10), "site", address=pin(NEAR)) for _ in range(6)])
    codes = [r.status_code for r in results]
    assert codes.count(201) == 2 and codes.count(409) == 4, codes
    assert len({r.json()["resource_ids"][0] for r in results if r.status_code == 201}) == 2


async def test_business_location_is_per_tenant(client: httpx.AsyncClient) -> None:
    a = await make_practice(client, radius=10)
    b = await make_practice(client, radius=None)
    assert (await client.get("/v1/admin/business-location", headers=a.admin)).json()["service_radius_km"] == 10
    assert (await client.get("/v1/admin/business-location", headers=b.admin)).json()["service_radius_km"] is None


async def test_service_catalog_accepts_only_known_categories_and_locations(practice: Practice) -> None:
    bad = practice.client.post
    for body in ({"name": "x", "duration_minutes": 30, "location_type": "moon"}, {"name": "x", "duration_minutes": 30, "service_category": "weird"},
                 {"name": "x", "duration_minutes": 30, "travel_buffer_minutes": -5}):
        assert (await bad("/v1/admin/services", json=body, headers=practice.admin)).status_code == 422
    listing = (await practice.client.get("/v1/public/services", headers=practice.pk)).json()
    site = next(s for s in listing if s["name"] == "Site inspection")
    assert (site["service_category"], site["location_type"], site["travel_buffer_minutes"]) == ("inspection", "on_site", 30)


@pytest.mark.usefixtures("postgis")
async def test_only_an_inspection_can_have_a_follow_up(practice: Practice) -> None:
    standard = (await pbook(practice, at(practice.day, 10), "work", address=pin(NEAR))).json()["id"]  # standard work booked directly
    await status(practice, standard, "in-progress")
    later = at(practice.day + dt.timedelta(days=1), 10)
    r = await practice.client.post(
        f"/v1/admin/bookings/{standard}/follow-up", json={"service_id": str(practice.svc["work"]), "start_at": later.isoformat()}, headers=practice.admin
    )
    assert r.status_code == 422 and "inspection" in r.json()["detail"]
