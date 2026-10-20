"""Runs the JustSimple booking site on this computer: the real backend, a bundled PostgreSQL with PostGIS, and the test pages.

    python run_local.py            start (the first start sets up the database and the sample data, about a minute)
    python run_local.py --reset    throw the local data away and start again from the sample data

Then open http://localhost:8780. Stop with Ctrl+C (the database stops with it). Windows only (the bundled PostgreSQL is a Windows build).
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT / "backend"
WEB = HERE / "web"
DATA = HERE / "data"
PGDATA = DATA / "pgdata"
PGBIN = HERE / "pgsql" / "bin"
SECRETS = DATA / "secrets.json"
CONFIG = DATA / "local.json"
PG_PORT = 54330
DB = "justsimple_local"


def say(text: str) -> None:
    print(f"[local] {text}", flush=True)


def pg(tool: str, *args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(PGBIN / f"{tool}.exe"), *args], check=check, text=True, capture_output=capture)


def psql(sql: str, db: str = "postgres") -> str:
    out = pg("psql", "-h", "localhost", "-p", str(PG_PORT), "-U", "postgres", "-d", db, "-tAc", sql, capture=True)
    return out.stdout.strip()


def load_secrets() -> dict[str, str]:
    if SECRETS.exists():
        return json.loads(SECRETS.read_text(encoding="utf8"))
    made = {
        "jwt_secret": secrets.token_urlsafe(48),
        "pdpa_key": base64.b64encode(os.urandom(32)).decode(),
        "platform_token": secrets.token_urlsafe(32),
        "owner_password": secrets.token_urlsafe(16),
        "app_password": secrets.token_urlsafe(16),
        "backup_password": secrets.token_urlsafe(16),
    }
    DATA.mkdir(parents=True, exist_ok=True)
    SECRETS.write_text(json.dumps(made, indent=2), encoding="utf8")
    return made


def pg_running() -> bool:
    return pg("pg_ctl", "status", "-D", str(PGDATA), check=False, capture=True).returncode == 0


def start_database(s: dict[str, str]) -> None:
    if not (PGDATA / "PG_VERSION").exists():
        say("creating the local database (first start only)...")
        PGDATA.parent.mkdir(parents=True, exist_ok=True)
        pg("initdb", "-D", str(PGDATA), "-U", "postgres", "-A", "trust", "-E", "UTF8", "--locale=C", capture=True)
    for _ in range(30):  # (a server that is still shutting down from a previous run counts as running for a moment)
        if pg_running() and pg("psql", "-h", "localhost", "-p", str(PG_PORT), "-U", "postgres", "-tAc", "SELECT 1", check=False, capture=True).returncode == 0:
            break
        if not pg_running():
            _start_server()
        time.sleep(1)
    else:
        raise SystemExit(f"PostgreSQL did not start: see {DATA / 'postgres.log'}")
    if psql(f"SELECT 1 FROM pg_database WHERE datname = '{DB}'") != "1":
        psql(f"CREATE DATABASE {DB}")
    _roles(s)


def _start_server() -> None:
    say(f"starting PostgreSQL on port {PG_PORT}...")
    # (not captured: the server inherits the pipes and would keep them open, so the call would never return)
    subprocess.run([str(PGBIN / "pg_ctl.exe"), "start", "-D", str(PGDATA), "-w", "-l", str(DATA / "postgres.log"),
                    "-o", f"-p {PG_PORT} -c listen_addresses=localhost"], check=False, stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _roles(s: dict[str, str]) -> None:
    # the three roles of the production set-up (the API runs as booking_app, which cannot switch tenant isolation off)
    pg("psql", "-h", "localhost", "-p", str(PG_PORT), "-U", "postgres", "-d", DB, "-q", "-v", "ON_ERROR_STOP=1",
       "-v", f"owner_password={s['owner_password']}", "-v", f"app_password={s['app_password']}", "-v", f"backup_password={s['backup_password']}",
       "-f", str(BACKEND / "deploy" / "postgres" / "01_roles.sql"), capture=True)


def stop_database() -> None:
    if PGDATA.exists() and pg_running():
        say("stopping PostgreSQL...")
        pg("pg_ctl", "stop", "-D", str(PGDATA), "-m", "fast", "-w", check=False, capture=True)


def environment(s: dict[str, str], port: int) -> dict[str, str]:
    site = f"http://localhost:{port}"
    return {
        "ENVIRONMENT": "development",
        "DATABASE_URL": f"postgresql+asyncpg://booking_app:{s['app_password']}@localhost:{PG_PORT}/{DB}",
        "MIGRATION_DATABASE_URL": f"postgresql+asyncpg://booking_owner:{s['owner_password']}@localhost:{PG_PORT}/{DB}",
        "JWT_SECRET": s["jwt_secret"],
        "PDPA_ENCRYPTION_KEY": s["pdpa_key"],
        "PLATFORM_ADMIN_TOKEN": s["platform_token"],
        "SELF_SERVICE_SIGNUP": "true",
        "OTP_CHANNEL": "whatsapp",
        # the links in the emails lead to the test pages, which finish the step
        "MAGIC_LINK_URL": site + "/#link/sign-in/{token}",
        "EMAIL_CONFIRM_URL": site + "/#link/confirm-email/{token}",
        "SIGNUP_VERIFY_URL": site + "/#link/sign-up/{token}",
        "OWNER_LOGIN_URL": site + "/#link/owner/{token}",
        "REVIEW_URL": site + "/#link/review/{token}",
        "TERMS_URL": site + "/#terms",
        # reminders would use up the free plan's 20 WhatsApp messages a month within a day of testing, and then phone sign-in stops
        "REMINDER_HOURS": "[]",
        "LOG_FORMAT": "text",
        "SWEEP_INTERVAL_SECONDS": "20",
        "SWEEP_GRACE_SECONDS": "20",
    }


def migrate(env: dict[str, str]) -> None:
    say("bringing the database schema up to date...")
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env={**os.environ, **env}, check=True, capture_output=True, text=True)


async def serve(port: int, s: dict[str, str], open_browser: bool = False) -> None:
    sys.path.insert(0, str(BACKEND))
    sys.path.insert(0, str(HERE))
    import httpx
    import uvicorn

    import devtools
    from app.core.config import get_settings
    from app.main import app
    from app.services import captcha, delivery, outbox
    from app.services.calendar import CalendarPublisher
    from app.services.notifications import NotificationPublisher
    from app.services.webhooks import WebhookPublisher
    from app import worker

    devtools.install_memory_redis()
    devtools.install_inbox()
    devtools.install_geocoder()
    captcha.configure(get_settings())
    delivery.configure_providers(get_settings())  # (fills nothing: the inbox already serves every channel)

    if not CONFIG.exists():
        say("adding the sample businesses and bookings (first start only, about a minute)...")
        from seed import Seeder

        settings = get_settings()
        settings.rate_limit_enabled = False  # (the seed makes a few hundred requests from one address)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
                seeder = Seeder(client, s["platform_token"], f"postgresql://postgres@localhost:{PG_PORT}/{DB}", lambda: list(devtools.INBOX))
                data = await seeder.run()
        finally:
            settings.rate_limit_enabled = True
        devtools.INBOX.clear()
        CONFIG.write_text(json.dumps(data, indent=2), encoding="utf8")
        say("sample data ready.")

    app.include_router(devtools.dev_router(app, devtools.FileStore(CONFIG)))
    app.mount("/", devtools.web_pages(WEB), name="web")

    outbox.set_event_publisher(outbox.CompositePublisher(NotificationPublisher(), WebhookPublisher(), CalendarPublisher()))
    stop = asyncio.Event()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    say(f"ready: open http://localhost:{port}   (Ctrl+C to stop)")
    if open_browser:
        import webbrowser

        asyncio.get_running_loop().call_later(1.5, webbrowser.open, f"http://localhost:{port}")
    sweeper = asyncio.create_task(worker.run(get_settings().sweep_interval_seconds, stop))
    try:
        await server.serve()
    finally:
        stop.set()
        await sweeper


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="delete the local data and start again from the sample data")
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--open", action="store_true", help="open the site in the browser once it is ready")
    args = parser.parse_args()
    if args.reset:
        stop_database()
        if PGDATA.exists():
            say("deleting the local data...")
            shutil.rmtree(PGDATA)
        CONFIG.unlink(missing_ok=True)
    s = load_secrets()
    start_database(s)
    try:
        env = environment(s, args.port)
        migrate(env)
        os.environ.update(env)
        asyncio.run(serve(args.port, s, args.open))
    except KeyboardInterrupt:
        pass
    finally:
        time.sleep(0.2)
        stop_database()


if __name__ == "__main__":
    main()
