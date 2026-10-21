"""The hosted demo of the JustSimple booking API (for example on Render): the real backend, unchanged, with five sample businesses everyone may use.

    python demo/server.py            (from the repository root; PORT and DATABASE_URL come from the environment)

What it does, in order:
1. reads the settings from the environment (``DATABASE_URL`` as Render gives it, ``postgres://...``, or as Neon gives it, ``...?sslmode=require``);
2. makes sure PostGIS and btree_gist exist, then brings the schema up to date with the backend's own migrations; where the database user may create
   roles (Neon), the backend itself then connects as a plain role of its own, so that Row-Level Security applies to it (see ``app_role``);
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
import hashlib
import hmac
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT / "backend"
LOCALSITE = ROOT / "localsite"

log = logging.getLogger("demo")


def say(text: str) -> None:
    print(f"[demo] {text}", flush=True)


def _address(url: str) -> tuple[str, bool]:
    """``(the address without its scheme and ?options, whether it asks for SSL)``. Neon's ends in ``?sslmode=require&channel_binding=require``:
    the SQLAlchemy driver takes neither as it stands, and asyncpg would send ``channel_binding`` to the server as a setting; only SSL matters."""
    base, _, query = url.partition("?")
    for prefix in ("postgresql+asyncpg://", "postgres://", "postgresql://"):
        if base.startswith(prefix):
            ssl = any(k in ("sslmode", "ssl") and v not in ("disable", "allow", "prefer", "false") for k, v in parse_qsl(query))
            return base[len(prefix):], ssl
    raise SystemExit("DATABASE_URL must be a PostgreSQL address (postgres://user:password@host:5432/database)")


def asyncpg_url(url: str) -> str:
    """``postgres://u:p@h/db`` (Render, Heroku), ``postgresql://...?sslmode=require`` (Neon) -> the SQLAlchemy asyncpg form the backend expects."""
    rest, ssl = _address(url)
    return "postgresql+asyncpg://" + rest + ("?ssl=require" if ssl else "")


def plain_url(url: str) -> str:
    """The same address for asyncpg itself (the sample loader's direct connection)."""
    rest, ssl = _address(url)
    return "postgresql://" + rest + ("?sslmode=require" if ssl else "")


def public_url() -> str:
    """The demo's own address (links in emails lead here)."""
    return os.environ.get("PUBLIC_URL", "").rstrip("/") or os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/") or "http://localhost:" + os.environ.get("PORT", "8000")


def environment() -> dict[str, str]:
    """The backend's settings for the demo. Secrets must be set on the host (Render generates them: see render.yaml)."""
    raw = os.environ.get("DATABASE_URL")
    if not raw:
        raise SystemExit("DATABASE_URL is not set")
    for name in ("JWT_SECRET", "PDPA_ENCRYPTION_KEY", "PLATFORM_ADMIN_TOKEN"):
        if not os.environ.get(name):
            raise SystemExit(f"{name} is not set")
    site = public_url()
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


async def prepare_database(url: str, secret: str) -> str | None:
    """PostGIS and btree_gist must exist before the first migration. Render's and Neon's database users may create them; elsewhere a superuser may
    have to. Returns the backend's own address (``app_role``), or ``None`` when it connects as the owner."""
    import asyncpg

    conn = await asyncpg.connect(plain_url(url))
    try:
        for ext in ("btree_gist", "postgis"):
            try:
                await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
            except asyncpg.PostgresError as exc:
                raise SystemExit(f"Could not create the {ext} extension ({exc}). Create it once as an administrator: CREATE EXTENSION {ext};") from exc
        await conn.execute("CREATE TABLE IF NOT EXISTS demo_state (key text PRIMARY KEY, value jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())")
        return await app_role(conn, url, secret)
    finally:
        await conn.close()


#: The backend's own login where the database user may create roles (see ``app_role``)
APP_ROLE = "justsimple_demo_app"


async def app_role(conn: Any, url: str, secret: str) -> str | None:
    """Row-Level Security does not apply to the tables' owner, nor to a role with BYPASSRLS (Neon gives its own users that), and the backend's
    start-up checks refuse a role it does not apply to. Where the database user may create roles (Neon), the backend gets a plain one of its own
    and the owner keeps the migrations and the demo's own tables; where it may not (Render), the backend connects as the owner, as before.
    The password comes from JWT_SECRET, so it is the same on every start and nothing has to be stored."""
    import asyncpg

    password = hmac.new(secret.encode(), b"demo database role", hashlib.sha256).hexdigest()  # (hex only: safe inside the quotes below)
    try:
        exists = await conn.fetchval("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = $1)", APP_ROLE)
        await conn.execute(f"{'ALTER' if exists else 'CREATE'} ROLE {APP_ROLE} LOGIN PASSWORD '{password}'")
        db = await conn.fetchval("SELECT current_database()")
        await conn.execute(f'GRANT CONNECT ON DATABASE "{db}" TO {APP_ROLE}')
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    except asyncpg.PostgresError as exc:
        say(f"the backend connects as the database owner (no role of its own: {exc.__class__.__name__})")
        return None
    unsafe = await conn.fetchval("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = $1", APP_ROLE)
    if unsafe:
        raise SystemExit(f"the role {APP_ROLE} skips Row-Level Security (superuser or BYPASSRLS): drop it and start again")
    parts = urlsplit(plain_url(url))
    host = parts.netloc.rsplit("@", 1)[-1]
    say(f"the backend connects as its own role, {APP_ROLE}")
    return asyncpg_url(urlunsplit(parts._replace(netloc=f"{APP_ROLE}:{password}@{host}")))


async def grant_app_role(url: str) -> None:
    """After the migrations: the backend's role may read and write every table, except the migration bookkeeping and the demo's own state."""
    import asyncpg

    conn = await asyncpg.connect(plain_url(url))
    try:
        await conn.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
        await conn.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
        await conn.execute(f"REVOKE ALL ON TABLE alembic_version, demo_state FROM {APP_ROLE}")
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


async def raise_monthly_limits(client: Any, platform_token: str, businesses: list[dict[str, Any]]) -> bool:
    """``False`` when a sample business no longer exists (a reset was cut off half-way: the caller resets again)."""
    for b in businesses:
        if b.get("is_new"):
            continue  # (businesses visitors signed up keep the free plan's limits)
        r = await client.put(f"/v1/platform/tenants/{b['tenant_id']}/overrides", headers={"X-Platform-Token": platform_token}, json=DEMO_LIMITS)
        if r.status_code == 404:
            return False
        if r.status_code != 200:
            raise SystemExit(f"could not set the demo limits for {b['name']}: {r.status_code} {r.text[:200]}")
    return True


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

    import app.core.redis as redis_module
    import pages
    from access import Access
    from reset import DemoReset
    from access import router as access_router

    devtools.install_memory_redis()
    devtools.install_inbox()
    devtools.install_geocoder()
    settings = get_settings()
    captcha.configure(settings)
    delivery.configure_providers(settings)

    url = env["MIGRATION_DATABASE_URL"]  # (the owner: the demo's own tables, the sample loader and the nightly reset)

    async def reseed() -> dict[str, Any]:
        """The five sample businesses and their bookings, through the real API (first start, and every reset: see reset.py)."""
        from seed import Seeder

        settings.rate_limit_enabled = False  # (the loader makes a few hundred requests from one address)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                fresh = await Seeder(client, settings.platform_admin_token, plain_url(url), lambda: list(devtools.INBOX)).run()
                await raise_monthly_limits(client, settings.platform_admin_token, fresh["businesses"])
        finally:
            settings.rate_limit_enabled = True
        devtools.INBOX.clear()
        await save_state(url, fresh)
        return fresh

    async def clear_memory() -> None:
        devtools.INBOX.clear()
        await redis_module.get_redis().flushall()  # (the in-memory stand-in: cached free times and sign-in codes of the old data)

    state = await load_state(url)
    first_start = state is None
    if state is None:
        say("adding the five sample businesses and their bookings (first start only, a minute or two)...")
        state = await reseed()
        say("sample data ready.")
    broken = False
    if not first_start:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
            broken = not await raise_monthly_limits(client, settings.platform_admin_token, state["businesses"])
    store = DbStore(url, state)
    resetter = DemoReset(plain_url(url), reseed, clear_memory, lambda fresh: setattr(store, "data", fresh))
    if first_start:
        await resetter.mark_fresh()
    if broken:
        say("the sample businesses are missing (a reset was cut off): resetting again...")
        repair = asyncio.get_running_loop().create_task(resetter.run("repair: a reset was cut off"))  # (kept in a name: a task nobody holds can be collected)
        await asyncio.sleep(0)  # (started first: the nightly check below then finds a reset running and leaves it be)
    # the website is for signed-in visitors only (access.py); the API stays open
    access = Access(plain_url(url), settings.jwt_secret, public_url(), settings.platform_admin_token, pages._page, busy=lambda: resetter.running)
    await access.prepare()
    web = devtools.web_pages(LOCALSITE / "web")

    async def site(request: Any) -> Any:
        return await web.get_response("", request.scope)

    app.include_router(access_router(access, site, resetter))
    app.include_router(pages.router(store, lambda: list(devtools.INBOX)))
    app.include_router(devtools.dev_router(app, store, guard=access.require))
    app.mount("/", web, name="web")  # (last: every route above wins over a file of the same name)
    outbox.set_event_publisher(outbox.CompositePublisher(NotificationPublisher(), WebhookPublisher(), CalendarPublisher()))
    port = int(os.environ.get("PORT", "8000"))
    stop = asyncio.Event()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", lifespan="on", proxy_headers=True, forwarded_allow_ips="*"))
    say(f"ready on port {port}")
    sweeper = asyncio.create_task(worker.run(settings.sweep_interval_seconds, stop))
    nightly = asyncio.create_task(resetter.nightly(stop))  # (also runs a reset that fell due while the free service slept)
    try:
        await server.serve()
    finally:
        stop.set()
        await sweeper
        await nightly


def main() -> None:
    env = environment()
    os.environ.update(env)
    backend_url = asyncio.run(prepare_database(env["MIGRATION_DATABASE_URL"], os.environ["JWT_SECRET"]))
    migrate(env)
    if backend_url:
        asyncio.run(grant_app_role(env["MIGRATION_DATABASE_URL"]))
        env["DATABASE_URL"] = backend_url
        os.environ["DATABASE_URL"] = backend_url
    asyncio.run(serve(env))


if __name__ == "__main__":
    main()
