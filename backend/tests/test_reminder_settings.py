"""Reminder lead times chosen by each business (GET/PUT/DELETE /admin/reminder-settings)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import tenant_session
from app.services import reminders
from tests.conftest import Recorder, Seed, at, make_seed
from tests.test_customer_identity import booked_with_email
from tests.test_reminders import H, notify, reminder_rows, reminders_sent, tick  # noqa: F401  (``notify`` is a fixture)

PATH = "/v1/admin/reminder-settings"


async def put(s: Seed, hours: Any, headers: dict[str, str] | None = None) -> httpx.Response:
    return await s.client.put(PATH, json={"hours": hours}, headers=headers or s.admin)


async def view(s: Seed) -> dict[str, Any]:
    r = await s.client.get(PATH, headers=s.admin)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


# --------------------------------------------------------------------- the settings
async def test_a_business_starts_with_the_server_default(seeded: Seed) -> None:
    assert await view(seeded) == {"hours": [24, 2], "source": "default", "default_hours": [24, 2]}


async def test_a_business_can_choose_its_own_lead_times_and_go_back(seeded: Seed) -> None:
    r = await put(seeded, [2, 48, 2, 6])
    assert r.status_code == 200 and r.json() == {"hours": [48, 6, 2], "source": "custom", "default_hours": [24, 2]}  # sorted, longest first, no duplicates
    assert await view(seeded) == r.json()
    off = await put(seeded, [])
    assert off.json() == {"hours": [], "source": "custom", "default_hours": [24, 2]}  # none at all is a choice too
    reset = await seeded.client.delete(PATH, headers=seeded.admin)
    assert reset.status_code == 200 and reset.json() == {"hours": [24, 2], "source": "default", "default_hours": [24, 2]}
    assert await view(seeded) == reset.json()


async def test_the_default_shown_follows_the_servers_setting(seeded: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "reminder_hours", [3, 72, 3])
    assert await view(seeded) == {"hours": [72, 3], "source": "default", "default_hours": [72, 3]}
    await put(seeded, [5])
    assert await view(seeded) == {"hours": [5], "source": "custom", "default_hours": [72, 3]}


@pytest.mark.parametrize("hours", [[0], [-1], [721], [1, 2, 3, 4, 5, 6], [24.5], ["24"], "24", None, [True], [1, None]])
async def test_unreasonable_lead_times_are_refused(seeded: Seed, hours: Any) -> None:
    assert (await put(seeded, hours)).status_code == 422, hours
    assert (await view(seeded))["source"] == "default"  # and nothing changed


async def test_the_boundaries_are_inclusive(seeded: Seed) -> None:
    assert (await put(seeded, [1, 720])).json()["hours"] == [720, 1]
    assert (await put(seeded, [1, 2, 3, 4, 5])).status_code == 200


async def test_only_the_hours_field_is_accepted(seeded: Seed) -> None:
    for body in ({"hours": [24], "extra": 1}, {}, {"hour": [24]}):
        assert (await seeded.client.put(PATH, json=body, headers=seeded.admin)).status_code == 422, body


async def test_managers_set_them_and_staff_only_nothing(seeded: Seed) -> None:
    manager, staff = await seeded.token("manager"), await seeded.token("staff")
    assert (await put(seeded, [12], manager)).status_code == 200
    assert (await seeded.client.get(PATH, headers=manager)).json()["hours"] == [12]
    for call in (seeded.client.get(PATH, headers=staff), put(seeded, [1], staff), seeded.client.delete(PATH, headers=staff)):
        assert (await call).status_code == 403
    for method in ("GET", "PUT", "DELETE"):
        assert (await seeded.client.request(method, PATH, headers=seeded.pk, json={"hours": [1]} if method == "PUT" else None)).status_code in (401, 403)
    assert (await seeded.client.get(PATH, headers=manager)).json()["hours"] == [12]


async def test_each_business_has_its_own(client: httpx.AsyncClient) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await put(a, [6])
    assert (await view(a))["hours"] == [6] and (await view(b)) == {"hours": [24, 2], "source": "default", "default_hours": [24, 2]}
    await client.delete(PATH, headers=b.admin)  # b resetting does not touch a
    assert (await view(a))["hours"] == [6]


async def test_a_change_is_recorded_in_the_audit_log(seeded: Seed) -> None:
    await put(seeded, [6])
    entries = (await seeded.client.get("/v1/admin/audit-log", params={"action": "reminder-settings"}, headers=seeded.admin)).json()
    assert [e["action"] for e in entries] == ["PUT /v1/admin/reminder-settings"]


# --------------------------------------------------------------------- what the sweep does with them
async def test_a_business_with_its_own_lead_times_is_reminded_at_those_times_only(seeded: Seed, notify: Recorder) -> None:
    await put(seeded, [6])
    bid = await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    assert (await tick(seeded, start - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 0  # the server default does not apply
    assert (await tick(seeded, start - 2 * H + dt.timedelta(minutes=5))).reminders_queued == 0
    assert (await tick(seeded, start - 6 * H + dt.timedelta(minutes=5))).reminders_queued == 1
    assert await reminder_rows(seeded, bid) == [(bid, "6h")]
    (message,) = reminders_sent(notify)
    assert "10:00" in message.body


async def test_a_business_can_switch_reminders_off(seeded: Seed, notify: Recorder) -> None:
    await put(seeded, [])
    await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    for lead in (24, 2):
        assert (await tick(seeded, start - lead * H + dt.timedelta(minutes=5))).reminders_queued == 0
    assert reminders_sent(notify) == [] and await reminder_rows(seeded) == []


async def test_resetting_brings_back_the_default(seeded: Seed, notify: Recorder) -> None:
    await put(seeded, [])
    await seeded.client.delete(PATH, headers=seeded.admin)
    await booked_with_email(seeded, hour=10)
    assert (await tick(seeded, at(seeded.day, 10) - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 1


async def test_businesses_are_reminded_by_their_own_settings_in_the_same_sweep(client: httpx.AsyncClient, notify: Recorder) -> None:
    a, b = await make_seed(client), await make_seed(client)
    await put(a, [48])
    await booked_with_email(a, "+60111110001", "a@example.com", hour=10)
    await booked_with_email(b, "+60111110002", "b@example.com", hour=10)
    start = at(a.day, 10)
    a_report = await tick(a, start - 48 * H + dt.timedelta(minutes=5))
    b_report = await tick(b, start - 48 * H + dt.timedelta(minutes=5))
    assert (a_report.reminders_queued, b_report.reminders_queued) == (1, 0)  # b has the 24 h / 2 h default
    assert [m.recipient for m in reminders_sent(notify)] == ["a@example.com"]
    assert (await tick(b, start - 24 * H + dt.timedelta(minutes=5))).reminders_queued == 1


async def test_changing_the_times_does_not_repeat_a_reminder_already_sent(seeded: Seed, notify: Recorder) -> None:
    bid = await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    await tick(seeded, start - 24 * H + dt.timedelta(minutes=5))
    await put(seeded, [24, 12])  # the 24 h one was already sent
    assert (await tick(seeded, start - 24 * H + dt.timedelta(minutes=10))).reminders_queued == 0
    assert (await tick(seeded, start - 12 * H + dt.timedelta(minutes=5))).reminders_queued == 1
    assert await reminder_rows(seeded, bid) == [(bid, "12h"), (bid, "24h")]
    assert len(reminders_sent(notify)) == 2


async def test_enqueue_due_takes_explicit_lead_times_and_falls_back_to_the_servers(seeded: Seed, notify: Recorder) -> None:
    await booked_with_email(seeded, hour=10)
    start = at(seeded.day, 10)
    async with tenant_session(seeded.tenant_id) as db:
        assert await reminders.enqueue_due(db, seeded.tenant_id, start - 24 * H + dt.timedelta(minutes=5), []) == 0  # an empty list is "none", not "default"
        assert await reminders.enqueue_due(db, seeded.tenant_id, start - 5 * H + dt.timedelta(minutes=5), [5]) == 1
        assert await reminders.enqueue_due(db, seeded.tenant_id, start - 24 * H + dt.timedelta(minutes=5), None) == 1
        assert await reminders.enqueue_due(db, seeded.tenant_id, start - 2 * H + dt.timedelta(minutes=5)) == 1


async def test_the_column_is_null_until_a_business_chooses(seeded: Seed) -> None:
    async with tenant_session(seeded.tenant_id) as db:
        assert (await db.execute(text("SELECT reminder_hours FROM tenants WHERE id = :t"), {"t": seeded.tenant_id})).scalar_one() is None
    await put(seeded, [])
    async with tenant_session(seeded.tenant_id) as db:
        assert (await db.execute(text("SELECT reminder_hours FROM tenants WHERE id = :t"), {"t": seeded.tenant_id})).scalar_one() == []
    await seeded.client.delete(PATH, headers=seeded.admin)
    async with tenant_session(seeded.tenant_id) as db:
        assert (await db.execute(text("SELECT reminder_hours FROM tenants WHERE id = :t"), {"t": seeded.tenant_id})).scalar_one() is None
    assert uuid.uuid4()
