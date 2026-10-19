"""Review flags judge qualification the way the booking was made: every service for a single resource, each segment for a workshop job order."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
from sqlalchemy import text

from app.core.database import tenant_session
from app.models import Booking, BookingAllocation
from app.services import flagging
from tests.conftest import Seed, at, make_seed
from tests.test_automotive import Garage, add_service as add_garage_service, gbook, make_garage
from tests.test_customer_cancel import PHONE, login, otp  # noqa: F401  (``otp`` is a fixture)
from tests.test_customer_reschedule import move
from tests.test_flagging import HEADER, flags
from tests.test_salon import make_salon, sbook

HEADER_LIFTED = "X-Flags-Lifted"


async def patch_service(s: Seed, service_id: uuid.UUID | str, *resource_ids: uuid.UUID) -> httpx.Response:
    r = await s.client.patch(f"/v1/admin/services/{service_id}", json={"resource_ids": [str(i) for i in resource_ids]}, headers=s.admin)
    assert r.status_code == 200, r.text
    return r


async def segments(s: Seed, bid: str) -> list[tuple[str, str, str | None, str]]:
    """``(resource type, resource name, service name, state)`` per reservation, in time order."""
    async with tenant_session(s.tenant_id) as db:
        rows = (await db.execute(text(
            "SELECT r.type, r.name, sv.name, a.state FROM booking_allocations a JOIN resources r ON r.id = a.resource_id "
            "LEFT JOIN services sv ON sv.id = a.service_id WHERE a.booking_id = :b ORDER BY lower(a.period), r.type"
        ), {"b": uuid.UUID(bid)})).all()
    return [(t, n, sv, st) for t, n, sv, st in rows]


# --------------------------------------------------------------------- one resource, several services: it must do all of them
async def test_a_staff_member_booked_for_several_services_must_still_do_every_one(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    facial = uuid.UUID((await seeded.client.post("/v1/admin/services", json={"name": "Facial", "duration_minutes": 30, "price": "80.00"}, headers=seeded.admin)).json()["id"])
    body = seeded.booking_body(at(seeded.day, 10), mei, service_ids=[str(seeded.service_id), str(facial)])
    bid = (await seeded.client.post("/v1/public/bookings", json=body, headers=seeded.pk)).json()["id"]
    assert await flags(seeded, bid) == []
    r = await patch_service(seeded, facial, lin)  # Mei lost the facial but can still give the massage: the old rule let that pass
    assert r.headers[HEADER] == "1" and await flags(seeded, bid) == ["staff_not_qualified"]
    r = await patch_service(seeded, facial, mei, lin)
    assert r.headers[HEADER] == "0" and r.headers[HEADER_LIFTED] == "1" and await flags(seeded, bid) == []
    r = await patch_service(seeded, seeded.service_id, lin)  # ... or the massage but not the facial
    assert r.headers[HEADER] == "1" and await flags(seeded, bid) == ["staff_not_qualified"]
    await patch_service(seeded, seeded.service_id)  # no links: everybody qualifies again
    assert await flags(seeded, bid) == []


async def test_someone_qualified_for_all_of_the_services_is_left_alone_however_they_are_listed(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    facial = uuid.UUID((await seeded.client.post("/v1/admin/services", json={"name": "Facial", "duration_minutes": 30, "price": "80.00"}, headers=seeded.admin)).json()["id"])
    body = seeded.booking_body(at(seeded.day, 10), mei, service_ids=[str(seeded.service_id), str(facial)])
    bid = (await seeded.client.post("/v1/public/bookings", json=body, headers=seeded.pk)).json()["id"]
    await patch_service(seeded, seeded.service_id, mei)
    r = await patch_service(seeded, facial, mei, lin)
    assert r.headers[HEADER] == "0" and await flags(seeded, bid) == []
    other = (await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 15), lin, service_ids=[str(facial)]), headers=seeded.pk)).json()["id"]
    r = await patch_service(seeded, seeded.service_id, lin)  # Mei loses the massage: only the job that includes it is affected
    assert await flags(seeded, bid) == ["staff_not_qualified"] and await flags(seeded, other) == []


async def test_a_salon_cart_is_still_judged_by_its_base_service_alone(client: httpx.AsyncClient) -> None:
    s = await make_salon(client)
    mei, lin = s.stylists["Mei"], s.stylists["Lin"]
    bid = (await sbook(s, at(s.day, 10), ["cut", "gel", "treat"], stylist="Mei")).json()["id"]
    r = await patch_service(s, s.svc["gel"], lin)
    r = await patch_service(s, s.svc["treat"], lin)
    assert r.headers[HEADER] == "0" and await flags(s, bid) == []  # Mei no longer does either add-on, and that is fine
    r = await patch_service(s, s.svc["cut"], lin)
    assert r.headers[HEADER] == "1" and await flags(s, bid) == ["staff_not_qualified"]
    assert mei


# --------------------------------------------------------------------- a workshop job order: each segment on its own
async def test_every_segment_of_a_job_order_records_the_service_it_is_for(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali",), bays=("Bay 1",))
    bid = (await gbook(g, at(g.day, 9), ["brake", "oil"])).json()["id"]
    assert await segments(g, bid) == [
        ("bay", "Bay 1", "Brake service", "active"), ("staff", "Ali", "Brake service", "active"),
        ("bay", "Bay 1", "Oil change", "active"), ("staff", "Ali", "Oil change", "active"),
    ]
    spa = await make_seed(client)
    staff = await spa.add_staff("Mei")
    sid = (await client.post("/v1/public/bookings", json=spa.booking_body(at(spa.day, 10), staff), headers=spa.pk)).json()["id"]
    assert await segments(spa, sid) == [("staff", "Mei", None, "active")]  # one resource for the whole booking: no single service


async def test_a_mechanic_who_lost_one_segments_service_is_flagged_though_he_can_still_do_the_other(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    ali, bala = g.mechanics["Ali"], g.mechanics["Bala"]
    await patch_service(g, g.svc["brake"], ali)
    await patch_service(g, g.svc["oil"], ali, bala)
    bid = (await gbook(g, at(g.day, 9), ["brake", "oil"])).json()["id"]
    assert [(n, sv) for t, n, sv, _ in await segments(g, bid) if t == "staff"] == [("Ali", "Brake service"), ("Ali", "Oil change")]
    assert await flags(g, bid) == []
    r = await patch_service(g, g.svc["brake"], bala)  # Ali loses the brake service; his oil change is still fine, which the old rule took as enough
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]
    r = await patch_service(g, g.svc["brake"], ali, bala)
    assert r.headers[HEADER] == "0" and await flags(g, bid) == []


async def test_each_segment_is_judged_against_its_own_mechanic(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    ali, bala = g.mechanics["Ali"], g.mechanics["Bala"]
    await patch_service(g, g.svc["brake"], ali)
    await patch_service(g, g.svc["oil"], bala)  # a different person for each segment
    bid = (await gbook(g, at(g.day, 9), ["brake", "oil"])).json()["id"]
    assert [(n, sv) for t, n, sv, _ in await segments(g, bid) if t == "staff"] == [("Ali", "Brake service"), ("Bala", "Oil change")]
    assert await flags(g, bid) == []
    r = await patch_service(g, g.svc["oil"], ali)  # Bala loses the oil change: his segment
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]
    await patch_service(g, g.svc["oil"], ali, bala)
    assert await flags(g, bid) == []
    r = await patch_service(g, g.svc["brake"], bala)  # Ali loses the brake service: the other segment
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]


async def test_each_segment_is_judged_against_its_own_bay(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali",), bays=("Bay 1", "Bay 2"))
    bay1, bay2 = g.bays["Bay 1"], g.bays["Bay 2"]
    await patch_service(g, g.svc["oil"], bay1)
    await patch_service(g, g.svc["brake"], bay1, bay2)
    bid = (await gbook(g, at(g.day, 9), ["oil", "brake"])).json()["id"]
    assert {(n, sv) for t, n, sv, _ in await segments(g, bid) if t == "bay"} == {("Bay 1", "Oil change"), ("Bay 1", "Brake service")}
    r = await patch_service(g, g.svc["brake"], bay2)  # Bay 1 can still do the oil change but not the brakes it is holding
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]
    r = await patch_service(g, g.svc["brake"], bay1, bay2)
    assert r.headers[HEADER] == "0" and await flags(g, bid) == []


async def test_changing_a_service_the_job_order_does_not_include_flags_nothing(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    tyres = await add_garage_service(g, "Tyres", 60)
    bid = (await gbook(g, at(g.day, 9), ["oil"])).json()["id"]
    r = await patch_service(g, tyres, g.mechanics["Bala"])
    assert r.headers[HEADER] == "0" and await flags(g, bid) == []


async def test_a_resumed_job_judges_the_remaining_segments_by_their_services(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    ali, bala = g.mechanics["Ali"], g.mechanics["Bala"]
    await patch_service(g, g.svc["brake"], ali)
    await patch_service(g, g.svc["oil"], ali, bala)
    bid = (await gbook(g, at(g.day, 9), ["oil", "brake"])).json()["id"]
    for status in ("in-progress", "waiting-for-parts"):
        assert (await g.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": status}, headers=g.admin)).status_code == 200
    later = at(g.day + dt.timedelta(days=2), 10)
    resumed = await g.client.post(f"/v1/admin/bookings/{bid}/resume", json={"start_at": later.isoformat(), "service_ids": [str(g.svc["brake"])]}, headers=g.admin)
    assert resumed.status_code == 200, resumed.text
    live = [(t, n, sv) for t, n, sv, st in await segments(g, bid) if st == "active"]
    assert sorted(live) == [("bay", "Bay 1", "Brake service"), ("staff", "Ali", "Brake service")]
    assert await flags(g, bid) == []
    r = await patch_service(g, g.svc["brake"], bala)
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]


async def test_a_customer_move_keeps_the_segments_services(client: httpx.AsyncClient, otp: Any) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    await patch_service(g, g.svc["brake"], g.mechanics["Ali"])
    await patch_service(g, g.svc["oil"], g.mechanics["Ali"], g.mechanics["Bala"])
    bid = (await gbook(g, at(g.day, 9), ["brake", "oil"], customer={"name": "Aisha", "phone": PHONE})).json()["id"]
    moved = await move(g, await login(g), bid, at(g.day, 14))
    assert moved.status_code == 200, moved.text
    live = [(n, sv) for t, n, sv, st in await segments(g, bid) if st == "active" and t == "staff"]
    assert live == [("Ali", "Brake service"), ("Ali", "Oil change")]
    r = await patch_service(g, g.svc["brake"], g.mechanics["Bala"])
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]


# --------------------------------------------------------------------- bookings from before the column existed
async def test_an_old_multi_service_job_order_without_segment_information_keeps_the_lenient_rule(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    await patch_service(g, g.svc["brake"], g.mechanics["Ali"])
    bid = (await gbook(g, at(g.day, 9), ["brake", "oil"])).json()["id"]
    async with tenant_session(g.tenant_id) as db:  # as if it had been made before segments were recorded
        await db.execute(text("UPDATE booking_allocations SET service_id = NULL WHERE booking_id = :b"), {"b": uuid.UUID(bid)})
    r = await patch_service(g, g.svc["brake"], g.mechanics["Bala"])  # Ali lost the brakes but can still do the oil: not provable, not flagged
    assert r.headers[HEADER] == "0" and await flags(g, bid) == []
    await patch_service(g, g.svc["brake"], g.mechanics["Bala"])
    r = await patch_service(g, g.svc["oil"], g.mechanics["Bala"])  # now Ali can do neither of the job's services
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]


async def test_an_old_single_service_job_order_is_judged_strictly(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    await patch_service(g, g.svc["oil"], g.mechanics["Ali"])
    bid = (await gbook(g, at(g.day, 9), ["oil"])).json()["id"]
    async with tenant_session(g.tenant_id) as db:
        await db.execute(text("UPDATE booking_allocations SET service_id = NULL WHERE booking_id = :b"), {"b": uuid.UUID(bid)})
    r = await patch_service(g, g.svc["oil"], g.mechanics["Bala"])
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]


# --------------------------------------------------------------------- the rule itself
def test_a_reservation_remembers_its_service_in_the_flagging_view() -> None:
    service, start, end = uuid.uuid4(), at(dt.date(2030, 1, 7), 9), at(dt.date(2030, 1, 7), 10)
    from sqlalchemy.dialects.postgresql import Range

    booking = Booking(id=uuid.uuid4(), start_at=start, end_at=end, status="confirmed", allocations=[
        BookingAllocation(resource_id=uuid.uuid4(), service_id=service, period=Range(start, end, bounds="[)"), state="active"),
        BookingAllocation(resource_id=uuid.uuid4(), service_id=None, period=Range(start, end, bounds="[)"), state="active"),
        BookingAllocation(resource_id=uuid.uuid4(), service_id=service, period=Range(start, end, bounds="[)"), state="released"),
    ])
    spans = flagging._spans(booking, start - dt.timedelta(days=1))
    assert [sp.service_id for sp in spans] == [service, None]
    assert flagging.Span(uuid.uuid4(), start, end).service_id is None


def test_the_workshop_allocator_names_the_service_of_each_segment() -> None:
    from app.services.allocators.base import Assignment

    assert Assignment(uuid.uuid4(), at(dt.date(2030, 1, 7), 9), at(dt.date(2030, 1, 7), 10)).service_id is None
    sid = uuid.uuid4()
    assert Assignment(uuid.uuid4(), at(dt.date(2030, 1, 7), 9), at(dt.date(2030, 1, 7), 10), service_id=sid).service_id == sid
