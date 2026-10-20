"""Putting the demo's sample data back: every night (``DEMO_RESET_HOUR``, default 3 am Malaysia time) and when the owner presses the button on
``/demo/visitors``.

A reset empties every table the booking system owns (bookings, customers, businesses visitors signed up, changes made in set-up...), clears the
in-memory inbox and cache, and adds the five sample businesses again through the real API, as on the very first start. It keeps the demo's own
tables: the visitors and the register and log-in log (``demo_visitors``, ``demo_access_log``), and ``plans`` (the plan limits the migrations put
in). The schema is untouched, so no migration runs.

**Render's free plan sleeps** after 15 minutes without visitors, and a sleeping service has no timer. So the time of the last reset is kept in
the database, and on every start (a wake-up is a start) a reset that was due while asleep runs straight away. While a reset runs (a minute or two)
the website shows "being reset" instead of half-empty pages.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger("demo")

KL = ZoneInfo("Asia/Kuala_Lumpur")
#: The demo's own tables and the reference data from the migrations: never emptied.
#: The background sweep's lock (``app.services.sweep.SWEEP_LOCK_KEY``). The reset holds it from start to end: the sweep reads every business in one
#: transaction, and emptying the tables while it runs would leave the two waiting on each other for ever (both in this one process, so the
#: database cannot see it as a deadlock). Holding it, a sweep that is running finishes first and the next ones skip until the reset is done.
from app.services.sweep import SWEEP_LOCK_KEY  # noqa: E402  (imported once the backend is on the path: see server.py)
KEEP = frozenset({"alembic_version", "plans", "spatial_ref_sys", "demo_state", "demo_visitors", "demo_access_log"})


def reset_hour() -> int | None:
    """The hour (Malaysia time) of the nightly reset; ``DEMO_RESET_HOUR=off`` switches it off."""
    raw = os.environ.get("DEMO_RESET_HOUR", "3").strip().lower()
    if raw in ("off", "none", "no", ""):
        return None
    hour = int(raw)
    if not 0 <= hour <= 23:
        raise SystemExit("DEMO_RESET_HOUR must be an hour from 0 to 23, or off")
    return hour


def last_due(now: dt.datetime, hour: int) -> dt.datetime:
    """The most recent nightly reset time at or before ``now``."""
    local = now.astimezone(KL)
    due = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if due > local:
        due -= dt.timedelta(days=1)
    return due


class DemoReset:
    def __init__(self, dsn: str, reseed: Callable[[], Awaitable[dict[str, Any]]], clear_memory: Callable[[], Awaitable[None]],
                 on_new_state: Callable[[dict[str, Any]], None]) -> None:
        self.dsn, self.reseed, self.clear_memory, self.on_new_state = dsn, reseed, clear_memory, on_new_state
        self.running = False
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ when it last ran
    async def _conn(self) -> Any:
        import asyncpg

        return await asyncpg.connect(self.dsn)

    async def last_reset(self) -> dt.datetime | None:
        conn = await self._conn()
        try:
            raw = await conn.fetchval("SELECT value FROM demo_state WHERE key = 'last_reset'")
        finally:
            await conn.close()
        return dt.datetime.fromisoformat(json.loads(raw)["at"]) if raw else None

    async def _mark(self, why: str) -> None:
        conn = await self._conn()
        try:
            await conn.execute(
                "INSERT INTO demo_state (key, value) VALUES ('last_reset', $1::jsonb) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
                json.dumps({"at": dt.datetime.now(dt.UTC).isoformat(), "why": why}),
            )
        finally:
            await conn.close()

    async def mark_fresh(self) -> None:
        """The sample data was just made (the very first start): that counts as a reset."""
        await self._mark("first start")

    # ------------------------------------------------------------------ the reset
    async def _empty_tables(self) -> None:
        conn = await self._conn()
        try:
            await conn.execute("SET lock_timeout = '60s'")  # (if anything still holds a table, fail and say so rather than wait for ever)
            names = [r["tablename"] for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user")]
            wipe = [n for n in names if n not in KEEP]
            if wipe:
                # (TRUNCATE fires no row triggers, so the audit log's append-only trigger does not stop it; RESTART IDENTITY resets the counters)
                await conn.execute("TRUNCATE " + ", ".join(f'public."{n}"' for n in wipe) + " RESTART IDENTITY CASCADE")
        finally:
            await conn.close()

    async def run(self, why: str) -> bool:
        """Reset now. ``False`` when a reset is already running."""
        if self._lock.locked():
            return False
        async with self._lock:
            self.running = True
            started = dt.datetime.now(dt.UTC)
            log.warning("demo reset started (%s)", why)
            sweep_lock = await self._conn()
            try:
                await asyncio.wait_for(sweep_lock.execute("SELECT pg_advisory_lock($1)", SWEEP_LOCK_KEY), timeout=300)
                await self._empty_tables()
                await self.clear_memory()
                state = await self.reseed()
                self.on_new_state(state)
                await self._mark(why)
                log.warning("demo reset done in %ds", (dt.datetime.now(dt.UTC) - started).total_seconds())
            except Exception:  # noqa: BLE001 - a failed reset is logged; the next one (or a restart) tries again
                log.exception("demo reset failed")
            finally:
                await sweep_lock.close()  # (closing the session releases the lock)
                self.running = False
        return True

    async def nightly(self, stop: asyncio.Event) -> None:
        """Runs a reset that was missed while asleep, then one every night at ``DEMO_RESET_HOUR``."""
        hour = reset_hour()
        if hour is None:
            return
        last = await self.last_reset()
        if last is None or last < last_due(dt.datetime.now(dt.UTC), hour):
            await self.run("nightly (was due while the demo slept)")
        while not stop.is_set():
            now = dt.datetime.now(dt.UTC)
            nxt = last_due(now, hour) + dt.timedelta(days=1)
            try:
                await asyncio.wait_for(stop.wait(), timeout=max(1.0, (nxt - now).total_seconds()))
            except TimeoutError:
                await self.run("nightly")
