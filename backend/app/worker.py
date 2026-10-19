"""Background worker: ``python -m app.worker`` runs the periodic sweep (see :mod:`app.services.sweep`).

``--once`` sweeps a single time and exits (for cron / a one-off job). Otherwise it loops every
``SWEEP_INTERVAL_SECONDS`` until it receives SIGTERM/SIGINT. Several workers may run at once; the sweep takes a
database lock, so only one of them sweeps at a time. An unexpected error (say the database restarting) is logged
and the next tick tries again.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time
import uuid
from collections.abc import Sequence

from app.core.config import get_settings
from app.core import hardening, logs, observability
from app.core.database import dispose_engine, get_engine
from app.services import delivery, outbox
from app.services.calendar import CalendarPublisher
from app.services.notifications import NotificationPublisher
from app.services.webhooks import WebhookPublisher
from app.services.sweep import SweepReport, sweep_all

log = logging.getLogger("app.worker")


def summarise(report: SweepReport) -> str:
    if not report.ran:
        return "sweep skipped: another worker holds the lock"
    released = sum(t.holds_released for t in report.tenants)
    checked = sum(t.bookings_checked for t in report.tenants)
    flagged = sum(t.bookings_flagged for t in report.tenants)
    published = sum(t.events_published for t in report.tenants)
    purged = sum(t.notes_purged for t in report.tenants)
    queued = sum(t.reminders_queued for t in report.tenants)
    hooked = sum(t.webhooks_delivered for t in report.tenants)
    calendared = sum(t.calendar_synced + t.calendar_removed for t in report.tenants)
    return (
        f"swept {len(report.tenants)} tenants: {released} holds expired, {checked} new bookings checked, "
        f"{flagged} flagged, {published} events published, {queued} reminders queued, {hooked} webhooks delivered, {calendared} calendar events written, {purged} health notes purged, {len(report.errors)} failed"
    )


async def run(interval: float, stop: asyncio.Event, *, once: bool = False) -> None:
    """Sweep, wait ``interval`` seconds (or until ``stop`` is set), repeat."""
    while not stop.is_set():
        started = time.monotonic()
        logs.request_id_var.set(f"sweep-{uuid.uuid4().hex[:12]}")  # every line of this tick carries it
        try:
            log.info(summarise(await sweep_all()))
        except Exception:  # noqa: BLE001 - keep the worker alive; the next tick retries
            log.exception("Sweep failed")
        if once:
            return
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.0, interval - (time.monotonic() - started)))
        except TimeoutError:
            pass


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Booking engine background worker")
    parser.add_argument("--once", action="store_true", help="sweep once and exit")
    return parser.parse_args(argv)


async def amain(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    settings = get_settings()
    logs.configure_logging(settings.log_level, logs.resolve_format(settings))  # first: the start-up checks below write to it
    observability.init_sentry(settings)
    await hardening.enforce(get_settings(), get_engine())  # the worker writes to every tenant's tables: the same isolation rule applies
    delivery.configure_providers(get_settings())
    delivery.report_configuration(get_settings())
    # booking events become customer messages and are owed to the businesses' webhooks
    outbox.set_event_publisher(outbox.CompositePublisher(NotificationPublisher(), WebhookPublisher(), CalendarPublisher()))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows event loops have no signal handlers; Ctrl+C still raises
            pass
    try:
        await run(get_settings().sweep_interval_seconds, stop, once=args.once)
    finally:
        await dispose_engine()


if __name__ == "__main__":
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass
