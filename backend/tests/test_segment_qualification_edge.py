"""Segment-by-segment qualification: the edges found by mutation testing."""
from __future__ import annotations

import datetime as dt
import uuid

import httpx
from sqlalchemy import text

from app.core.database import tenant_session
from tests.conftest import Seed, at
from tests.test_automotive import add_service as add_garage_service, gbook, make_garage
from tests.test_flagging import HEADER, flags
from tests.test_segment_qualification import patch_service


async def test_only_the_booking_whose_resource_lost_the_service_is_flagged(seeded: Seed) -> None:
    mei, lin = await seeded.add_staff("Mei"), await seeded.add_staff("Lin")
    facial = uuid.UUID((await seeded.client.post("/v1/admin/services", json={"name": "Facial", "duration_minutes": 30, "price": "80.00"}, headers=seeded.admin)).json()["id"])
    both = [str(seeded.service_id), str(facial)]
    with_mei = (await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 10), mei, service_ids=both), headers=seeded.pk)).json()["id"]
    with_lin = (await seeded.client.post("/v1/public/bookings", json=seeded.booking_body(at(seeded.day, 15), lin, service_ids=both), headers=seeded.pk)).json()["id"]
    r = await patch_service(seeded, facial, lin)  # Mei loses the facial; Lin keeps it
    assert r.headers[HEADER] == "1"
    assert await flags(seeded, with_mei) == ["staff_not_qualified"] and await flags(seeded, with_lin) == []


async def test_a_segment_whose_service_is_an_add_on_is_still_judged_by_that_service(client: httpx.AsyncClient) -> None:
    """A workshop service marked as an add-on is not part of the "base" services, but its segment has a mechanic and a bay all the same."""
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    wash = uuid.UUID((await client.post("/v1/admin/services", json={"name": "Wash", "duration_minutes": 30, "is_addon": True}, headers=g.admin)).json()["id"])
    g.svc["wash"] = wash
    bid = (await gbook(g, at(g.day, 9), ["oil", "wash"])).json()["id"]
    r = await patch_service(g, g.svc["oil"], g.mechanics["Ali"], g.mechanics["Bala"])  # a recheck of this job; nobody lost anything
    assert r.headers[HEADER] == "0" and await flags(g, bid) == []
    segs = {(t, n) for t, n in await _segment_resources(g, bid, "Wash")}
    assert ("staff", "Ali") in segs
    r = await patch_service(g, wash, g.mechanics["Bala"])  # Ali loses the wash he holds
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]
    assert add_garage_service


async def _segment_resources(g: Seed, bid: str, service: str) -> list[tuple[str, str]]:
    async with tenant_session(g.tenant_id) as db:
        rows = (await db.execute(text(
            "SELECT r.type, r.name FROM booking_allocations a JOIN resources r ON r.id = a.resource_id JOIN services s ON s.id = a.service_id "
            "WHERE a.booking_id = :b AND s.name = :n"), {"b": uuid.UUID(bid), "n": service})).all()
    return [(t, n) for t, n in rows]


async def test_a_segment_that_is_already_over_is_history_and_is_not_judged_again(client: httpx.AsyncClient) -> None:
    g = await make_garage(client, mechanics=("Ali", "Bala"), bays=("Bay 1",))
    ali, bala = g.mechanics["Ali"], g.mechanics["Bala"]
    await patch_service(g, g.svc["oil"], ali)
    await patch_service(g, g.svc["brake"], ali, bala)
    bid = (await gbook(g, at(g.day, 9), ["oil", "brake"])).json()["id"]
    async with tenant_session(g.tenant_id) as db:  # the oil change was done days ago (the job was interrupted and resumed later)
        past = dt.datetime.now(dt.UTC) - dt.timedelta(days=3)
        await db.execute(text(
            "UPDATE booking_allocations a SET period = tstzrange(:s, :e) FROM services s WHERE s.id = a.service_id AND s.name = 'Oil change' AND a.booking_id = :b"),
            {"s": past, "e": past + dt.timedelta(hours=1), "b": uuid.UUID(bid)})
    r = await patch_service(g, g.svc["oil"], bala)  # Ali no longer does oil changes, but the one he did is done
    assert r.headers[HEADER] == "0" and await flags(g, bid) == []
    r = await patch_service(g, g.svc["brake"], bala)  # the brake segment still ahead is another matter
    assert r.headers[HEADER] == "1" and await flags(g, bid) == ["staff_not_qualified"]
