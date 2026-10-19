"""Which role each administrative route needs, and which appointments a staff member may touch (the admin route role review of 2026-10-12).

The blueprint's roles: **Owner** - everything, including API keys and integrations; **Manager** - settings, services, resources, all bookings; **Staff** - own schedule,
own appointments, status updates, upsells. Three layers are pinned here:

1. ``MATRIX`` is the lowest role every role-gated route accepts. The test reads the live app and compares, both ways: a route added without a row here fails, and so
   does a route whose role changed. (``test_security_review`` already proves a role is needed; this proves *which*.)
2. Every route is then called with a token of each role: below its minimum it must answer 403, at or above it must get past the check (anything but 401 / 403).
3. A staff member tied to one resource (a stylist, a mechanic) may work only on appointments allocated to them; managers, owners and staff tied to nobody (a receptionist)
   may work on any. Before this review five routes forgot that rule.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute

from tests.conftest import Seed, at, make_seed
from tests.test_salon import Salon, make_salon, sbook

STAFF = (
    "GET /v1/admin/bookings", "GET /v1/admin/bookings/{booking_id}", "PATCH /v1/admin/bookings/{booking_id}/actual-duration",
    "POST /v1/admin/bookings/{booking_id}/follow-up", "GET /v1/admin/bookings/{booking_id}/history", "POST /v1/admin/bookings/{booking_id}/items",
    "PATCH /v1/admin/bookings/{booking_id}/meeting-link", "GET /v1/admin/bookings/{booking_id}/parts", "PUT /v1/admin/bookings/{booking_id}/parts",
    "POST /v1/admin/bookings/{booking_id}/resume", "PATCH /v1/admin/bookings/{booking_id}/status", "GET /v1/admin/days-off", "GET /v1/admin/resources",
    "GET /v1/admin/resources/{resource_id}/shifts",
)
MANAGER = (
    "GET /v1/admin/site",
    "POST /v1/admin/bookings/{booking_id}/clear-flags", "POST /v1/admin/bookings/{booking_id}/reschedule", "POST /v1/admin/bookings/{booking_id}/resolve-conflict",
    "GET /v1/admin/business-location", "PUT /v1/admin/business-location", "GET /v1/admin/business-profile", "PUT /v1/admin/business-profile",
    "GET /v1/admin/business-settings", "GET /v1/admin/calendar/connections", "POST /v1/admin/days-off", "DELETE /v1/admin/days-off/{day_off_id}",
    "GET /v1/admin/notification-placeholders", "GET /v1/admin/notification-templates", "DELETE /v1/admin/notification-templates/{event}/{channel}",
    "PUT /v1/admin/notification-templates/{event}/{channel}", "GET /v1/admin/notifications", "PUT /v1/admin/operating-hours", "GET /v1/admin/plan",
    "GET /v1/admin/policies", "POST /v1/admin/policies", "GET /v1/admin/products", "POST /v1/admin/products", "PUT /v1/admin/products/{product_id}",
    "DELETE /v1/admin/reminder-settings", "GET /v1/admin/reminder-settings", "PUT /v1/admin/reminder-settings", "POST /v1/admin/resources",
    "PATCH /v1/admin/resources/{resource_id}", "PUT /v1/admin/resources/{resource_id}/shifts", "GET /v1/admin/reviews", "PATCH /v1/admin/reviews/{review_id}",
    "GET /v1/admin/services", "POST /v1/admin/services", "PATCH /v1/admin/services/{service_id}", "PUT /v1/admin/services/{service_id}/parts",
    "GET /v1/admin/supported-vehicles", "POST /v1/admin/supported-vehicles", "DELETE /v1/admin/supported-vehicles/{vehicle_id}", "GET /v1/admin/table-open-rules",
    "PUT /v1/admin/table-open-rules", "GET /v1/admin/time-slot-settings", "PUT /v1/admin/time-slot-settings", "GET /v1/admin/users", "GET /v1/admin/webhook-events",
    "GET /v1/admin/webhooks", "GET /v1/admin/webhooks/{webhook_id}/deliveries",
)
OWNER = (
    "PUT /v1/admin/site",
    "GET /v1/admin/audit-log", "PUT /v1/admin/business-settings", "POST /v1/admin/calendar/connect", "DELETE /v1/admin/calendar/connections/{connection_id}",
    "PATCH /v1/admin/calendar/connections/{connection_id}", "POST /v1/admin/calendar/connections/{connection_id}/resync", "GET /v1/admin/data-requests",
    "POST /v1/admin/data-requests/{request_id}/complete", "GET /v1/admin/data-requests/{request_id}/export", "POST /v1/admin/data-requests/{request_id}/reject",
    "PATCH /v1/admin/users/{user_id}", "POST /v1/admin/webhooks", "DELETE /v1/admin/webhooks/{webhook_id}", "PATCH /v1/admin/webhooks/{webhook_id}",
    "POST /v1/admin/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver", "POST /v1/admin/webhooks/{webhook_id}/rotate-secret",
    "POST /v1/admin/webhooks/{webhook_id}/test", "GET /v1/auth/api-keys", "POST /v1/auth/api-keys", "POST /v1/auth/api-keys/recover",
    "DELETE /v1/auth/api-keys/{key_id}",
)
MATRIX: dict[tuple[str, str], str] = {
    **{tuple(r.split(" ", 1)): "staff" for r in STAFF},  # type: ignore[misc]
    **{tuple(r.split(" ", 1)): "manager" for r in MANAGER},  # type: ignore[misc]
    **{tuple(r.split(" ", 1)): "owner" for r in OWNER},  # type: ignore[misc]
}
RANK = {"staff": 1, "manager": 2, "owner": 3}


# --------------------------------------------------------------------- 1. what the app demands, read from the app
def _calls(dependant: Any) -> list[Any]:
    out = []
    for d in dependant.dependencies:
        out.append(d.call)
        out.extend(_calls(d))
    return out


def _minimum(call: Any) -> str | None:
    if getattr(call, "__qualname__", "") != "require_role.<locals>.dep":
        return None
    cells = dict(zip(call.__code__.co_freevars, (c.cell_contents for c in call.__closure__ or ()), strict=True))
    return str(cells["minimum"])


def _walk(routes: list[Any], prefix: str, inherited: list[Any], out: dict[tuple[str, str], str]) -> None:
    for r in routes:
        if isinstance(r, APIRoute):
            roles = [m for m in (_minimum(c) for c in [*inherited, *_calls(r.dependant)]) if m]
            if roles:
                for method in r.methods - {"HEAD", "OPTIONS"}:
                    out[(method, prefix + r.path)] = max(roles, key=lambda n: RANK[n])
        elif hasattr(r, "include_context"):  # FastAPI nests included routers: the prefix and the router's own dependencies live on the context
            ctx = r.include_context
            _walk(r.original_router.routes, prefix + ctx.prefix, [*inherited, *(d.dependency for d in ctx.dependencies)], out)


def required_roles() -> dict[tuple[str, str], str]:
    from app.main import app

    found: dict[tuple[str, str], str] = {}
    _walk(app.routes, "", [], found)
    return found


def test_the_role_every_route_demands_is_the_one_written_down() -> None:
    found = required_roles()
    assert len(found) > 80
    assert {k: v for k, v in found.items() if MATRIX.get(k) != v} == {}, "a route needs a different role than the matrix says (or is not in the matrix)"
    assert [k for k in MATRIX if k not in found] == [], "the matrix names a route that no longer exists or no longer needs a role"


def test_every_administrative_route_needs_a_role_and_the_matrix_agrees_with_the_blueprint() -> None:
    found = required_roles()
    from tests.test_security_review import operations

    admin = [(m, p) for m, p in operations() if p.startswith("/v1/admin/")]
    unguarded = [
        (m, p) for m, p in admin
        if not any(re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", pat), p) and mm == m for (mm, pat) in found)
    ]
    assert unguarded == []
    owner_only_areas = ("audit-log", "data-requests", "webhooks", "calendar/connect", "calendar/connections/", "business-settings", "users/")
    for (method, path), role in MATRIX.items():
        if method == "GET" and role == "owner" and path != "/v1/admin/audit-log" and "data-requests" not in path and "/v1/auth/api-keys" not in path:
            raise AssertionError(f"{method} {path}: only the audit log, privacy requests and API keys are owner-only reads")
        if method != "GET" and role == "staff":
            assert "/bookings/" in path, f"{method} {path}: staff may change only the appointment they are working on"
        if any(area in path for area in owner_only_areas) and method != "GET":
            assert role == "owner", f"{method} {path}: changing settings, integrations or users is the owner's"


def test_rescheduling_and_review_are_for_managers_and_staff_cannot_read_the_whole_setup() -> None:
    assert MATRIX[("POST", "/v1/admin/bookings/{booking_id}/reschedule")] == "manager"
    assert MATRIX[("POST", "/v1/admin/bookings/{booking_id}/clear-flags")] == "manager"
    assert MATRIX[("POST", "/v1/admin/bookings/{booking_id}/resolve-conflict")] == "manager"
    for path in ("services", "policies", "products", "notifications", "notification-templates", "reviews", "users", "plan", "webhooks"):
        assert MATRIX[("GET", f"/v1/admin/{path}")] != "staff", path


# --------------------------------------------------------------------- 2. what the routes really answer to each role
@pytest.fixture
async def tokens(client: httpx.AsyncClient) -> dict[str, dict[str, str]]:
    s = await make_seed(client)
    return {"seed": s, "owner": s.admin, "manager": await s.token("manager"), "staff": await s.token("staff")}  # type: ignore[dict-item]


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", lambda _: str(uuid.uuid4()), path)


async def call(s: Seed, method: str, path: str, headers: dict[str, str]) -> int:
    return (await s.client.request(method, concrete(path), headers=headers, json={} if method in ("POST", "PUT", "PATCH") else None)).status_code


async def test_a_token_below_the_minimum_is_refused_and_one_at_or_above_it_gets_past_the_check(tokens: dict[str, Any]) -> None:
    s: Seed = tokens["seed"]
    wrong: list[str] = []
    for (method, path), minimum in sorted(MATRIX.items()):
        for role in ("staff", "manager", "owner"):
            status = await call(s, method, path, tokens[role])
            allowed = RANK[role] >= RANK[minimum]
            if allowed and status in (401, 403):
                wrong.append(f"{role} {method} {path} -> {status} (should pass the role check)")
            if not allowed and status != 403:
                wrong.append(f"{role} {method} {path} -> {status} (should be refused: needs {minimum})")
    assert wrong == []


# --------------------------------------------------------------------- 3. a staff member tied to a resource works on their own appointments only
async def two_stylists_one_booking(client: httpx.AsyncClient) -> tuple[Salon, str, dict[str, dict[str, str]]]:
    s = await make_salon(client)
    made = await sbook(s, at(s.day, 10), ["cut"], stylist="Mei")
    assert made.status_code == 201, made.text
    bid = str(made.json()["id"])
    return s, bid, {
        "mei": await s.token("staff", s.stylists["Mei"]), "lin": await s.token("staff", s.stylists["Lin"]),
        "reception": await s.token("staff"), "manager": await s.token("manager"), "owner": s.admin,
    }


def booking_calls(s: Salon, bid: str) -> list[tuple[str, str, dict[str, Any] | None]]:
    start = at(s.day, 15).isoformat()
    base = f"/v1/admin/bookings/{bid}"
    return [
        ("GET", base, None),
        ("GET", f"{base}/history", None),
        ("PATCH", f"{base}/status", {"status": "in-progress"}),
        ("POST", f"{base}/items", {"service_ids": [str(s.svc["gel"])], "products": []}),
        ("GET", f"{base}/parts", None),
        ("PUT", f"{base}/parts", {"parts": []}),
        ("POST", f"{base}/resume", {"start_at": start}),
        ("POST", f"{base}/follow-up", {"service_id": str(s.svc["cut"]), "start_at": start}),
        ("PATCH", f"{base}/actual-duration", {"minutes": 50}),
        ("PATCH", f"{base}/meeting-link", {"url": "https://meet.example.com/room"}),
    ]


async def test_another_stylists_appointment_is_closed_to_a_staff_member_tied_to_a_resource_on_every_route(client: httpx.AsyncClient) -> None:
    s, bid, tokens = await two_stylists_one_booking(client)
    wrong: list[str] = []
    for method, path, body in booking_calls(s, bid):
        status = (await client.request(method, path, headers=tokens["lin"], json=body)).status_code
        if status != 403:
            wrong.append(f"{method} {path.removeprefix(f'/v1/admin/bookings/{bid}') or '/'} -> {status} for Lin on Mei's booking")
    assert wrong == []
    assert (await client.request("GET", f"/v1/admin/bookings/{bid}", headers=tokens["lin"])).json()["detail"] == "Staff can only see their own appointments"
    assert (await client.patch(f"/v1/admin/bookings/{bid}/status", headers=tokens["lin"], json={"status": "in-progress"})).json()["detail"] == "Staff can only update their own appointments"
    assert (await client.get(f"/v1/admin/bookings/{bid}/history", headers=tokens["lin"])).json()["detail"] == "Staff can only see their own appointments"


async def test_the_stylist_whose_appointment_it_is_and_everyone_above_or_outside_the_tie_are_not_stopped(client: httpx.AsyncClient) -> None:
    s, bid, tokens = await two_stylists_one_booking(client)
    wrong: list[str] = []
    for who in ("mei", "reception", "manager", "owner"):
        for method, path, body in booking_calls(s, bid):
            status = (await client.request(method, path, headers=tokens[who], json=body)).status_code
            if status in (401, 403):
                wrong.append(f"{who} {method} {path.removeprefix(f'/v1/admin/bookings/{bid}') or '/'} -> {status}")
    assert wrong == []


async def test_the_list_shows_a_tied_stylist_only_their_own_and_the_rest_everything(client: httpx.AsyncClient) -> None:
    s, bid, tokens = await two_stylists_one_booking(client)
    other = await sbook(s, at(s.day, 11), ["cut"], stylist="Lin")
    assert other.status_code == 201, other.text

    async def ids(who: str, **params: Any) -> set[str]:
        r = await client.get("/v1/admin/bookings", headers=tokens[who], params=params)
        assert r.status_code == 200, r.text
        return {b["id"] for b in r.json()}

    assert await ids("mei") == {bid} and await ids("lin") == {str(other.json()["id"])}
    assert await ids("mei", resource_id=str(s.stylists["Lin"])) == {bid}  # asking for someone else's list changes nothing
    everything = {bid, str(other.json()["id"])}
    assert await ids("reception") == await ids("manager") == await ids("owner") == everything


async def test_a_booking_that_does_not_exist_is_not_found_for_everyone(client: httpx.AsyncClient) -> None:
    s, _, tokens = await two_stylists_one_booking(client)
    ghost = str(uuid.uuid4())
    for who in ("mei", "reception", "manager", "owner"):
        for method, path, body in booking_calls(s, ghost):
            status = (await client.request(method, path, headers=tokens[who], json=body)).status_code
            assert status in (404, 409, 422), f"{who} {method} {path} -> {status}"


async def test_a_staff_member_cannot_widen_their_own_tie_by_asking_for_another_resource(client: httpx.AsyncClient) -> None:
    """The tie is in the signed token (``rid``); nothing in the request can change it."""
    s, bid, tokens = await two_stylists_one_booking(client)
    r = await client.get(f"/v1/admin/bookings/{bid}", headers=tokens["lin"], params={"resource_id": str(s.stylists["Mei"])})
    assert r.status_code == 403
    listed = await client.get("/v1/admin/bookings", headers=tokens["lin"], params={"resource_id": str(s.stylists["Mei"])})
    assert listed.json() == []


# --------------------------------------------------------------------- 4. who booked: every role that may see the appointment sees the customer's name, phone and email
async def test_every_role_that_may_see_an_appointment_sees_who_booked_it_and_a_colleague_does_not(client: httpx.AsyncClient) -> None:
    """Decided 2026-10-15: name, phone and the booking's email for every admin role (staff call a customer who is late or missing). *Contact details of other staff are hidden
    from staff* is about colleagues, not customers. A stylist tied to a resource still sees nobody else's customers, because they do not see the appointment at all."""
    s, bid, tokens = await two_stylists_one_booking(client)
    phone = "+60123334444"
    mine = await sbook(s, at(s.day, 12), ["cut"], stylist="Mei", customer={"name": "Aisha", "phone": phone, "email": "aisha@example.com"})
    assert mine.status_code == 201, mine.text
    mid = str(mine.json()["id"])
    expected = {"customer_name": "Aisha", "customer_phone": phone, "contact_email": "aisha@example.com"}
    for who in ("mei", "reception", "manager", "owner"):
        rows = {b["id"]: b for b in (await client.get("/v1/admin/bookings", headers=tokens[who])).json()}
        one = (await client.get(f"/v1/admin/bookings/{mid}", headers=tokens[who])).json()
        assert {k: rows[mid][k] for k in expected} == expected == {k: one[k] for k in expected}, who
        assert rows[bid]["customer_name"] == "Aisha" and rows[bid]["customer_phone"].startswith("+6012"), who
    lin = (await client.get("/v1/admin/bookings", headers=tokens["lin"])).json()
    assert lin == [] and phone not in str(lin)
    assert (await client.get(f"/v1/admin/bookings/{mid}", headers=tokens["lin"])).status_code == 403
