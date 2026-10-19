"""Webhooks: the edges the first pass of mutation testing found uncovered."""
from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import httpx
import pytest
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models.events import OutboxEvent
from app.services import webhooks as hooks
from app.services.webhooks import UnsafeUrl, WebhookPublisher, check_url
from tests.conftest import Recorder, Seed, at
from tests.test_customer_identity import booked_with_email
from tests.test_webhooks import (  # noqa: F401  (fixtures)
    H,
    URL,
    Receiver,
    add_hook,
    deliveries,
    endpoints_count,
    net,
    tick,
    worker_publishers,
)


async def test_a_name_with_no_addresses_is_refused() -> None:
    async def nothing(host: str) -> list[str]:
        return []

    hooks.set_resolver(nothing)
    try:
        with pytest.raises(UnsafeUrl, match="does not resolve"):
            await check_url("https://hooks.example.com/in")
    finally:
        hooks.set_resolver(None)


async def test_no_more_than_three_addresses_are_tried(seeded: Seed, net: Receiver) -> None:
    async def many(host: str) -> list[str]:
        return [f"93.184.216.{n}" for n in range(30, 38)]

    hooks.set_resolver(many)
    hook = await add_hook(seeded)
    net.respond = lambda request: httpx.ConnectError("refused")
    await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)
    assert len(net.requests) == 3


async def test_requests_carry_the_configured_timeout(seeded: Seed, net: Receiver, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "webhook_timeout_seconds", 2.5)
    hook = await add_hook(seeded)
    await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)
    assert net.requests[0].extensions["timeout"] == {"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5}


async def test_informational_and_redirect_statuses_are_not_success(seeded: Seed, net: Receiver) -> None:
    hook = await add_hook(seeded)
    for status in (199, 300, 304, 399):
        net.respond = lambda request, status=status: httpx.Response(status)  # type: ignore[misc]
        r = (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()
        assert r == {"delivered": False, "status_code": status, "error": f"HTTP {status}"}, status
    for status in (200, 201, 204, 299):
        net.respond = lambda request, status=status: httpx.Response(status)  # type: ignore[misc]
        assert (await seeded.client.post(f"/v1/admin/webhooks/{hook['id']}/test", headers=seeded.admin)).json()["delivered"] is True, status


async def test_a_pending_delivery_of_a_switched_off_endpoint_is_not_sent(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    await add_hook(seeded, events=["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    await booked_with_email(seeded, hour=10)
    await tick(seeded)  # owed, tried once, failed: pending a retry
    assert len(net.requests) == 1
    async with tenant_session(seeded.tenant_id) as db:  # switched off behind the API's back, so the delivery stays pending
        await db.execute(text("UPDATE webhook_endpoints SET active = false"))
    assert [d.status for d in await deliveries(seeded)] == ["pending"]
    await tick(seeded, dt.datetime.now(dt.UTC) + H)
    assert len(net.requests) == 1


async def test_two_sweeps_delivering_at_the_same_moment_do_not_send_the_same_delivery_twice(
    seeded: Seed, net: Receiver, worker_publishers: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    await add_hook(seeded, events=["booking.confirmed"])
    mei = await seeded.add_staff("Mei")
    for hour in (9, 10, 11, 12):
        await seeded.client.post("/v1/public/bookings", headers=seeded.pk, json=seeded.booking_body(
            at(seeded.day, hour), mei, customer={"name": "A", "phone": f"+6012000{hour:04d}"}))
    async with tenant_session(seeded.tenant_id) as db:  # fan out first, without sending
        for event in (await db.scalars(select(OutboxEvent).order_by(OutboxEvent.seq))).all():
            await WebhookPublisher().publish(event)
    real_send = hooks.send

    async def slow(url: str, body: bytes, headers: dict[str, str]) -> hooks.SendResult:
        await asyncio.sleep(0.2)  # long enough for the other sweep to look at the same rows
        return await real_send(url, body, headers)

    monkeypatch.setattr(hooks, "send", slow)

    async def one_sweep() -> int:
        async with tenant_session(seeded.tenant_id) as db:
            return (await hooks.deliver_due(db, dt.datetime.now(dt.UTC) + H)).delivered

    sent = await asyncio.gather(one_sweep(), one_sweep())
    ids = [r.headers["x-webhook-id"] for r in net.requests]
    assert sum(sent) == len(ids) == 4 and len(set(ids)) == 4


async def test_switching_one_endpoint_off_leaves_the_pending_deliveries_of_another(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    one = await add_hook(seeded, "https://one.example.com/in", ["booking.confirmed"])
    two = await add_hook(seeded, "https://two.example.com/in", ["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    await booked_with_email(seeded, hour=10)
    await tick(seeded)
    assert (await seeded.client.patch(f"/v1/admin/webhooks/{one['id']}", json={"active": False}, headers=seeded.admin)).status_code == 200
    assert [d.status for d in await deliveries(seeded, one["id"])] == ["failed"]
    assert [d.status for d in await deliveries(seeded, two["id"])] == ["pending"]  # the other one is still being retried


async def test_each_webhooks_delivery_list_shows_only_its_own(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    one = await add_hook(seeded, "https://one.example.com/in", ["booking.confirmed"])
    two = await add_hook(seeded, "https://two.example.com/in", ["*"])
    await booked_with_email(seeded, hour=10)
    await tick(seeded)
    for hook, expected in ((one, 1), (two, 3)):
        listing = (await seeded.client.get(f"/v1/admin/webhooks/{hook['id']}/deliveries", headers=seeded.admin)).json()
        assert len(listing) == expected
        assert {d["id"] for d in listing} == {str(d.id) for d in await deliveries(seeded, hook["id"])}
    assert (await seeded.client.get(f"/v1/admin/webhooks/{uuid.uuid4()}/deliveries", headers=seeded.admin)).status_code == 404


async def test_a_delivery_that_finally_succeeds_forgets_its_old_error(seeded: Seed, net: Receiver, worker_publishers: Recorder) -> None:
    await add_hook(seeded, events=["booking.confirmed"])
    net.respond = lambda request: httpx.Response(500)
    await booked_with_email(seeded, hour=10)
    now = dt.datetime.now(dt.UTC)
    await tick(seeded, now)
    net.respond = lambda request: httpx.Response(200)
    await tick(seeded, now + H)
    (delivery,) = await deliveries(seeded)
    assert (delivery.status, delivery.error) == ("delivered", None)


async def test_the_endpoint_limit_is_enforced_exactly(seeded: Seed, net: Receiver) -> None:
    for n in range(hooks.MAX_ENDPOINTS - 1):
        await add_hook(seeded, f"https://hooks.example.com/{n}")
    await add_hook(seeded, "https://hooks.example.com/last")  # the tenth is fine
    assert await endpoints_count(seeded) == hooks.MAX_ENDPOINTS
    assert (await seeded.client.post("/v1/admin/webhooks", json={"url": URL, "events": ["*"]}, headers=seeded.admin)).status_code == 409
