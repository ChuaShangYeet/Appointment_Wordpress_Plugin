"""The policy's minimum notice and booking window decide which slots the public availability list offers (every vertical)."""
from __future__ import annotations

import datetime as dt
from typing import Any

import httpx

from tests.conftest import TZ, Seed
from tests.test_automotive import gslots, make_garage
from tests.test_customer_cancel import set_default_policy
from tests.test_field_service import make_practice, pslots
from tests.test_restaurant import make_restaurant, slots


def starts(slot_list: list[dict[str, Any]]) -> list[str]:
    return [dt.datetime.fromisoformat(s["start_at"]).astimezone(TZ).strftime("%H:%M") for s in slot_list]


async def staff_slots(s: Seed) -> list[str]:
    r = await s.client.get("/v1/public/availability", params={"date": s.day.isoformat(), "service_ids": str(s.service_id)}, headers=s.pk)
    assert r.status_code == 200, r.text
    return starts(r.json()["slots"])


async def test_staff_slots_follow_the_notice_and_the_window(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    everything = await staff_slots(seeded)
    assert everything and everything[0] == "09:00"
    await set_default_policy(seeded, booking_window=86400)  # one day ahead: the day three days out is not offered
    assert await staff_slots(seeded) == []
    await set_default_policy(seeded, min_notice_booking=10 * 86400)  # ten days of notice
    assert await staff_slots(seeded) == []


async def test_staff_slots_are_cut_at_the_window_end_and_the_notice_end(seeded: Seed) -> None:
    await seeded.add_staff("Mei")
    now = dt.datetime.now(dt.UTC)
    day = seeded.day
    cut = dt.datetime.combine(day, dt.time(12, 30), tzinfo=TZ)
    await set_default_policy(seeded, booking_window=int((cut - now).total_seconds()))
    early = await staff_slots(seeded)
    assert early and max(early) <= "12:30" and "12:00" in early  # the morning is offered, the afternoon is beyond the window
    first = dt.datetime.combine(day, dt.time(14, 30), tzinfo=TZ)
    await set_default_policy(seeded, min_notice_booking=int((first - now).total_seconds()))
    late = await staff_slots(seeded)
    assert late and min(late) >= "14:30" and "17:00" in late  # nothing earlier than the notice allows


async def test_restaurant_slots_follow_the_notice_and_the_window(client: httpx.AsyncClient) -> None:
    rest = await make_restaurant(client)
    assert await slots(rest, 2)
    await set_default_policy(rest, booking_window=86400)
    assert await slots(rest, 2) == []
    await set_default_policy(rest, min_notice_booking=10 * 86400)
    assert await slots(rest, 2) == []


async def test_workshop_slots_follow_the_notice_and_the_window(client: httpx.AsyncClient) -> None:
    garage = await make_garage(client)
    assert await gslots(garage, ["oil"])
    await set_default_policy(garage, booking_window=86400)
    assert await gslots(garage, ["oil"]) == []
    await set_default_policy(garage, min_notice_booking=10 * 86400)
    assert await gslots(garage, ["oil"]) == []


async def test_field_service_slots_follow_the_notice_and_the_window(client: httpx.AsyncClient) -> None:
    practice = await make_practice(client)
    assert (await pslots(practice, "office")).json()["slots"]
    await set_default_policy(practice, booking_window=86400)
    assert (await pslots(practice, "office")).json()["slots"] == []
    await set_default_policy(practice, min_notice_booking=10 * 86400)
    assert (await pslots(practice, "office")).json()["slots"] == []
