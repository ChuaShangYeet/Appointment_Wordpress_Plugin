"""Updating resources and deleting days off: upcoming bookings are flagged when a change no longer suits them (never
cancelled), and the flags are lifted when the change is undone."""
from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core import availability_cache
from app.core import redis as redis_module
from app.core.config import get_settings
from app.services import flagging
from tests.conftest import Seed, allocations, at, make_seed
from tests.test_flagging import HEADER, _ctx, _table_booking, book, day_off, flags, get
from tests.test_restaurant import make_restaurant
from tests.test_restaurant import book as rbook

LIFTED = "X-Flags-Lifted"
D = dt.date(2030, 1, 7)


async def patch(s: Seed, rid: uuid.UUID | str, headers: dict[str, str] | None = None, **body: Any) -> httpx.Response:
    return await s.client.patch(f"/v1/admin/resources/{rid}", json=body, headers=headers or s.admin)


async def party_at(rest: Seed, party: int, start: dt.datetime, **extra: Any) -> tuple[str, list[str]]:
    r = await rbook(rest, start, party, **extra)
    assert r.status_code == 201, r.text
    return str(r.json()["id"]), [str(x) for x in r.json()["resource_ids"]]


@pytest.fixture
async def cache(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    import fakeredis.aioredis

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis", fake)
    monkeypatch.setattr(get_settings(), "availability_cache_ttl_seconds", 30)
    availability_cache.reset_breaker()
    yield fake
    availability_cache.reset_breaker()


# --------------------------------------------------------------------- pure rules
def _table(capacity: int, *, vip: bool = False, status: str = "active") -> Any:
    return SimpleNamespace(type="table", capacity=capacity, status=status, attributes={"is_vip": vip} if vip else {})


def test_a_table_no_longer_seats_the_party_only_when_the_seats_add_up_to_less() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    ctx = _ctx(resources={a: _table(4), b: _table(2)})
    single = _table_booking([a], at(D, 19), at(D, 21))
    single.party_size = 4
    assert flagging.table_too_small(ctx, single) is False  # exactly enough
    single.party_size = 5
    assert flagging.table_too_small(ctx, single) is True
    combined = _table_booking([a, b], single.start_at, single.end_at)
    combined.party_size = 6
    assert flagging.table_too_small(ctx, combined) is False  # 4 + 2 seats
    combined.party_size = 7
    assert flagging.table_too_small(ctx, combined) is True
    combined.party_size = None
    assert flagging.table_too_small(ctx, combined) is False  # no party recorded: nothing to compare


def test_a_table_is_in_the_wrong_class_only_when_it_disagrees_with_the_booking() -> None:
    a = uuid.uuid4()
    booking = _table_booking([a], at(D, 19), at(D, 21), table_type="normal")
    assert flagging.table_class_changed(_ctx(resources={a: _table(4)}), booking) is False
    assert flagging.table_class_changed(_ctx(resources={a: _table(4, vip=True)}), booking) is True
    vip = _table_booking([a], at(D, 19), at(D, 21), table_type="vip")
    assert flagging.table_class_changed(_ctx(resources={a: _table(4, vip=True)}), vip) is False
    booking.table_type = None
    assert flagging.table_class_changed(_ctx(resources={a: _table(4, vip=True)}), booking) is False


def test_an_inactive_resource_flags_only_reservations_still_ahead() -> None:
    a = uuid.uuid4()
    booking = _table_booking([a], at(D, 19), at(D, 21))
    assert flagging.resource_inactive(_ctx(resources={a: _table(4)}), booking) is False
    assert flagging.resource_inactive(_ctx(resources={a: _table(4, status="inactive")}), booking) is True
    later = _ctx(resources={a: _table(4, status="inactive")})
    later.now = at(D, 23)  # the seating is over
    assert flagging.resource_inactive(later, booking) is False


# --------------------------------------------------------------------- deactivating staff
async def test_deactivating_staff_flags_their_upcoming_bookings_without_cancelling_and_reactivating_lifts_them(client: httpx.AsyncClient) -> None:
    s = await make_seed(client)
    mei, lin = await s.add_staff("Mei"), await s.add_staff("Lin")
    hers, his = await book(s, mei, at(s.day, 10)), await book(s, lin, at(s.day, 10))
    r = await patch(s, mei, status="inactive")
    assert r.status_code == 200 and r.json()["status"] == "inactive"
    assert (r.headers[HEADER], r.headers[LIFTED]) == ("1", "0")
    assert await flags(s, hers) == ["resource_inactive"] and await flags(s, his) == []
    d = await get(s, hers)
    assert d["status"] == "confirmed" and [row[4] for row in await allocations(s, hers)] == ["active"]  # flagged, not touched
    offered = await s.client.get("/v1/public/availability", params={"date": s.day.isoformat(), "service_ids": str(s.service_id)}, headers=s.pk)
    assert offered.json()["slots"] and all(str(mei) not in x["resource_ids"] for x in offered.json()["slots"])  # no new bookings for Mei
    assert str(mei) not in [x["id"] for x in (await s.client.get("/v1/public/resources", headers=s.pk)).json()]
    r = await patch(s, mei, status="active")
    assert (r.headers[HEADER], r.headers[LIFTED]) == ("0", "1") and await flags(s, hers) == []


async def test_a_change_only_rechecks_that_resources_bookings(client: httpx.AsyncClient) -> None:
    s = await make_seed(client)
    mei, lin = await s.add_staff("Mei"), await s.add_staff("Lin")
    hers = await book(s, mei, at(s.day, 10))
    await patch(s, mei, status="inactive")
    await s.client.post(f"/v1/admin/bookings/{hers}/clear-flags", headers=s.admin)  # a manager accepted it
    r = await patch(s, lin, status="inactive")  # Mei is still inactive, but this is Lin's change
    assert r.headers[HEADER] == "0" and await flags(s, hers) == []


async def test_changes_that_cannot_affect_bookings_do_not_recheck_anything(client: httpx.AsyncClient) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    await book(s, mei, at(s.day, 10))
    r = await patch(s, mei, name="Mei Ling")
    assert r.status_code == 200 and r.json()["name"] == "Mei Ling" and HEADER not in r.headers
    assert (await patch(s, mei)).status_code == 200  # nothing to change is not an error


async def test_resource_updates_are_validated_and_authorised(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    other = await make_seed(client)
    foreign = await other.add_staff("Someone")
    table = uuid.UUID((await rest.client.get("/v1/admin/resources", headers=rest.admin)).json()[0]["id"])
    assert (await patch(rest, table, type="staff")).status_code == 422  # a resource's type is fixed
    assert (await patch(rest, table, name=None)).status_code == 422
    assert (await patch(rest, table, capacity=0)).status_code == 422
    assert (await patch(rest, table, status="broken")).status_code == 422
    assert (await patch(rest, table, attributes={"is_vip": "yes"})).status_code == 422  # tables keep the create-time rules
    assert (await patch(rest, table, attributes={"minimum_spend": 100})).status_code == 422  # only VIP tables carry one
    assert (await patch(rest, table, attributes={"location": "roof"})).status_code == 422
    assert (await patch(rest, uuid.uuid4(), name="x")).status_code == 404
    assert (await patch(rest, foreign, name="x")).status_code == 404  # another tenant's resource does not exist here
    staff_token = await rest.token("staff")
    assert (await patch(rest, table, headers=staff_token, name="x")).status_code == 403
    manager = await rest.token("manager")
    assert (await patch(rest, table, headers=manager, name="Window")).status_code == 200


# --------------------------------------------------------------------- restaurant tables
async def test_a_table_taken_out_of_service_flags_its_reservation_only(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    first, (t1,) = await party_at(rest, 2, at(rest.day, 19))
    second, (t2,) = await party_at(rest, 2, at(rest.day, 19))
    r = await patch(rest, t2, status="inactive")
    assert r.headers[HEADER] == "1"
    assert await flags(rest, second) == ["resource_inactive"] and await flags(rest, first) == []
    assert t1 != t2


async def test_shrinking_a_table_flags_parties_it_no_longer_seats_and_growing_it_lifts_the_flag(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    four, (c4,) = await party_at(rest, 4, at(rest.day, 19))
    two, (a2,) = await party_at(rest, 2, at(rest.day, 19))
    r = await patch(rest, c4, capacity=3)
    assert r.headers[HEADER] == "1" and await flags(rest, four) == ["table_too_small"] and await flags(rest, two) == []
    assert (await patch(rest, c4, capacity=4)).headers[LIFTED] == "1" and await flags(rest, four) == []
    assert (await patch(rest, a2, capacity=1)).headers[HEADER] == "1"  # a party of 2 at a table now seating 1
    assert await flags(rest, two) == ["table_too_small"]


async def test_a_combined_party_is_judged_on_all_its_tables_together(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    party, tables = await party_at(rest, 8, at(rest.day, 19), allow_separate_seating=True)
    assert len(tables) == 2
    caps = {str(k): v for k, v in {rest.tables["a2"]: 2, rest.tables["b2"]: 2, rest.tables["c4"]: 4, rest.tables["d6"]: 6}.items()}
    biggest = max(tables, key=lambda t: caps[t])
    assert (await patch(rest, biggest, capacity=caps[biggest] - 1)).headers[HEADER] == "1"  # 8 seats became 7
    assert await flags(rest, party) == ["table_too_small"]
    assert (await patch(rest, biggest, capacity=caps[biggest])).headers[LIFTED] == "1" and await flags(rest, party) == []


async def test_moving_a_table_between_normal_and_vip_flags_bookings_of_the_old_class(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    bid, (c4,) = await party_at(rest, 4, at(rest.day, 19))
    r = await patch(rest, c4, attributes={"location": "indoor", "is_vip": True, "minimum_spend": 300})
    assert r.headers[HEADER] == "1" and await flags(rest, bid) == ["table_class_changed"]
    assert (await patch(rest, c4, attributes={"location": "indoor"})).headers[LIFTED] == "1" and await flags(rest, bid) == []
    assert (await patch(rest, c4, attributes={"location": "outdoor"})).headers[HEADER] == "0"  # same class: nothing to flag


# --------------------------------------------------------------------- deleting days off
async def test_deleting_a_day_off_lifts_the_flags_it_caused(client: httpx.AsyncClient) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, at(s.day, 10))
    off = (await day_off(s, s.day, mei)).json()["id"]
    assert await flags(s, bid) == ["day_off"]
    r = await s.client.delete(f"/v1/admin/days-off/{off}", headers=s.admin)
    assert r.status_code == 204 and (r.headers[HEADER], r.headers[LIFTED]) == ("0", "1")
    assert await flags(s, bid) == []
    assert off not in [d["id"] for d in (await s.client.get("/v1/admin/days-off", headers=s.admin)).json()]
    assert (await s.client.delete(f"/v1/admin/days-off/{off}", headers=s.admin)).status_code == 404  # gone


async def test_a_booking_stays_flagged_while_another_day_off_still_covers_it(client: httpx.AsyncClient) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")
    bid = await book(s, mei, at(s.day, 10))
    leave = (await day_off(s, s.day, mei)).json()["id"]
    holiday = (await day_off(s, s.day)).json()["id"]
    r = await s.client.delete(f"/v1/admin/days-off/{leave}", headers=s.admin)
    assert r.headers[LIFTED] == "0" and await flags(s, bid) == ["day_off"]  # the company holiday still applies
    r = await s.client.delete(f"/v1/admin/days-off/{holiday}", headers=s.admin)
    assert r.headers[LIFTED] == "1" and await flags(s, bid) == []


async def test_deleting_a_day_off_is_authorised_and_tenant_scoped(client: httpx.AsyncClient) -> None:
    ours, theirs = await make_seed(client), await make_seed(client)
    off = (await day_off(ours, ours.day)).json()["id"]
    assert (await ours.client.delete(f"/v1/admin/days-off/{off}", headers=await ours.token("staff"))).status_code == 403
    assert (await theirs.client.delete(f"/v1/admin/days-off/{off}", headers=theirs.admin)).status_code == 404
    assert (await ours.client.delete(f"/v1/admin/days-off/{uuid.uuid4()}", headers=ours.admin)).status_code == 404
    assert off in [d["id"] for d in (await ours.client.get("/v1/admin/days-off", headers=ours.admin)).json()]  # nothing was removed
    assert (await ours.client.delete(f"/v1/admin/days-off/{off}", headers=await ours.token("manager"))).status_code == 204


# --------------------------------------------------------------------- the availability cache follows both
async def test_both_changes_reach_the_cached_availability(client: httpx.AsyncClient, cache: Any) -> None:
    s = await make_seed(client)
    mei = await s.add_staff("Mei")

    async def slots() -> tuple[str, int]:
        r = await s.client.get("/v1/public/availability", params={"date": s.day.isoformat(), "service_ids": str(s.service_id)}, headers=s.pk)
        return r.headers["X-Cache"], len(r.json()["slots"])

    _, open_ = await slots()
    assert open_ > 0 and (await slots())[0] == "HIT"
    await patch(s, mei, status="inactive")
    assert await slots() == ("MISS", 0)  # deactivated: nothing to offer, straight away
    await patch(s, mei, status="active")
    assert await slots() == ("MISS", open_)
    off = (await day_off(s, s.day, mei)).json()["id"]
    assert await slots() == ("MISS", 0)
    assert (await slots())[0] == "HIT"
    await s.client.delete(f"/v1/admin/days-off/{off}", headers=s.admin)
    assert await slots() == ("MISS", open_)  # the holiday is gone and its slots are back
