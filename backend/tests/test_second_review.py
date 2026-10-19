"""The open findings of the second code review (2026-10-14), fixed 2026-10-15:

1. a saved address or vehicle that an open booking uses cannot be changed (it would get round the checks made when booking);
2. erasing a customer rewrites their Google Calendar events without them;
3. erasure clears the idempotency key and request hash;
4. a webhook's host-name lookup has a time limit, and every unusable host gets one answer;
5. two obsolete IPv6 ranges are not public.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models import Tenant
from app.services import compliance, webhooks
from app.services.webhooks import UNUSABLE_HOST, UnsafeUrl, check_url, is_public
from tests.conftest import Seed, at
from tests.test_automotive import Garage, gbook, make_garage
from tests.test_calendar import Google, book, connect, google, only_event, redis, set_status, tick  # noqa: F401
from tests.test_customer_cancel import PHONE, login, otp  # noqa: F401
from tests.test_customer_profile import car, garage  # noqa: F401  (``garage`` is a fixture)
from tests.test_field_service import NEAR, SEREMBAN, Practice, pbook, pin, practice  # noqa: F401  (``practice`` is a fixture)

CUSTOMER = {"name": "Aisha", "phone": PHONE}


@pytest.fixture(autouse=True)
def strict_webhooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_allow_insecure", False)


# --------------------------------------------------------------------- 1. addresses
async def booked_address(p: Practice, hour: int = 10, phone: str = PHONE) -> tuple[dict[str, str], str, str]:
    """A customer with an on-site booking: their login headers, the saved address's id, the booking's id."""
    r = await pbook(p, at(p.day, hour), "site", customer={"name": "Aisha", "phone": phone}, address=pin(NEAR))
    assert r.status_code == 201, r.text
    me = await login(p, phone)
    (address,) = (await p.client.get("/v1/public/me/addresses", headers=me)).json()
    return me, str(address["id"]), str(r.json()["id"])


@pytest.mark.usefixtures("postgis")
@pytest.mark.parametrize("change", [
    {"latitude": SEREMBAN[0], "longitude": SEREMBAN[1]}, {"line1": "99 Jalan Lain"}, {"city": "Seremban"}, {"postcode": "70000"},
    {"latitude": SEREMBAN[0], "longitude": SEREMBAN[1], "line1": "99 Jalan Lain"},
], ids=["pin", "street", "city", "postcode", "all"])
async def test_an_address_an_open_booking_uses_cannot_be_changed(practice: Practice, otp: Any, change: dict[str, Any]) -> None:
    me, aid, _ = await booked_address(practice)
    before = (await practice.client.get("/v1/public/me/addresses", headers=me)).json()
    r = await practice.client.patch(f"/v1/public/me/addresses/{aid}", json=change, headers=me)
    assert r.status_code == 409 and "1 upcoming booking" in r.json()["detail"] and "save a new address" in r.json()["detail"], r.text
    assert (await practice.client.get("/v1/public/me/addresses", headers=me)).json() == before  # nothing was changed


@pytest.mark.usefixtures("postgis")
async def test_saying_what_is_already_saved_changes_nothing_and_is_allowed(practice: Practice, otp: Any) -> None:
    me, aid, _ = await booked_address(practice)
    (saved,) = (await practice.client.get("/v1/public/me/addresses", headers=me)).json()
    same = await practice.client.patch(f"/v1/public/me/addresses/{aid}", json={"line1": saved["line1"], "latitude": saved["latitude"], "longitude": saved["longitude"]}, headers=me)
    assert same.status_code == 200 and same.json() == saved


@pytest.mark.usefixtures("postgis")
async def test_another_address_of_the_same_customer_can_still_be_edited(practice: Practice, otp: Any) -> None:
    me, aid, _ = await booked_address(practice)
    other = (await practice.client.post("/v1/public/me/addresses", json=pin(NEAR, "Office"), headers=me)).json()["id"]
    moved = await practice.client.patch(f"/v1/public/me/addresses/{other}", json={"latitude": SEREMBAN[0], "longitude": SEREMBAN[1]}, headers=me)
    assert moved.status_code == 200 and moved.json()["latitude"] == SEREMBAN[0]
    assert (await practice.client.patch(f"/v1/public/me/addresses/{aid}", json={"city": "x"}, headers=me)).status_code == 409


@pytest.mark.usefixtures("postgis")
@pytest.mark.parametrize("ending", ["cancelled", "completed"])
async def test_once_the_booking_is_over_the_address_can_be_changed(practice: Practice, otp: Any, ending: str) -> None:
    me, aid, bid = await booked_address(practice)
    assert (await practice.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": ending}, headers=practice.admin)).status_code == 200
    moved = await practice.client.patch(f"/v1/public/me/addresses/{aid}", json={"latitude": SEREMBAN[0], "longitude": SEREMBAN[1]}, headers=me)
    assert moved.status_code == 200 and moved.json()["longitude"] == SEREMBAN[1]


@pytest.mark.usefixtures("postgis")
async def test_the_same_place_that_is_refused_when_booking_is_refused_after_booking_too(practice: Practice, otp: Any) -> None:
    """The finding: book near, then edit the saved address to a point outside the radius."""
    me, aid, _ = await booked_address(practice)
    assert (await pbook(practice, at(practice.day, 15), "site", customer=CUSTOMER, address=pin(SEREMBAN))).status_code == 422  # outside_service_area at booking
    assert (await practice.client.patch(f"/v1/public/me/addresses/{aid}", json={"latitude": SEREMBAN[0], "longitude": SEREMBAN[1]}, headers=me)).status_code == 409


# --------------------------------------------------------------------- 1. vehicles
async def booked_vehicle(g: Garage, hour: int = 10) -> tuple[dict[str, str], str, str]:
    me = await login(g, PHONE)
    vid = (await g.client.post("/v1/public/me/vehicles", json=car(g, "abc 123", "Vios", year=2019), headers=me)).json()["id"]
    r = await gbook(g, at(g.day, hour), ["oil"], customer=CUSTOMER, vehicle=None, vehicle_id=vid)
    assert r.status_code == 201, r.text
    return me, str(vid), str(r.json()["id"])


@pytest.mark.parametrize("change", [{"plate_no": "xyz 999"}, {"year": 2021}, {"supported_vehicle_id": "CITY"}, {"plate_no": "xyz 999", "year": 2021}], ids=["plate", "year", "model", "two"])
async def test_a_vehicle_an_open_job_order_uses_cannot_be_changed(garage: Garage, otp: Any, change: dict[str, Any]) -> None:
    me, vid, _ = await booked_vehicle(garage)
    body = {k: (str(garage.cars["City"]) if v == "CITY" else v) for k, v in change.items()}
    before = (await garage.client.get("/v1/public/me/vehicles", headers=me)).json()
    r = await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json=body, headers=me)
    assert r.status_code == 409 and "1 upcoming booking" in r.json()["detail"] and "save a new vehicle" in r.json()["detail"], r.text
    assert (await garage.client.get("/v1/public/me/vehicles", headers=me)).json() == before


async def test_a_vehicle_edit_that_changes_nothing_is_allowed_and_a_bad_one_is_still_422(garage: Garage, otp: Any) -> None:
    me, vid, _ = await booked_vehicle(garage)
    same = await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"plate_no": "ABC-123", "year": 2019, "supported_vehicle_id": str(garage.cars["Vios"])}, headers=me)
    assert same.status_code == 200 and same.json()["year"] == 2019 and same.json()["plate_no"] == "ABC123"
    assert (await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"year": 1800}, headers=me)).status_code == 422
    assert (await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"supported_vehicle_id": str(uuid.uuid4())}, headers=me)).status_code == 422  # invalid input first


async def test_another_vehicle_and_a_finished_job_leave_the_vehicle_free_to_change(garage: Garage, otp: Any) -> None:
    me, vid, bid = await booked_vehicle(garage)
    other = (await garage.client.post("/v1/public/me/vehicles", json=car(garage, "DEF 456", "Vios", year=2018), headers=me)).json()["id"]
    assert (await garage.client.patch(f"/v1/public/me/vehicles/{other}", json={"year": 2020}, headers=me)).status_code == 200
    assert (await garage.client.patch(f"/v1/admin/bookings/{bid}/status", json={"status": "cancelled"}, headers=garage.admin)).status_code == 200
    assert (await garage.client.patch(f"/v1/public/me/vehicles/{vid}", json={"plate_no": "xyz 999", "year": 2021}, headers=me)).json()["year"] == 2021


async def test_deleting_is_still_refused_while_a_booking_uses_the_row(garage: Garage, otp: Any) -> None:
    me, vid, _ = await booked_vehicle(garage)
    assert (await garage.client.delete(f"/v1/public/me/vehicles/{vid}", headers=me)).status_code == 409


# --------------------------------------------------------------------- 2 and 3. erasure
async def erase(s: Seed) -> None:
    async with tenant_session(s.tenant_id) as db:
        tenant = await db.get(Tenant, s.tenant_id)
        cid = (await db.execute(text("SELECT id FROM customers"))).scalar_one()
        assert tenant is not None
        await compliance.erase_customer(db, tenant, cid, dt.datetime.now(dt.UTC))


async def test_erasing_a_customer_rewrites_their_calendar_events_without_them(seeded: Seed, google: Google) -> None:
    await connect(seeded, google, share_customer_details=True)
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, 10, customer={"name": "Aisha Karim", "phone": "+60123450001"}, special_request="please call the gate")
    await set_status(seeded, bid, "completed")
    await tick(seeded)
    event = only_event(google)
    assert "Aisha Karim" in event["summary"] and "+60123450001" in event["description"] and "please call the gate" in event["description"]
    await erase(seeded)
    await tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    event = only_event(google)
    assert event["summary"] == "Massage" and "Aisha" not in str(event) and "60123450001" not in str(event) and "gate" not in str(event)
    assert "Booking " in event["description"] and "Status: completed" in event["description"]  # the booking itself stays on the calendar


async def test_a_calendar_that_was_paused_when_the_customer_was_erased_is_rewritten_when_connected_again(seeded: Seed, google: Google) -> None:
    await connect(seeded, google, share_customer_details=True)
    mei = await seeded.add_staff("Mei")
    bid = await book(seeded, mei, 10, customer={"name": "Aisha Karim", "phone": "+60123450001"})
    await set_status(seeded, bid, "completed")
    await tick(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        await db.execute(text("UPDATE calendar_connections SET status = 'needs_reconnect'"))
    await erase(seeded)
    await tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(hours=1))
    assert "Aisha Karim" in only_event(google)["summary"]  # nothing can be written to a paused calendar
    await connect(seeded, google, share_customer_details=True)  # the owner connects it again
    await tick(seeded, dt.datetime.now(dt.UTC) + dt.timedelta(hours=2))
    assert "Aisha" not in str(only_event(google))


async def test_erasing_a_customer_with_no_calendar_connected_still_works(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": "+60123450001"}))
    await seeded.client.patch(f"/v1/admin/bookings/{r.json()['id']}/status", json={"status": "cancelled"}, headers=seeded.admin)
    await erase(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        assert (await db.execute(text("SELECT count(*) FROM calendar_events"))).scalar_one() == 0


async def test_erasure_clears_the_idempotency_key_and_the_request_hash(seeded: Seed) -> None:
    mei = await seeded.add_staff("Mei")
    r = await seeded.client.post("/v1/public/bookings", headers={**seeded.pk, "Idempotency-Key": str(uuid.uuid4())}, json=seeded.booking_body(at(seeded.day, 10), mei, customer={"name": "Aisha", "phone": "+60123450001"}))
    assert r.status_code == 201
    async with tenant_session(seeded.tenant_id) as db:
        assert tuple((await db.execute(text("SELECT idempotency_key IS NOT NULL, idempotency_hash IS NOT NULL FROM bookings"))).one()) == (True, True)
    await seeded.client.patch(f"/v1/admin/bookings/{r.json()['id']}/status", json={"status": "cancelled"}, headers=seeded.admin)
    await erase(seeded)
    async with tenant_session(seeded.tenant_id) as db:
        assert tuple((await db.execute(text("SELECT idempotency_key IS NULL, idempotency_hash IS NULL FROM bookings"))).one()) == (True, True)


# --------------------------------------------------------------------- 4. the host-name lookup
async def lookup(answer: Any) -> Any:
    async def resolver(host: str) -> list[str]:
        if isinstance(answer, Exception):
            raise answer
        if callable(answer):
            return await answer()  # type: ignore[no-any-return]
        return list(answer)

    return resolver


async def refused_text(answer: Any) -> str:
    webhooks.set_resolver(await lookup(answer))
    try:
        with pytest.raises(UnsafeUrl) as raised:
            await check_url("https://hooks.example.test/x")
    finally:
        webhooks.set_resolver(None)
    return str(raised.value)


async def test_a_name_server_that_never_answers_cannot_hold_a_webhook_for_long(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_timeout_seconds", 0.2)

    async def never() -> list[str]:
        await asyncio.sleep(30)
        return ["93.184.216.34"]

    webhooks.set_resolver(await lookup(never))
    try:
        began = time.monotonic()
        with pytest.raises(UnsafeUrl, match="does not resolve to a public server"):
            await check_url("https://tarpit.example.test/hook")
        sent = await webhooks.send("https://tarpit.example.test/hook", b"{}", {})
        assert time.monotonic() - began < 3
        assert sent.status is None and sent.error is not None and sent.error.startswith("blocked:")
    finally:
        webhooks.set_resolver(None)


async def test_a_name_that_answers_in_time_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_timeout_seconds", 2.0)

    async def quick() -> list[str]:
        await asyncio.sleep(0.05)
        return ["93.184.216.34"]

    webhooks.set_resolver(await lookup(quick))
    try:
        await check_url("https://fine.example.test/hook")
    finally:
        webhooks.set_resolver(None)


async def test_every_unusable_host_gets_the_same_answer_so_names_cannot_be_probed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow() -> list[str]:
        await asyncio.sleep(30)
        return ["93.184.216.34"]

    monkeypatch.setattr(get_settings(), "webhook_timeout_seconds", 0.2)
    messages = {
        await refused_text(OSError("no such host")), await refused_text([]), await refused_text(["10.0.0.5"]), await refused_text(["127.0.0.1", "93.184.216.34"]),
        await refused_text(["::127.0.0.1"]), await refused_text(["fec0::1"]), await refused_text(slow),
    }
    assert messages == {UNUSABLE_HOST}
    assert "private" not in UNUSABLE_HOST and "reserved" not in UNUSABLE_HOST


async def test_a_host_written_as_an_address_is_not_looked_up_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    async def resolver(host: str) -> list[str]:
        asked.append(host)
        return ["93.184.216.34"]

    webhooks.set_resolver(resolver)
    try:
        await webhooks.resolve_public("93.184.216.34")
    finally:
        webhooks.set_resolver(None)
    assert asked == []


# --------------------------------------------------------------------- 5. the address rules
@pytest.mark.parametrize("address", [
    "::127.0.0.1", "::10.0.0.1", "::192.168.1.1", "::169.254.169.254", "::8.8.8.8", "::1", "::", "::2", "0:0:0:0:0:0:7f00:1",
    "::1:0:0", "0:1::1", "0:ffff:ffff:ffff:ffff:ffff:ffff:ffff",  # (the rest of the reserved ::/8 block, not only ::/96)
    "fec0::1", "fec0::ffff:ffff", "feff::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "fe80::1", "fc00::1", "2002:7f00:1::", "64:ff9b::7f00:1", "2001::1",
    "127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "0.0.0.0",
])
def test_these_addresses_are_never_public(address: str) -> None:
    assert is_public(address) is False


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2001:4860:4860::8888", "2606:4700:4700::1111", "::ffff:8.8.8.8", "2a00:1450:4001:81b::200e"])
def test_ordinary_internet_addresses_are_public(address: str) -> None:
    assert is_public(address) is True


async def test_a_table_with_a_minimum_spend_that_is_not_a_number_is_refused_when_saved(seeded: Seed) -> None:
    """Looked at in the review as a possible 500: it cannot be saved in the first place."""
    for bad in ("abc", "", None, [1], -5):
        r = await seeded.client.post("/v1/admin/resources", json={"type": "table", "name": "T1", "capacity": 4, "attributes": {"is_vip": True, "minimum_spend": bad}}, headers=seeded.admin)
        assert r.status_code == 422, (bad, r.text)
    assert (await seeded.client.post("/v1/admin/resources", json={"type": "table", "name": "T2", "capacity": 4, "attributes": {"is_vip": True, "minimum_spend": "50.5"}}, headers=seeded.admin)).status_code == 201
