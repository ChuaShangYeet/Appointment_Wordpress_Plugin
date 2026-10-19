"""Test fixtures.

Pure-logic tests run anywhere. Tests using the ``seeded`` / ``client`` fixtures need a real
PostgreSQL 16 (``btree_gist`` is essential); point ``TEST_DATABASE_URL`` at a *disposable*
database, e.g. ``postgresql+asyncpg://booking:booking@localhost:5432/booking_test``.
The schema is dropped and recreated with Alembic at session start. If the database is
unreachable those tests are skipped, never faked.

PostGIS (on-site radius checks) is not a "trusted" extension, so it can only be created by a
superuser. Set ``TEST_ADMIN_DATABASE_URL`` (same database, superuser) and the schema is rebuilt with
PostGIS enabled; without it, tests marked by the ``postgis`` fixture are skipped.
"""
from __future__ import annotations

import asyncio
import base64
import datetime as dt
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import pytest_asyncio

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "postgresql+asyncpg://booking:booking@localhost:5432/booking_test")
TEST_ADMIN_DB_URL = os.environ.get("TEST_ADMIN_DATABASE_URL")
os.environ["DATABASE_URL"] = TEST_DB_URL
os.environ.setdefault("JWT_SECRET", "test-jwt-secret-0123456789-abcdefghij")
os.environ.setdefault("PDPA_ENCRYPTION_KEY", base64.b64encode(bytes(range(32))).decode())
os.environ.setdefault("PLATFORM_ADMIN_TOKEN", "test-platform-token-0123")
os.environ.setdefault("ENVIRONMENT", "test")
# no Redis in the test environment: the availability cache is off unless a test turns it on (with fakeredis)
os.environ.setdefault("AVAILABILITY_CACHE_TTL_SECONDS", "0")
# every test shares one client address: rate limiting is off unless a test turns it on (with fakeredis)
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from app.core.database import dispose_engine, tenant_session  # noqa: E402
from app.models import BookingAllocation, Resource  # noqa: E402

TZ = ZoneInfo("Asia/Kuala_Lumpur")
PLATFORM = {"X-Platform-Token": os.environ["PLATFORM_ADMIN_TOKEN"]}


@pytest_asyncio.fixture(scope="session")
async def database() -> AsyncIterator[str]:
    """Recreate the schema via Alembic; skip dependent tests if PostgreSQL is not reachable."""
    engine = create_async_engine(TEST_ADMIN_DB_URL or TEST_DB_URL, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            if TEST_ADMIN_DB_URL:
                # rebuilt by a superuser so PostGIS can be created; the app role owns the schema
                from sqlalchemy.engine import make_url

                owner = make_url(TEST_DB_URL).username
                await conn.execute(text(f'CREATE SCHEMA public AUTHORIZATION "{owner}"'))
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS postgis"))
            else:
                await conn.execute(text("CREATE SCHEMA public"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"PostgreSQL not reachable at TEST_DATABASE_URL ({exc.__class__.__name__})")
    await engine.dispose()

    from alembic import command
    from alembic.config import Config

    # env.py drives its own event loop, so run it off-thread.
    await asyncio.to_thread(command.upgrade, Config("alembic.ini"), "head")
    yield TEST_DB_URL
    await dispose_engine()


@pytest_asyncio.fixture(scope="session")
async def postgis(database: str) -> None:
    """Depend on this from tests that need PostGIS; they are skipped where it is not enabled."""
    from app.core.database import get_sessionmaker

    async with get_sessionmaker()() as session:
        try:
            await session.execute(text("SELECT postgis_version()"))
        except Exception:  # noqa: BLE001
            pytest.skip("PostGIS is not enabled in the test database (set TEST_ADMIN_DATABASE_URL)")


@pytest_asyncio.fixture(scope="session")
async def client(database: str) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _next_day(days: int = 3) -> dt.date:
    return (dt.datetime.now(TZ) + dt.timedelta(days=days)).date()


def at(day: dt.date, hh: int, mm: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, dt.time(hh, mm), tzinfo=TZ)


async def set_zone(tenant_id: uuid.UUID, timezone: str, *, country: str | None = None) -> None:
    """Put a business in a time zone (and country) directly, bypassing the sign-up rules.

    Sign-up only offers a country its own zones; the engine itself works in any zone, and some tests need one the three countries do not have
    (New York, far-eastern Kiritimati) or an existing business in another country.
    """
    from sqlalchemy import update

    from app.core import countries
    from app.core.database import tenant_session
    from app.models import Tenant

    values: dict[str, str] = {"timezone": timezone}
    if country is not None:
        values.update(country=country, phone_country_code=countries.get(country).calling_code)
    async with tenant_session(None) as s:
        await s.execute(update(Tenant).where(Tenant.id == tenant_id).values(**values))


class Recorder:
    """A delivery provider that remembers what it was asked to send, and can be told to fail."""

    name = "recorder"

    def __init__(self) -> None:
        self.sent: list[Any] = []
        #: channels that currently fail, and how: ``{"email": RuntimeError("boom")}``
        self.failing: dict[str, Exception] = {}

    async def send(self, message: Any) -> str | None:
        if message.channel in self.failing:
            raise self.failing[message.channel]
        self.sent.append(message)
        return f"msg-{len(self.sent)}"

    def to(self, channel: str) -> list[Any]:
        return [m for m in self.sent if m.channel == channel]


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Email, SMS and WhatsApp all go to one recorder for the length of the test (nothing is registered otherwise)."""
    from app.services import delivery

    rec = Recorder()
    monkeypatch.setattr(delivery, "_providers", {"email": rec, "sms": rec, "whatsapp": rec})
    return rec


class Seed:
    """A tenant with an admin token, one 60-minute service and helper factories."""

    def __init__(self, client: httpx.AsyncClient, data: dict[str, Any]) -> None:
        self.client = client
        self.tenant_id: uuid.UUID = uuid.UUID(data["tenant_id"])
        self.pk = {"X-Site-Key": data["publishable_key"]}
        self.sk = {"X-API-Key": data["secret_key"]}
        self.webhook_secret: str = data["webhook_secret"]
        self.admin: dict[str, str] = {}
        self.service_id: uuid.UUID
        self.day = _next_day()

    async def token(self, role: str = "owner", resource_id: uuid.UUID | None = None) -> dict[str, str]:
        body: dict[str, Any] = {"user_id": "wp-1", "role": role}
        if resource_id:
            body["resource_id"] = str(resource_id)
        r = await self.client.post("/v1/auth/token", json=body, headers=self.sk)
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['access_token']}"}

    async def add_staff(self, name: str, *, shifts: bool = True) -> uuid.UUID:
        r = await self.client.post("/v1/admin/resources", json={"type": "staff", "name": name}, headers=self.admin)
        assert r.status_code == 201, r.text
        rid = uuid.UUID(r.json()["id"])
        if shifts:
            body = [
                {"weekday": d, "start_time": "09:00:00", "end_time": "18:00:00",
                 "break_start": "13:00:00", "break_end": "14:00:00"}
                for d in range(7)
            ]
            r = await self.client.put(f"/v1/admin/resources/{rid}/shifts", json=body, headers=self.admin)
            assert r.status_code == 200, r.text
        return rid

    def booking_body(self, start: dt.datetime, resource_id: uuid.UUID | None = None, **extra: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "service_ids": [str(self.service_id)], "start_at": start.isoformat(),
            "customer": {"name": "Aisha", "phone": f"+6012{uuid.uuid4().int % 10**7:07d}"},
        }
        if resource_id:
            body["resource_id"] = str(resource_id)
        body.update(extra)
        return body


async def make_seed(client: httpx.AsyncClient, *, default_status: str = "confirmed") -> Seed:
    r = await client.post(
        "/v1/auth/tenants", headers=PLATFORM,
        json={"name": f"Spa {uuid.uuid4().hex[:6]}", "business_type": "spa", "timezone": "Asia/Kuala_Lumpur"},
    )
    assert r.status_code == 201, r.text
    seed = Seed(client, r.json())
    seed.admin = await seed.token("owner")
    r = await client.post(
        "/v1/admin/policies", headers=seed.admin,
        json={"name": "Default", "default_status": default_status, "min_notice_booking": 60,
              "min_notice_cancel": 86400, "booking_window": 60 * 86400, "pending_timeout": 1800,
              "make_default": True},
    )
    assert r.status_code == 201, r.text
    r = await client.post(
        "/v1/admin/services", headers=seed.admin,
        json={"name": "Massage", "duration_minutes": 60, "buffer_minutes": 0, "price": "120.00"},
    )
    assert r.status_code == 201, r.text
    seed.service_id = uuid.UUID(r.json()["id"])
    return seed


@pytest_asyncio.fixture
async def seeded(client: httpx.AsyncClient) -> Seed:
    return await make_seed(client)


async def allocations(g: Seed, booking_id: str) -> list[tuple[str, str, str, str, str]]:
    """(resource type, resource name, start, end, state) per allocation row, ordered by start then type."""
    async with tenant_session(g.tenant_id) as s:
        rows = (await s.execute(
            select(Resource.type, Resource.name, BookingAllocation.period, BookingAllocation.state)
            .join(BookingAllocation, BookingAllocation.resource_id == Resource.id)
            .where(BookingAllocation.booking_id == uuid.UUID(booking_id))
        )).all()
    fmt = lambda d: d.astimezone(TZ).strftime("%H:%M")  # noqa: E731
    return sorted((t, n, fmt(p.lower), fmt(p.upper), st) for t, n, p, st in rows)


@pytest.fixture(autouse=True)
def no_status_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests mark tomorrow's bookings in progress, completed or no-show without waiting for tomorrow. The rule that forbids it (``booking.ensure_status_time``) is switched off here
    and put back by ``tests/test_status_timing.py``, which tests it."""
    from app.services import booking as booking_module

    monkeypatch.setattr(booking_module, "ensure_status_time", lambda booking, new_status, tenant, now: None)
