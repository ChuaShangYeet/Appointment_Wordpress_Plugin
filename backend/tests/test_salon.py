"""Salon / barber: add-ons, strictest policy, on-the-spot upsell, inventory deduction (blueprint §9)."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.core.exceptions import ValidationFailed
from app.models import BookingPolicy
from app.services import booking as booking_module
from app.services.allocators.salon import split_cart
from app.services.availability import merge_policies
from tests.conftest import PLATFORM, TZ, Seed, allocations, at, make_seed


# --------------------------------------------------------------------- pure logic
def _policy(name: str, *, notice: int, cancel: int, window: int, status: str = "confirmed", timeout: int = 30) -> BookingPolicy:
    return BookingPolicy(
        id=uuid.uuid4(), tenant_id=uuid.UUID(int=1), name=name, default_status=status,
        min_notice_booking=dt.timedelta(hours=notice), min_notice_cancel=dt.timedelta(hours=cancel),
        booking_window=dt.timedelta(days=window), pending_timeout=dt.timedelta(minutes=timeout),
    )


def test_a_single_policy_is_returned_untouched_and_duplicates_collapse() -> None:
    p = _policy("A", notice=2, cancel=24, window=60)
    assert merge_policies([p]) is p
    assert merge_policies([p, p]) is p


def test_strictest_policy_wins() -> None:
    a = _policy("A", notice=2, cancel=48, window=60, timeout=30)
    b = _policy("B", notice=6, cancel=12, window=30, status="pending", timeout=10)
    m = merge_policies([a, b])
    assert m.min_notice_booking == dt.timedelta(hours=6)  # longest booking notice
    assert m.min_notice_cancel == dt.timedelta(hours=48)  # longest cancellation notice
    assert m.booking_window == dt.timedelta(days=30)  # shortest booking window
    assert m.default_status == "pending" and m.pending_timeout == dt.timedelta(minutes=10)
    assert sorted(m.merged_from) == sorted([str(a.id), str(b.id)])  # type: ignore[attr-defined]
    assert merge_policies([b, a]).min_notice_booking == m.min_notice_booking  # order does not matter


def _svc(addon: bool) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(id=uuid.uuid4(), is_addon=addon, name="s")


def test_a_cart_has_exactly_one_base_service() -> None:
    base, extra = _svc(False), _svc(True)
    assert split_cart([base, extra, _svc(True)])[0] is base and len(split_cart([extra, base])[1]) == 1
    for bad in ([extra], [base, _svc(False)], []):
        with pytest.raises(ValidationFailed):
            split_cart(bad)


# --------------------------------------------------------------------- fixtures
class Salon(Seed):
    stylists: dict[str, uuid.UUID]
    svc: dict[str, uuid.UUID]
    products: dict[str, uuid.UUID]


async def _service(s: Seed, name: str, minutes: int, *, addon: bool = False, buffer: int = 0, policy: str | None = None) -> uuid.UUID:
    body: dict[str, Any] = {"name": name, "duration_minutes": minutes, "buffer_minutes": buffer, "is_addon": addon}
    if policy:
        body["policy_id"] = policy
    r = await s.client.post("/v1/admin/services", json=body, headers=s.admin)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def _product(s: Seed, name: str, qty: int, price: str = "30.00", **extra: Any) -> uuid.UUID:
    r = await s.client.post("/v1/admin/products", json={"name": name, "stock_qty": qty, "price": price, **extra}, headers=s.admin)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def make_salon(client: httpx.AsyncClient, *, stylists: tuple[str, ...] = ("Mei", "Lin")) -> Salon:
    r = await client.post("/v1/auth/tenants", headers=PLATFORM, json={"name": f"Salon {uuid.uuid4().hex[:6]}", "business_type": "salon"})
    assert r.status_code == 201, r.text
    s = Salon(client, r.json())
    s.admin = await s.token("owner")
    ok = await client.post(
        "/v1/admin/policies", headers=s.admin,
        json={"name": "Default", "default_status": "confirmed", "min_notice_booking": 60, "min_notice_cancel": 86400,
              "booking_window": 60 * 86400, "pending_timeout": 1800, "make_default": True},
    )
    assert ok.status_code == 201, ok.text
    s.stylists = {n: await s.add_staff(n) for n in stylists}  # shifts 09:00-18:00, break 13:00-14:00
    s.svc = {
        "cut": await _service(s, "Haircut", 45), "color": await _service(s, "Colour", 90),
        "gel": await _service(s, "Gel finish", 30, addon=True), "treat": await _service(s, "Treatment", 20, addon=True, buffer=5),
    }
    s.products = {"shampoo": await _product(s, "Shampoo", 5), "wax": await _product(s, "Wax", 1, "18.50")}
    return s


@pytest.fixture
async def salon(client: httpx.AsyncClient) -> Salon:
    return await make_salon(client)


async def sbook(s: Salon, start: dt.datetime, services: list[str], *, stylist: str | None = None, **extra: Any) -> httpx.Response:
    body: dict[str, Any] = {
        "service_ids": [str(s.svc[n]) for n in services], "start_at": start.isoformat(),
        "customer": {"name": "Aisha", "phone": f"+6012{uuid.uuid4().int % 10**7:07d}"},
    }
    if stylist:
        body["resource_id"] = str(s.stylists[stylist])
    body.update(extra)
    return await s.client.post("/v1/public/bookings", json=body, headers=s.pk)


async def sslots(s: Salon, services: list[str], **params: Any) -> httpx.Response:
    return await s.client.get("/v1/public/availability", params={"date": s.day.isoformat(), "service_ids": [str(s.svc[n]) for n in services], **params}, headers=s.pk)


async def status(s: Seed, bid: str, new: str) -> httpx.Response:
    return await s.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": new}, headers=s.admin)


async def upsell(s: Salon, bid: str, services: list[str] | None = None, products: list[tuple[str, int]] | None = None, headers: dict[str, str] | None = None) -> httpx.Response:
    body = {"service_ids": [str(s.svc[n]) for n in services or []], "products": [{"product_id": str(s.products[n]), "quantity": q} for n, q in products or []]}
    return await s.client.post(f"/v1/admin/bookings/{bid}/items", json=body, headers=headers or s.admin)


async def detail(s: Seed, bid: str) -> dict[str, Any]:
    r = await s.client.get(f"/v1/admin/bookings/{bid}", headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# --------------------------------------------------------------------- add-ons extend the interval
async def test_addons_extend_the_appointment_and_are_recorded(salon: Salon) -> None:
    slots = (await sslots(salon, ["cut", "gel"])).json()["slots"]
    first = slots[0]
    assert dt.datetime.fromisoformat(first["end_at"]) - dt.datetime.fromisoformat(first["start_at"]) == dt.timedelta(minutes=75)
    starts = [dt.datetime.fromisoformat(s["start_at"]).astimezone(TZ).strftime("%H:%M") for s in slots]
    assert starts[-1] == "16:45"  # 45 + 30 minutes must still end by 18:00

    r = await sbook(salon, at(salon.day, 10), ["cut", "gel", "treat"], stylist="Mei")
    assert r.status_code == 201, r.text
    assert [(s, e) for _, _, s, e, _ in await allocations(salon, r.json()["id"])] == [("10:00", "11:40")]  # 45 + 30 + (20+5 buffer)
    d = await detail(salon, r.json()["id"])
    assert sorted(i["name"] for i in d["items"]) == ["Gel finish", "Haircut", "Treatment"] and not any(i["is_upsell"] for i in d["items"])


async def test_an_appointment_needs_exactly_one_base_service(salon: Salon) -> None:
    only_addon = await sbook(salon, at(salon.day, 10), ["gel"])
    assert only_addon.status_code == 422 and "base service" in only_addon.json()["detail"]
    two_bases = await sbook(salon, at(salon.day, 10), ["cut", "color"])
    assert two_bases.status_code == 422
    assert (await sslots(salon, ["gel"])).status_code == 422
    assert (await salon.client.get("/v1/admin/bookings", headers=salon.admin)).json() == []


async def test_stylist_qualification_follows_the_base_service_only(salon: Salon) -> None:
    mei, lin = salon.stylists["Mei"], salon.stylists["Lin"]
    await salon.client.patch(f"/v1/admin/services/{salon.svc['cut']}", json={"resource_ids": [str(mei)]}, headers=salon.admin)
    await salon.client.patch(f"/v1/admin/services/{salon.svc['gel']}", json={"resource_ids": [str(lin)]}, headers=salon.admin)
    slots = (await sslots(salon, ["cut", "gel"])).json()["slots"]
    assert slots and all(s["resource_ids"] == [str(mei)] for s in slots)  # Mei cuts; the add-on does not need Lin
    r = await sbook(salon, at(salon.day, 10), ["cut", "gel"])
    assert r.status_code == 201 and r.json()["resource_ids"] == [str(mei)]


async def test_with_no_stylist_chosen_the_least_loaded_one_is_assigned(salon: Salon) -> None:
    await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")
    auto = await sbook(salon, at(salon.day, 15), ["cut", "gel"])
    assert auto.json()["resource_ids"] == [str(salon.stylists["Lin"])]


# --------------------------------------------------------------------- strictest policy
async def test_a_cart_with_several_policies_gets_the_strictest(salon: Salon) -> None:
    strict = await salon.client.post("/v1/admin/policies", headers=salon.admin, json={
        "name": "Strict", "default_status": "pending", "min_notice_booking": 10 * 86400, "min_notice_cancel": 3 * 86400,
        "booking_window": 20 * 86400, "pending_timeout": 600})
    assert strict.status_code == 201
    salon.svc["keratin"] = await _service(salon, "Keratin", 30, addon=True, policy=strict.json()["id"])

    assert (await sbook(salon, at(salon.day, 10), ["cut"])).status_code == 201  # default policy alone: 3 days ahead is fine
    early = await sbook(salon, at(salon.day, 11), ["cut", "keratin"])  # the add-on's 10-day notice governs the whole cart
    assert early.status_code == 422 and early.json()["error"] == "outside_booking_window"
    far = await sbook(salon, at(salon.day + dt.timedelta(days=25), 10), ["cut", "keratin"])
    assert far.status_code == 422 and far.json()["error"] == "outside_booking_window"  # the add-on's 20-day window is shorter

    ok = await sbook(salon, at(salon.day + dt.timedelta(days=12), 10), ["cut", "keratin"])
    assert ok.status_code == 201 and ok.json()["status"] == "pending" and ok.json()["hold_expires_at"]  # pending wins
    snap = (await detail(salon, ok.json()["id"]))["policy_snapshot"]
    assert snap["min_notice_booking_s"] == 10 * 86400 and snap["min_notice_cancel_s"] == 3 * 86400  # longest cancellation notice
    assert snap["booking_window_s"] == 20 * 86400 and snap["pending_timeout_s"] == 600  # shortest window and timeout
    assert len(snap["merged_from"]) == 2


async def test_a_cart_with_one_distinct_policy_is_not_treated_as_merged(salon: Salon) -> None:
    r = await sbook(salon, at(salon.day, 10), ["cut", "gel"])  # both services fall back to the tenant default
    assert "merged_from" not in (await detail(salon, r.json()["id"]))["policy_snapshot"]


# --------------------------------------------------------------------- on-the-spot upsell
async def test_an_addon_added_during_the_appointment_extends_the_reservation(salon: Salon) -> None:
    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]  # 10:00-10:45
    await status(salon, bid, "in-progress")
    r = await upsell(salon, bid, ["gel"])
    assert r.status_code == 200, r.text
    assert r.json()["extended"] is True and r.json()["added_minutes"] == 30 and r.json()["conflicts"] == []
    assert [(s, e) for _, _, s, e, _ in await allocations(salon, bid)] == [("10:00", "11:15")]
    d = await detail(salon, bid)
    assert dt.datetime.fromisoformat(d["end_at"]) == at(salon.day, 11, 15)
    assert {(i["name"], i["is_upsell"]) for i in d["items"]} == {("Haircut", False), ("Gel finish", True)}
    assert "10:45" not in [dt.datetime.fromisoformat(s["start_at"]).astimezone(TZ).strftime("%H:%M") for s in (await sslots(salon, ["cut"], resource_id=str(salon.stylists["Mei"]))).json()["slots"]]


async def test_an_upsell_that_reaches_the_next_appointment_alerts_the_stylist_and_forces_nothing(salon: Salon) -> None:
    mei = "Mei"
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist=mei)).json()["id"]  # 10:00-10:45
    nxt = await sbook(salon, at(salon.day, 10, 45), ["color"], stylist=mei, customer={"name": "Next Customer", "phone": "+60111222333"})
    assert nxt.status_code == 201
    await status(salon, first, "in-progress")
    r = await upsell(salon, first, ["gel"])  # +30 min would run to 11:15, into the 10:45 appointment
    assert r.status_code == 200 and r.json()["extended"] is False
    [alert] = r.json()["conflicts"]
    assert alert["booking_id"] == nxt.json()["id"] and alert["status"] == "confirmed"
    assert dt.datetime.fromisoformat(alert["start_at"]) == at(salon.day, 10, 45) and dt.datetime.fromisoformat(alert["end_at"]) == at(salon.day, 12, 15)
    assert alert["customer_name"] == "Next Customer" and alert["service_names"] == ["Colour"]
    assert [(s, e) for _, _, s, e, _ in await allocations(salon, first)] == [("10:00", "10:45")]  # not stretched into their time
    assert [(s, e) for _, _, s, e, _ in await allocations(salon, nxt.json()["id"])] == [("10:45", "12:15")]  # and theirs is untouched
    assert {i["name"] for i in (await detail(salon, first))["items"]} == {"Haircut", "Gel finish"}  # the customer still gets the add-on
    flagged = (await salon.client.get("/v1/admin/bookings", params={"schedule_conflict": "true"}, headers=salon.admin)).json()
    assert {b["id"] for b in flagged} == {first, nxt.json()["id"]}


async def test_an_upsell_ending_exactly_when_the_next_appointment_starts_is_fine(salon: Salon) -> None:
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]  # 10:00-10:45
    await sbook(salon, at(salon.day, 11, 15), ["cut"], stylist="Mei")  # starts right where +30 min would end
    await status(salon, first, "in-progress")
    r = await upsell(salon, first, ["gel"])
    assert r.json()["extended"] is True and r.json()["conflicts"] == []


async def test_upsell_rules(client: httpx.AsyncClient, salon: Salon) -> None:
    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    early = await upsell(salon, bid, ["gel"])
    assert early.status_code == 409 and "in progress" in early.json()["detail"]
    await status(salon, bid, "in-progress")
    assert (await upsell(salon, bid, ["color"])).status_code == 422  # a base service is not an add-on
    assert (await upsell(salon, bid)).status_code == 422  # nothing to add
    assert (await client.post(f"/v1/admin/bookings/{bid}/items", json={"service_ids": [str(uuid.uuid4())]}, headers=salon.admin)).status_code == 404
    other = (await sbook(salon, at(salon.day, 15), ["cut"], stylist="Lin")).json()["id"]
    await status(salon, other, "in-progress")
    mei_only = await salon.token("staff", resource_id=salon.stylists["Mei"])
    assert (await upsell(salon, other, ["gel"], headers=mei_only)).status_code == 403  # staff: own appointments only
    assert (await upsell(salon, bid, ["gel"], headers=mei_only)).status_code == 200
    spa = await make_seed(client)
    assert (await client.post(f"/v1/admin/bookings/{uuid.uuid4()}/items", json={"service_ids": [str(spa.service_id)]}, headers=spa.admin)).status_code == 409


async def test_concurrent_upsells_to_one_appointment_all_apply_exactly_once(salon: Salon, monkeypatch: pytest.MonkeyPatch) -> None:
    """A barrier *before* the row lock makes all three requests start together. Without the lock they
    would each read the same reservation and the last write would silently erase the others."""
    from app.services import upsell as upsell_module

    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]  # 10:00-10:45
    await status(salon, bid, "in-progress")
    barrier = asyncio.Barrier(3)
    real = upsell_module.add_to_ticket

    async def together(*a: Any, **k: Any) -> Any:
        await asyncio.wait_for(barrier.wait(), timeout=10)
        return await real(*a, **k)

    monkeypatch.setattr(upsell_module, "add_to_ticket", together)
    results = await asyncio.gather(*[upsell(salon, bid, ["gel"]) for _ in range(3)])
    monkeypatch.undo()
    assert [r.status_code for r in results] == [200, 200, 200]
    assert [(s, e) for _, _, s, e, _ in await allocations(salon, bid)] == [("10:00", "12:15")]  # 45 + 3 x 30: no lost update
    d = await detail(salon, bid)
    assert len(d["items"]) == 4 and dt.datetime.fromisoformat(d["end_at"]) == at(salon.day, 12, 15)


async def test_a_booking_slipping_into_the_gap_is_reported_as_a_clash_not_a_server_error(
    salon: Salon, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]  # 10:00-10:45
    await sbook(salon, at(salon.day, 10, 45), ["cut"], stylist="Mei")  # sits exactly where the extension would go
    await status(salon, first, "in-progress")
    real = booking_module._clashes
    calls = {"n": 0}

    async def blind_first_time(*a: Any, **k: Any) -> list[uuid.UUID]:
        calls["n"] += 1
        return [] if calls["n"] == 1 else await real(*a, **k)  # the check "misses" the neighbour, so the write must catch it

    monkeypatch.setattr(booking_module, "_clashes", blind_first_time)
    r = await upsell(salon, first, ["gel"])
    assert r.status_code == 200 and r.json()["extended"] is False and len(r.json()["conflicts"]) == 1
    assert calls["n"] == 2  # the constraint refused the write, then the clash was looked up again
    assert [(s, e) for _, _, s, e, _ in await allocations(salon, first)] == [("10:00", "10:45")]


# --------------------------------------------------------------------- retail products and stock
async def test_public_catalog_shows_status_not_counts(salon: Salon) -> None:
    listing = (await salon.client.get("/v1/public/products", headers=salon.pk)).json()
    assert {p["name"]: p["stock_status"] for p in listing} == {"Shampoo": "in_stock", "Wax": "in_stock"}
    assert all("stock_qty" not in p and "sku" not in p for p in listing)


async def test_products_go_on_the_ticket_at_todays_price_with_stock_checks(salon: Salon) -> None:
    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    await status(salon, bid, "in-progress")
    assert (await upsell(salon, bid, products=[("shampoo", 2)])).status_code == 200
    assert (await upsell(salon, bid, products=[("shampoo", 1)])).status_code == 200  # same product again: quantity adds up
    d = await detail(salon, bid)
    assert [(p["name"], p["quantity"], Decimal(p["unit_price"])) for p in d["products"]] == [("Shampoo", 3, Decimal("30.00"))]
    over = await upsell(salon, bid, products=[("shampoo", 3)])  # 3 already on the ticket + 3 > 5 in stock
    assert over.status_code == 409 and over.json()["error"] == "insufficient_stock" and over.json()["in_stock"] == 5
    assert (await detail(salon, bid))["products"][0]["quantity"] == 3  # the refused request changed nothing
    await salon.client.put(f"/v1/admin/products/{salon.products['shampoo']}", json={"name": "Shampoo", "stock_qty": 5, "price": "99.00"}, headers=salon.admin)
    assert Decimal((await detail(salon, bid))["products"][0]["unit_price"]) == Decimal("30.00")  # price was snapshotted
    unknown = await salon.client.post(f"/v1/admin/bookings/{bid}/items", json={"products": [{"product_id": str(uuid.uuid4())}]}, headers=salon.admin)
    assert unknown.status_code == 404
    gone = await _product(salon, "Serum", 0, stock_status="out_of_stock")
    assert (await salon.client.post(f"/v1/admin/bookings/{bid}/items", json={"products": [{"product_id": str(gone)}]}, headers=salon.admin)).status_code == 409


async def _stock(s: Salon, name: str) -> tuple[int, str]:
    listing = (await s.client.get("/v1/admin/products", headers=s.admin)).json()
    p = next(x for x in listing if x["name"] == name)
    return p["stock_qty"], p["stock_status"]


async def test_completing_deducts_stock_and_zero_stock_shows_out_of_stock(salon: Salon) -> None:
    bid = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    await status(salon, bid, "in-progress")
    await upsell(salon, bid, ["gel"], [("shampoo", 2), ("wax", 1)])
    assert await _stock(salon, "Shampoo") == (5, "in_stock")  # nothing is deducted while the appointment is running
    assert (await status(salon, bid, "completed")).status_code == 200
    assert await _stock(salon, "Shampoo") == (3, "in_stock")
    assert await _stock(salon, "Wax") == (0, "out_of_stock")  # the last unit went
    catalog = {p["name"]: p["stock_status"] for p in (await salon.client.get("/v1/public/products", headers=salon.pk)).json()}
    assert catalog["Wax"] == "out_of_stock"
    assert (await detail(salon, bid))["actual_duration_minutes"] == 75  # duration finalised: 45 + the 30-minute add-on


async def test_stock_never_goes_negative_and_on_order_status_is_kept(salon: Salon) -> None:
    ordered = await _product(salon, "Colour tube", 1, stock_status="available_on_order", estimated_lead_time_days=3)
    salon.products["tube"] = ordered
    a = (await sbook(salon, at(salon.day, 10), ["cut"], stylist="Mei")).json()["id"]
    b = (await sbook(salon, at(salon.day, 15), ["cut"], stylist="Lin")).json()["id"]
    for bid in (a, b):
        await status(salon, bid, "in-progress")
        assert (await upsell(salon, bid, products=[("tube", 1)])).status_code == 200  # both saw the last unit
    await status(salon, a, "completed")
    await status(salon, b, "completed")
    assert await _stock(salon, "Colour tube") == (0, "available_on_order")  # clamped at zero, not -1; on-order flag untouched


# --------------------------------------------------------------------- catalog and isolation
async def test_addon_flag_is_managed_through_the_service_api(salon: Salon) -> None:
    r = await salon.client.patch(f"/v1/admin/services/{salon.svc['gel']}", json={"is_addon": False}, headers=salon.admin)
    assert r.status_code == 200 and r.json()["is_addon"] is False
    assert (await sbook(salon, at(salon.day, 10), ["cut", "gel"])).status_code == 422  # now it is a second base service
    listing = (await salon.client.get("/v1/public/services", headers=salon.pk)).json()
    assert {s["name"]: s["is_addon"] for s in listing}["Treatment"] is True


async def test_ticket_products_are_isolated_per_tenant(client: httpx.AsyncClient) -> None:
    from sqlalchemy import func, select

    from app.core.database import tenant_session
    from app.models import BookingProduct

    a, b = await make_salon(client), await make_salon(client)
    bid = (await sbook(a, at(a.day, 10), ["cut"], stylist="Mei")).json()["id"]
    await status(a, bid, "in-progress")
    await upsell(a, bid, products=[("shampoo", 1)])
    async with tenant_session(a.tenant_id) as s:
        assert await s.scalar(select(func.count()).select_from(BookingProduct)) == 1
    async with tenant_session(b.tenant_id) as s:
        assert await s.scalar(select(func.count()).select_from(BookingProduct)) == 0
