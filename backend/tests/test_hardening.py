"""Start-up safety checks (app.core.hardening), /readyz, and the split between the migration role and the application role."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core import hardening
from app.core.config import Settings
from app.core.hardening import StartupRefused, database_problems, enforce, settings_problems
from tests.conftest import TEST_ADMIN_DB_URL, TEST_DB_URL

KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8="  # bytes 0..31
GOOD = {
    "environment": "production", "jwt_secret": "k7Qm-3vXz9LpR2wT8yBn5cDf1HgJ6sAe", "platform_admin_token": "tZ9pLm4QxV7rKc2WbN8d",
    "database_url": "postgresql+asyncpg://booking_app:s3cr3t-Pw@db.internal:5432/booking", "pdpa_encryption_key": KEY,
    "cors_origins": ["https://shop.example.test"], "rate_limit_enabled": True,  # (the test environment switches rate limiting off)
}


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **{**GOOD, **kw})  # type: ignore[arg-type]


# --------------------------------------------------------------------- the settings
def test_sound_settings_have_no_problems() -> None:
    assert settings_problems(settings()) == []


@pytest.mark.parametrize("field", ["jwt_secret", "platform_admin_token"])
@pytest.mark.parametrize("word", ["change-me", "ChangeMe", "change_me", "replace-me", "placeholder", "my-example-secret", "Password"])
def test_placeholder_secrets_are_refused(field: str, word: str) -> None:
    value = (word + "-" + "x" * 40)[:40]
    (problem,) = settings_problems(settings(**{field: value}))
    assert problem == f"{field.upper()} is still a placeholder value"


def test_the_example_database_credentials_are_refused() -> None:
    (problem,) = settings_problems(settings(database_url="postgresql+asyncpg://booking:booking@db:5432/booking"))
    assert "booking:booking" in problem
    assert settings_problems(settings(database_url="postgresql+asyncpg://booking:other@db:5432/booking")) == []  # only the example pair
    assert settings_problems(settings(database_url="postgresql+asyncpg://other:booking@db:5432/booking")) == []


@pytest.mark.parametrize("url", [
    "redis://localhost:6379/0", "redis://127.0.0.1:6379/0", "redis://[::1]:6379/0", "redis://:s3cret@redis:6379/0", "redis://default:s3cret@cache.internal:6379/0",
    "rediss://:s3cret@cache.example.com:6380/0", "unix:///var/run/redis.sock",
])
def test_redis_needs_a_password_unless_it_is_on_this_machine(url: str) -> None:
    assert settings_problems(settings(redis_url=url)) == []


@pytest.mark.parametrize("url", ["redis://redis:6379/0", "redis://cache.internal:6379/0", "rediss://cache.example.com:6380/0", "redis://10.0.0.5/0", "redis://:@redis:6379/0"])
def test_a_redis_without_a_password_elsewhere_is_refused(url: str) -> None:
    (problem,) = settings_problems(settings(redis_url=url))
    assert problem.startswith("REDIS_URL has no password") and "s3cret" not in problem


async def test_startup_refuses_a_redis_without_a_password_and_allows_one_with(app_engine: AsyncEngine) -> None:
    with pytest.raises(StartupRefused, match="REDIS_URL has no password"):
        await enforce(settings(redis_url="redis://redis:6379/0"), app_engine)
    await enforce(settings(redis_url="redis://:s3cret@redis:6379/0"), app_engine)
    await enforce(settings(redis_url="redis://redis:6379/0", environment="development"), NeverUsed())  # type: ignore[arg-type]


def test_the_compose_file_gives_redis_a_password_and_hands_it_to_the_services() -> None:
    text_ = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"--requirepass", "${REDIS_PASSWORD:?set REDIS_PASSWORD}"' in text_
    assert text_.count("REDIS_URL: redis://:${REDIS_PASSWORD}@redis:6379/0") == 2 and "REDIS_URL: redis://redis" not in text_  # api and worker, both with it


def test_unsafe_switches_are_refused() -> None:
    assert [p.split(" ")[0] for p in settings_problems(settings(cors_origins=["https://a.test", "*"]))] == ["CORS_ORIGINS"]
    assert [p.split("=")[0] for p in settings_problems(settings(webhook_allow_insecure=True))] == ["WEBHOOK_ALLOW_INSECURE"]
    assert [p.split(":")[0] for p in settings_problems(settings(rate_limit_enabled=False))] == ["RATE_LIMIT_ENABLED=false"]
    assert len(settings_problems(settings(cors_origins=["*"], webhook_allow_insecure=True, rate_limit_enabled=False, jwt_secret="change-me-" + "x" * 30))) == 4


@pytest.mark.parametrize("bad", ["change-me", "not base64 !!", "AAECAwQ=", "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8AAAAA", KEY[:10] + "!" + KEY[10:], KEY[:20] + "\x0b" + KEY[20:]])  # stray characters inside a valid key are dropped by a lenient decoder
def test_the_encryption_key_must_be_32_bytes_of_base64(bad: str) -> None:
    with pytest.raises(ValidationError, match="PDPA_ENCRYPTION_KEY"):
        settings(pdpa_encryption_key=bad)
    assert settings(pdpa_encryption_key=KEY).pdpa_encryption_key == KEY


# --------------------------------------------------------------------- the database role
@pytest.fixture
async def app_engine(database: str) -> AsyncIterator[AsyncEngine]:
    """Connected as the test suite's own role: a normal (non-super) user that owns the tables, like a naive single-role deployment."""
    engine = create_async_engine(TEST_DB_URL)
    yield engine
    await engine.dispose()


@pytest.fixture
async def admin_engine(database: str) -> AsyncIterator[AsyncEngine]:
    if not TEST_ADMIN_DB_URL:
        pytest.skip("needs TEST_ADMIN_DATABASE_URL (a superuser)")
    engine = create_async_engine(TEST_ADMIN_DB_URL, isolation_level="AUTOCOMMIT")
    yield engine
    await engine.dispose()


async def problems_of(engine: AsyncEngine) -> tuple[list[str], list[str]]:
    async with engine.connect() as conn:
        return await database_problems(conn)


async def test_a_normal_role_is_accepted_but_one_that_owns_the_tables_is_warned_about(app_engine: AsyncEngine) -> None:
    errors, warnings = await problems_of(app_engine)
    assert errors == []
    (warning,) = warnings
    assert "owns the tables" in warning and "MIGRATION_DATABASE_URL" in warning


async def test_a_superuser_is_refused(admin_engine: AsyncEngine) -> None:
    errors, warnings = await problems_of(admin_engine)
    assert any("superuser" in e and "'postgres'" in e for e in errors)
    assert any(e.startswith("Row-Level Security is not in force for") for e in errors)  # the database confirms the policies do not apply
    assert warnings == []  # (the owner warning is for roles that could be fixed by splitting; a superuser is simply wrong)


async def test_a_role_with_bypassrls_is_refused(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        await conn.execute(text("DROP ROLE IF EXISTS rls_probe"))
        await conn.execute(text("CREATE ROLE rls_probe LOGIN BYPASSRLS"))
    probe = create_async_engine(make_url(TEST_DB_URL).set(username="rls_probe", password=None))
    try:
        errors, _ = await problems_of(probe)
        assert any("'rls_probe' has BYPASSRLS" in e for e in errors)
        assert any(e.startswith("Row-Level Security is not in force") for e in errors)
    finally:
        await probe.dispose()
        async with admin_engine.connect() as conn:
            await conn.execute(text("DROP ROLE IF EXISTS rls_probe"))


async def test_a_tenant_table_without_enforced_policies_is_found(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS zz_probe"))
        await conn.execute(text("CREATE TABLE zz_probe (tenant_id uuid)"))
        await conn.commit()
    try:
        errors, _ = await problems_of(app_engine)
        assert errors == ["Row-Level Security is not in force for 1 tenant table(s) for this role: zz_probe"]
        async with app_engine.connect() as conn:
            await conn.execute(text("ALTER TABLE zz_probe ENABLE ROW LEVEL SECURITY"))  # on, but the owner is exempt unless it is forced
            await conn.commit()
        assert (await problems_of(app_engine))[0] == errors
        async with app_engine.connect() as conn:
            await conn.execute(text("ALTER TABLE zz_probe FORCE ROW LEVEL SECURITY"))
            await conn.commit()
        assert (await problems_of(app_engine))[0] == []
    finally:
        async with app_engine.connect() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS zz_probe"))
            await conn.commit()


async def test_many_unprotected_tables_are_summarised(app_engine: AsyncEngine) -> None:
    names = [f"zz_probe_{i}" for i in range(7)]
    async with app_engine.connect() as conn:
        for n in names:
            await conn.execute(text(f"CREATE TABLE {n} (tenant_id uuid)"))
        await conn.commit()
    try:
        (error,) = (await problems_of(app_engine))[0]
        assert error == "Row-Level Security is not in force for 7 tenant table(s) for this role: zz_probe_0, zz_probe_1, zz_probe_2, zz_probe_3, zz_probe_4 and 2 more"
    finally:
        async with app_engine.connect() as conn:
            for n in names:
                await conn.execute(text(f"DROP TABLE IF EXISTS {n}"))
            await conn.commit()


async def test_the_identity_tables_are_exempt_and_tables_without_tenant_id_are_ignored(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        await conn.execute(text("CREATE TABLE zz_plain (id int)"))
        await conn.commit()
    try:
        assert (await problems_of(app_engine))[0] == []  # sites / api_keys carry tenant_id but have no policy by design
    finally:
        async with app_engine.connect() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS zz_plain"))
            await conn.commit()


async def test_exactly_five_unprotected_tables_are_all_listed_without_a_rest(app_engine: AsyncEngine) -> None:
    names = [f"zz_probe_{i}" for i in range(5)]
    async with app_engine.connect() as conn:
        for n in names:
            await conn.execute(text(f"CREATE TABLE {n} (tenant_id uuid)"))
        await conn.commit()
    try:
        (error,) = (await problems_of(app_engine))[0]
        assert error == "Row-Level Security is not in force for 5 tenant table(s) for this role: zz_probe_0, zz_probe_1, zz_probe_2, zz_probe_3, zz_probe_4"
    finally:
        async with app_engine.connect() as conn:
            for n in names:
                await conn.execute(text(f"DROP TABLE IF EXISTS {n}"))
            await conn.commit()


async def test_views_and_other_schemas_are_not_tenant_tables(app_engine: AsyncEngine) -> None:
    async with app_engine.connect() as conn:
        await conn.execute(text("CREATE TABLE zz_secure (tenant_id uuid)"))
        await conn.execute(text("ALTER TABLE zz_secure ENABLE ROW LEVEL SECURITY"))
        await conn.execute(text("ALTER TABLE zz_secure FORCE ROW LEVEL SECURITY"))
        await conn.execute(text("CREATE VIEW zz_view AS SELECT tenant_id FROM zz_secure"))  # a view shows tenant_id too, but is not a table
        await conn.execute(text("CREATE SCHEMA zz_other"))
        await conn.execute(text("CREATE TABLE zz_other.leaky (tenant_id uuid)"))  # outside the application's schema
        await conn.commit()
    try:
        assert (await problems_of(app_engine))[0] == []
    finally:
        async with app_engine.connect() as conn:
            await conn.execute(text("DROP SCHEMA IF EXISTS zz_other CASCADE"))
            await conn.execute(text("DROP VIEW IF EXISTS zz_view"))
            await conn.execute(text("DROP TABLE IF EXISTS zz_secure"))
            await conn.commit()


async def test_a_role_that_owns_nothing_and_is_bound_by_the_policies_gets_a_clean_bill(admin_engine: AsyncEngine) -> None:
    async with admin_engine.connect() as conn:
        await conn.execute(text("DROP ROLE IF EXISTS rls_limited"))
        await conn.execute(text("CREATE ROLE rls_limited LOGIN NOSUPERUSER NOBYPASSRLS"))
    limited = create_async_engine(make_url(TEST_DB_URL).set(username="rls_limited", password=None))
    try:
        assert await problems_of(limited) == ([], [])  # not the owner: nothing to warn about
    finally:
        await limited.dispose()
        async with admin_engine.connect() as conn:
            await conn.execute(text("DROP ROLE IF EXISTS rls_limited"))


# --------------------------------------------------------------------- enforce()
class NeverUsed:
    def connect(self) -> Any:
        raise AssertionError("the database must not be contacted in development and tests")


@pytest.mark.parametrize("environment", ["development", "test"])
async def test_nothing_is_checked_in_development_and_tests(environment: str) -> None:
    await enforce(settings(environment=environment, jwt_secret="change-me-" + "x" * 30), NeverUsed())  # type: ignore[arg-type]


async def test_production_refuses_a_superuser_and_names_everything_wrong(admin_engine: AsyncEngine) -> None:
    with pytest.raises(StartupRefused) as refused:
        await enforce(settings(jwt_secret="change-me-" + "x" * 30), admin_engine)
    text_ = str(refused.value)
    assert text_.startswith("Refusing to start:") and "JWT_SECRET is still a placeholder" in text_ and "superuser" in text_ and "Row-Level Security is not in force" in text_


async def test_production_accepts_a_sound_setup_and_logs_the_owner_warning(app_engine: AsyncEngine, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.core.hardening"):
        await enforce(settings(), app_engine)
    assert any("owns the tables" in r.getMessage() for r in caplog.records)


async def test_unsafe_startup_can_be_forced_but_says_so_loudly(admin_engine: AsyncEngine, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.CRITICAL, logger="app.core.hardening"):
        await enforce(settings(allow_unsafe_startup=True), admin_engine)
    assert caplog.records and all(r.levelno == logging.CRITICAL and "ALLOW_UNSAFE_STARTUP" in r.getMessage() for r in caplog.records)


async def test_the_api_and_the_worker_run_the_checks_before_anything_else(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main, worker
    from app.services import delivery, outbox

    seen: list[str] = []

    async def spy(s: Any, engine: Any) -> None:
        seen.append("enforce")

    monkeypatch.setattr(hardening, "enforce", spy)
    monkeypatch.setattr(delivery, "configure_providers", lambda s: seen.append("providers"))
    monkeypatch.setattr(delivery, "report_configuration", lambda s: [])
    async with main.lifespan(main.app):
        pass
    assert seen == ["enforce", "providers"]
    seen.clear()

    async def no_sweep(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(worker, "run", no_sweep)
    monkeypatch.setattr(outbox, "set_event_publisher", lambda p: None)
    await worker.amain(["--once"])
    assert seen == ["enforce", "providers"]


async def test_a_refusal_stops_the_api_from_starting(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main

    async def refuse(s: Any, engine: Any) -> None:
        raise StartupRefused("Refusing to start: test")

    monkeypatch.setattr(hardening, "enforce", refuse)
    with pytest.raises(StartupRefused):
        async with main.lifespan(main.app):
            pytest.fail("the application started")


# --------------------------------------------------------------------- /readyz
async def test_readyz_reports_the_database_and_redis(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis.aioredis

    from app.core import redis as redis_module

    monkeypatch.setattr(redis_module, "_redis", fakeredis.aioredis.FakeRedis(decode_responses=True))
    r = await client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"status": "ok", "database": "ok", "redis": "ok"}

    class Down:
        async def ping(self) -> None:
            raise ConnectionError("redis is down")

    monkeypatch.setattr(redis_module, "_redis", Down())
    r = await client.get("/readyz")
    assert r.status_code == 200 and r.json() == {"status": "degraded", "database": "ok", "redis": "down"}  # still serving: the API fails open without it


async def test_readyz_is_503_when_the_database_is_down(client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis.aioredis

    from app import main
    from app.core import redis as redis_module

    monkeypatch.setattr(redis_module, "_redis", fakeredis.aioredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(main, "get_engine", lambda: create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none"))
    r = await client.get("/readyz")
    assert r.status_code == 503 and r.json() == {"status": "unavailable", "database": "down", "redis": "ok"}
    assert "nobody" not in r.text and "127.0.0.1" not in r.text  # nothing about the connection leaks to the caller


async def test_liveness_does_not_touch_anything(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).json() == {"status": "ok"}


# --------------------------------------------------------------------- migrations run as the owner
BACKEND = Path(__file__).resolve().parent.parent


def alembic_current(**env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-m", "alembic", "current"], cwd=BACKEND, capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env},
    )


def test_migrations_use_the_owner_url_when_there_is_one(database: str) -> None:
    unreachable = "postgresql+asyncpg://nobody@127.0.0.1:1/none"
    wrong = alembic_current(DATABASE_URL=TEST_DB_URL, MIGRATION_DATABASE_URL=unreachable)
    assert wrong.returncode != 0  # it tried the migration URL, not DATABASE_URL
    right = alembic_current(DATABASE_URL=unreachable, MIGRATION_DATABASE_URL=TEST_DB_URL)
    assert right.returncode == 0, right.stderr[-500:]
    fallback = alembic_current(DATABASE_URL=TEST_DB_URL, MIGRATION_DATABASE_URL="")
    assert fallback.returncode == 0, fallback.stderr[-500:]
    assert uuid.UUID(int=0)  # (keeps the import used)


# --------------------------------------------------------------------- static scans (bandit, pip-audit), run on 2026-10-13
def test_the_image_never_runs_python_with_assertions_stripped() -> None:
    """``bandit`` B101 (assert) is skipped on the understanding that assertions stay on: a ``-O`` would strip every ``assert`` in ``app/``."""
    import re

    root = Path(__file__).resolve().parent.parent
    for name in ("Dockerfile", "docker-compose.yml", "docker-compose.dev.yml"):
        text_ = (root / name).read_text(encoding="utf-8")
        assert "PYTHONOPTIMIZE" not in text_, name
        assert not re.search(r"\bpython[0-9.]*\s+(?:-\w*O\w*)\b", text_), name
        assert not re.search(r'"-\w*O\w*"', text_), name


def test_bandit_is_configured_to_skip_only_asserts_and_to_leave_the_tests_out() -> None:
    import tomllib

    config = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["bandit"]
    assert config["skips"] == ["B101"] and set(config["exclude_dirs"]) == {"tests", ".venv"}


def test_the_scan_script_runs_both_tools_over_what_a_build_installs_and_what_is_shipped() -> None:
    script = (Path(__file__).resolve().parent.parent / "deploy" / "security_scan.sh").read_text(encoding="utf-8")
    assert "pip_audit -r requirements.txt" in script and "bandit -c pyproject.toml -r app alembic" in script and "set -euo pipefail" in script


@pytest.mark.parametrize(("latitude", "longitude"), [(None, None), (3.1, None), (None, 101.7)])
async def test_a_business_location_without_both_coordinates_never_reaches_the_database(client: httpx.AsyncClient, latitude: float | None, longitude: float | None) -> None:
    """The schema refuses these (422); the handler refuses them too, so that it does not rest on an ``assert`` that ``python -O`` would remove."""
    from fastapi import Response

    from app.api.v1.admin import put_business_location
    from app.core.database import tenant_session
    from app.core.exceptions import ValidationFailed
    from app.models import Tenant
    from app.schemas.scheduling import BusinessLocationIn
    from tests.conftest import make_seed

    s = await make_seed(client)
    r = await client.put("/v1/admin/business-location", json={"latitude": 3.1, "longitude": 101.7, "service_radius_km": 10}, headers=s.admin)
    assert r.status_code == 200
    body = BusinessLocationIn.model_construct(latitude=latitude, longitude=longitude, address=None, service_radius_km=5.0)  # (built without the schema's checks)
    async with tenant_session(s.tenant_id) as db:
        tenant = await db.get(Tenant, s.tenant_id)
        assert tenant is not None
        with pytest.raises(ValidationFailed, match="latitude and longitude"):
            await put_business_location(body, Response(), None, db, tenant)  # type: ignore[arg-type]
    async with tenant_session(s.tenant_id) as db:
        kept = await db.get(Tenant, s.tenant_id)
        assert kept is not None and (kept.latitude, kept.longitude, kept.service_radius_km) == (3.1, 101.7, 10)
