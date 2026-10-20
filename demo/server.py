"""The hosted demo of the JustSimple booking API (for example on Render): the real backend, unchanged, with five sample businesses everyone may use.

    python demo/server.py            (from the repository root; PORT and DATABASE_URL come from the environment)

What it does, in order:
1. reads the settings from the environment (``DATABASE_URL`` as Render gives it, ``postgres://...``, is accepted);
2. makes sure PostGIS and btree_gist exist, then brings the schema up to date with the backend's own migrations;
3. on the first start only, adds the five sample businesses and their bookings through the real API (``localsite/seed.py``) and keeps
   their keys in the table ``demo_state`` (Render's disk is wiped on every deploy; the database is not);
4. serves the API (docs at ``/docs``), the sweep worker, **the demo website at ``/``** (the local test site's pages, ``localsite/web``: booking,
   My bookings, business admin, sign-up, all on the real API), and the demo routes: ``/api`` (a page with the keys and links), ``/demo/info``
   (the same as JSON), ``/demo/messages`` (what the backend "sent": nothing leaves the server, so sign-in codes are read there) and the website's
   own ``/dev/*`` routes (the business list, an admin token per role in place of the WordPress plugin, the inbox).

**This is a public demo:** the keys, including the secret keys, are shown to anyone, and every visitor shares the same data. Never put real
customers or a real business into it. Messages (WhatsApp, SMS, email) are never sent; Redis is an in-memory stand-in, so it runs as ONE instance.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT / "backend"
LOCALSITE = ROOT / "localsite"

log = logging.getLogger("demo")


def say(text: str) -> None:
    print(f"[demo] {text}", flush=True)


def asyncpg_url(url: str) -> str:
    """``postgres://u:p@h/db`` (Render, Heroku) or ``postgresql://`` -> the SQLAlchemy asyncpg form the backend expects."""
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    raise SystemExit("DATABASE_URL must be a PostgreSQL address (postgres://user:password@host:5432/database)")


def plain_url(url: str) -> str:
    """The same address for asyncpg itself (the sample loader's direct connection)."""
    return "postgresql://" + asyncpg_url(url)[len("postgresql+asyncpg://"):]


def environment() -> dict[str, str]:
    """The backend's settings for the demo. Secrets must be set on the host (Render generates them: see render.yaml)."""
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise SystemExit("DATABASE_URL is not set")
    for name in ("JWT_SECRET", "PDPA_ENCRYPTION_KEY", "PLATFORM_ADMIN_TOKEN"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} is not set")
    site = os.environ.get("PUBLIC_URL", "").rstrip("/") or os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/") or "http://localhost:" + os.environ.get("PORT", "8000")
    env = {
        "ENVIRONMENT": os.environ.get("ENVIRONMENT", "staging"),  # (the start-up safety checks run; "development" would skip them)
        "DOCS_ENABLED": "true",
        "DATABASE_URL": asyncpg_url(raw),
        "MIGRATION_DATABASE_URL": asyncpg_url(raw),
        "SELF_SERVICE_SIGNUP": "true",
        "OTP_CHANNEL": "whatsapp",
        "MAGIC_LINK_URL": site + "/demo/link/sign-in/{token}",
        "EMAIL_CONFIRM_URL": site + "/demo/link/confirm-email/{token}",
        "SIGNUP_VERIFY_URL": site + "/demo/link/sign-up/{token}",
        "OWNER_LOGIN_URL": site + "/demo/link/owner/{token}",
        "REVIEW_URL": site + "/demo/link/review/{token}",
        "TERMS_URL": site + "/",
        "TERMS_VERSION": "demo",
        # reminders would use up the free plan's 20 WhatsApp messages a month, and then phone sign-in stops
        "REMINDER_HOURS": "[]",
        "LOG_FORMAT": "text",
        "SWEEP_INTERVAL_SECONDS": "20",
        "SWEEP_GRACE_SECONDS": "20",
        "RATE_LIMIT_TRUSTED_PROXIES": os.environ.get("RATE_LIMIT_TRUSTED_PROXIES", "1"),  # (Render's proxy adds the visitor's address)
    }
    return {**env, **{k: v for k, v in os.environ.items() if k in env and k not in ("DATABASE_URL", "MIGRATION_DATABASE_URL")}}


async def prepare_database(url: str) -> None:
    """PostGIS and btree_gist must exist before the first migration. Render's database user may create them; elsewhere a superuser may have to."""
    import asyncpg

    conn = await asyncpg.connect(plain_url(url))
    try:
        for ext in ("btree_gist", "postgis"):
            try:
                await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
            except asyncpg.PostgresError as exc:
                raise SystemExit(f"Could not create the {ext} extension ({exc}). Create it once as an administrator: CREATE EXTENSION {ext};") from exc
        await conn.execute("CREATE TABLE IF NOT EXISTS demo_state (key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())")
    finally:
        await conn.close()


def migrate(env: dict[str, str]) -> None:
    say("bringing the database schema up to date...")
    done = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env={**os.environ, **env}, capture_output=True, text=True)
    if done.returncode != 0:
        print(done.stdout, done.stderr, sep="\n", file=sys.stderr)
        raise SystemExit("the migrations failed (see above)")


async def load_state(url: str) -> dict[str, Any] | None:
    import asyncpg

    conn = await asyncpg.connect(plain_url(url))
    try:
        raw = await conn.fetchval("SELECT value FROM demo_state WHERE key = 'sample'")
    finally:
        await conn.close()
    return json.loads(raw) if raw else None


class DbStore:
    """The website's list of businesses and their keys, kept in ``demo_state`` (Render wipes the disk on every deploy; the database stays)."""

    hosted = True

    def __init__(self, url: str, data: dict[str, Any]) -> None:
        self.url, self.data = url, data

    async def load(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.data))  # (a copy: a change counts only once saved)

    async def save(self, data: dict[str, Any]) -> None:
        await save_state(self.url, data)
        self.data = data


async def save_state(url: str, data: dict[str, Any]) -> None:
    import asyncpg

    conn = await asyncpg.connect(plain_url(url))
    try:
        await conn.execute(
            "INSERT INTO demo_state (key, value) VALUES ('sample', $1::jsonb) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
            json.dumps(data),
        )
    finally:
        await conn.close()


#: Shared testing would use up a free plan's monthly bookings and messages within days (the sample history alone uses most of the bookings), so the demo
#: businesses get these monthly limits instead. The plan's other limits (staff, services, sites) stay as they are.
DEMO_LIMITS = {"bookings_per_month": 100000, "whatsapp_per_month": 100000, "sms_per_month": 100000}


async def raise_monthly_limits(client: Any, platform_token: str, businesses: list[dict[str, Any]]) -> None:
    for b in businesses:
        r = await client.put(f"/v1/platform/tenants/{b['tenant_id']}/overrides", headers={"X-Platform-Token": platform_token}, json=DEMO_LIMITS)
        if r.status_code != 200:
            raise SystemExit(f"could not set the demo limits for {b['name']}: {r.status_code} {r.text[:200]}")


async def serve(env: dict[str, str]) -> None:
    sys.path.insert(0, str(BACKEND))
    sys.path.insert(0, str(LOCALSITE))
    sys.path.insert(0, str(HERE))
    import httpx
    import uvicorn

    import devtools  # (localsite: the in-memory inbox, Redis stand-in and address table; nothing in it reaches the outside world)
    from app import worker
    from app.core.config import get_settings
    from app.main import app
    from app.services import captcha, delivery, outbox
    from app.services.calendar import CalendarPublisher
    from app.services.notifications import NotificationPublisher
    from app.services.webhooks import WebhookPublisher

    import pages

    devtools.install_memory_redis()
    devtools.install_inbox()
    devtools.install_geocoder()
    settings = get_settings()
    captcha.configure(settings)
    delivery.configure_providers(settings)

    url = env["DATABASE_URL"]
    state = await load_state(url)
    if state is None:
        say("adding the five sample businesses and their bookings (first start only, a minute or two)...")
        from seed import Seeder

        settings.rate_limit_enabled = False  # (the loader makes a few hundred requests from one address)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                state = await Seeder(client, settings.platform_admin_token, plain_url(url), lambda: list(devtools.INBOX)).run()
        finally:
            settings.rate_limit_enabled = True
        devtools.INBOX.clear()
        await save_state(url, state)
        say("sample data ready.")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        await raise_monthly_limits(client, settings.platform_admin_token, state["businesses"])
    store = DbStore(url, state)
    app.include_router(pages.router(store, lambda: list(devtools.INBOX)))
    app.include_router(devtools.dev_router(app, store))
    app.mount("/", devtools.web_pages(LOCALSITE / "web"), name="web")  # (last: every route above wins over a file of the same name)
    outbox.set_event_publisher(outbox.CompositePublisher(NotificationPublisher(), WebhookPublisher(), CalendarPublisher()))
    port = int(os.environ.get("PORT", "8000"))
    stop = asyncio.Event()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", lifespan="on", proxy_headers=True, forwarded_allow_ips="*"))
    say(f"ready on port {port}")
    sweeper = asyncio.create_task(worker.run(settings.sweep_interval_seconds, stop))
    try:
        await server.serve()
    finally:
        stop.set()
        await sweeper


def main() -> None:
    env = environment()
    os.environ.update(env)
    asyncio.run(prepare_database(env["DATABASE_URL"]))
    migrate(env)
    asyncio.run(serve(env))


if __name__ == "__main__":
    main()
