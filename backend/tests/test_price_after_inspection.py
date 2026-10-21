"""A service whose price is told after an inspection (2026-10-21, migration 0033): a car nobody knows what is wrong with, a house's wiring.
``price`` is then what is known up front (0, or a starting price), so 0 no longer has to mean free."""
from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import Seed


async def make(seeded: Seed, **body: Any) -> dict[str, Any]:
    r = await seeded.client.post("/v1/admin/services", json={"name": "Wiring check", "duration_minutes": 60, **body}, headers=seeded.admin)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def public(seeded: Seed, sid: str) -> dict[str, Any]:
    r = await seeded.client.get("/v1/public/services", headers=seeded.pk)
    assert r.status_code == 200, r.text
    return next(s for s in r.json() if s["id"] == sid)


async def test_a_fixed_price_unless_told_otherwise(seeded: Seed) -> None:
    svc = await make(seeded, price="0")
    assert svc["price_after_inspection"] is False
    assert (await public(seeded, svc["id"]))["price_after_inspection"] is False


async def test_customers_see_the_price_comes_after_the_inspection(seeded: Seed) -> None:
    svc = await make(seeded, price="0", price_after_inspection=True)
    shown = await public(seeded, svc["id"])
    assert shown["price_after_inspection"] is True
    assert shown["price"] == "0.00"


async def test_a_starting_price_and_switching_it_off(seeded: Seed) -> None:
    svc = await make(seeded, price="60", price_after_inspection=True)
    url = f"/v1/admin/services/{svc['id']}"
    r = await seeded.client.patch(url, json={"price": "80"}, headers=seeded.admin)  # (left out: kept)
    assert r.status_code == 200, r.text
    assert (r.json()["price"], r.json()["price_after_inspection"]) == ("80.00", True)
    r = await seeded.client.patch(url, json={"price_after_inspection": False}, headers=seeded.admin)
    assert r.status_code == 200, r.text
    assert r.json()["price_after_inspection"] is False
    assert (await public(seeded, svc["id"]))["price_after_inspection"] is False


@pytest.mark.parametrize("field", ["price_after_inspection", "active", "price", "name", "duration_minutes", "travel_buffer_minutes"])
async def test_a_required_setting_cannot_be_emptied(seeded: Seed, field: str) -> None:
    svc = await make(seeded)
    r = await seeded.client.patch(f"/v1/admin/services/{svc['id']}", json={field: None}, headers=seeded.admin)
    assert r.status_code == 422, r.text
    assert field in r.text


async def test_the_optional_settings_can_still_be_emptied(seeded: Seed) -> None:
    svc = await make(seeded, description="Every socket tested", service_category="inspection")
    r = await seeded.client.patch(f"/v1/admin/services/{svc['id']}", json={"description": None, "service_category": None}, headers=seeded.admin)
    assert r.status_code == 200, r.text
    assert (r.json()["description"], r.json()["service_category"]) == (None, None)


async def test_only_managers_change_it(seeded: Seed) -> None:
    svc = await make(seeded)
    staff = await seeded.token("staff")
    r = await seeded.client.patch(f"/v1/admin/services/{svc['id']}", json={"price_after_inspection": True}, headers=staff)
    assert r.status_code == 403
