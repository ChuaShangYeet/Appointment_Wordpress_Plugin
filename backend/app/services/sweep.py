"""The periodic sweep: the housekeeping that must happen even when nobody is using the system.

Per active tenant, in one transaction:

1. **Expire pending holds.** A pending booking nobody answered is rejected and its slot freed once its hold runs
   out (blueprint §13.5). This also happens opportunistically whenever someone books, but a quiet tenant would
   otherwise show stale ``pending`` rows and a slot that stays blocked in the cached availability.
2. **Check review flags once for every new booking** (blueprint §13.5). Changing a setting re-checks the bookings that
   exist at that moment. A booking that was being made at the very same instant was validated against the *old*
   setting and may commit after that re-check ran, so nothing would ever flag it. Shortly after a booking is created
   (``sweep_grace_seconds``, longer than any booking transaction) every rule is run against it once and it is marked
   (``flags_checked_at``); anything the change-time re-check missed is caught here.

Why "once per booking" and not "re-check everything every minute": a manager may deliberately dismiss a flag
(``POST /admin/bookings/{id}/clear-flags``). Re-checking dismissed bookings would raise the flag again forever.
The change-time re-checks already keep existing bookings current; the sweep only has to catch the ones they could not see.

Runs from ``python -m app.worker``. A Postgres advisory lock lets several worker processes run safely: only one
sweeps at a time, the others skip that tick. A failing tenant is logged and skipped without affecting the rest.
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import tenant_session
from app.models import Booking, Tenant
from app.services import calendar, compliance, flagging, outbox, reminders, webhooks
from app.services.booking import LIVE_STATUSES, release_expired_holds

log = logging.getLogger(__name__)

#: One sweep at a time across all worker processes (arbitrary constant).
SWEEP_LOCK_KEY = 7_449_211_305
#: Bookings examined per query, and how many such batches one tenant gets per tick (a large backlog is worked
#: off over several ticks instead of holding one transaction open for long).
BATCH_SIZE = 200
MAX_BATCHES_PER_TENANT = 10


@dataclass
class TenantSweep:
    tenant_id: uuid.UUID
    holds_released: int = 0
    bookings_checked: int = 0
    bookings_flagged: int = 0
    events_published: int = 0
    events_failed: int = 0
    notes_purged: int = 0
    reminders_queued: int = 0
    webhooks_delivered: int = 0
    webhooks_failed: int = 0
    calendar_synced: int = 0
    calendar_removed: int = 0
    calendar_failed: int = 0
    error: str | None = None


@dataclass
class SweepReport:
    #: ``False`` when another worker held the sweep lock and this tick was skipped.
    ran: bool
    tenants: list[TenantSweep] = field(default_factory=list)

    @property
    def errors(self) -> list[TenantSweep]:
        return [t for t in self.tenants if t.error]


async def sweep_tenant(
    tenant_id: uuid.UUID, *, now: dt.datetime | None = None, grace: dt.timedelta | None = None
) -> TenantSweep:
    """Do one tenant's housekeeping in a single transaction (rolled back as a whole if anything fails), then work off its outbox."""
    now = now or dt.datetime.now(dt.UTC)
    grace = grace if grace is not None else dt.timedelta(seconds=get_settings().sweep_grace_seconds)
    result = TenantSweep(tenant_id)
    async with tenant_session(tenant_id) as db:
        tenant = await db.get(Tenant, tenant_id)
        if tenant is None:
            return result
        result.holds_released = await release_expired_holds(db, now)
        for _ in range(MAX_BATCHES_PER_TENANT):
            batch = list((await db.scalars(
                select(Booking)
                .where(Booking.flags_checked_at.is_(None), Booking.created_at <= now - grace)
                .order_by(Booking.created_at, Booking.id)
                .limit(BATCH_SIZE)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )).unique().all())
            if not batch:
                break
            # Bookings that are over or no longer hold a slot need no rule check; they are just marked done.
            live = [b.id for b in batch if b.status in LIVE_STATUSES and b.end_at > now]
            if live:
                report = await flagging.reevaluate(db, tenant, flagging.CHECKS, booking_ids=live, now=now)
                result.bookings_flagged += report.newly_flagged
            for b in batch:
                b.flags_checked_at = now
            await db.flush()
            result.bookings_checked += len(live)
            if len(batch) < BATCH_SIZE:
                break
        result.reminders_queued = await reminders.enqueue_due(db, tenant_id, now, tenant.reminder_hours)
        result.notes_purged = await compliance.purge_health_notes(
            db, tenant_id, now=now, days=get_settings().health_notes_retention_days
        )
    # The outbox is worked off in a transaction of its own, after the housekeeping above has committed. A publisher may
    # open sessions of its own (the notification publisher writes rows that point at bookings); done inside the
    # housekeeping transaction, which holds row locks on the very bookings it flags, it would wait for a lock that is
    # only released when it returns - a deadlock that no database can detect.
    publisher = outbox.get_event_publisher()
    if publisher is not None:  # without one (only the worker installs it) events wait in the table
        async with tenant_session(tenant_id) as db:
            dispatched = await outbox.dispatch_pending(db, publisher, now=now)
            result.events_published, result.events_failed = dispatched.published, dispatched.failed
            retention = dt.timedelta(days=get_settings().outbox_retention_days)
            await outbox.purge_published(db, older_than=now - retention)
    # Webhook deliveries are sent in a transaction of their own, after the outbox has been worked off (that is when new ones appear):
    # they wait on other people's servers, and nothing else in the sweep should hold locks meanwhile.
    async with tenant_session(tenant_id) as db:
        sent = await webhooks.deliver_due(db, now)
        result.webhooks_delivered, result.webhooks_failed = sent.delivered, sent.failed_attempts
        retention = dt.timedelta(days=get_settings().webhook_log_retention_days)
        await webhooks.purge_old(db, older_than=now - retention)
    # Calendar events likewise: they wait on Google, in a transaction of their own.
    async with tenant_session(tenant_id) as db:
        synced = await calendar.sync_due(db, now)
        result.calendar_synced, result.calendar_removed, result.calendar_failed = synced.synced, synced.removed, synced.failed
    return result


async def sweep_all(
    *, now: dt.datetime | None = None, grace: dt.timedelta | None = None, tenant_ids: Sequence[uuid.UUID] | None = None
) -> SweepReport:
    """Sweep every active tenant (or just ``tenant_ids``), unless another worker is already sweeping."""
    async with tenant_session(None) as lock:
        # held until this transaction ends, i.e. for the whole run
        if not await lock.scalar(select(func.pg_try_advisory_xact_lock(SWEEP_LOCK_KEY))):
            return SweepReport(ran=False)
        q = select(Tenant.id).where(Tenant.status == "active").order_by(Tenant.id)
        if tenant_ids is not None:
            q = q.where(Tenant.id.in_(list(tenant_ids)))
        ids = list((await lock.scalars(q)).all())
        report = SweepReport(ran=True)
        for tenant_id in ids:
            try:
                report.tenants.append(await sweep_tenant(tenant_id, now=now, grace=grace))
            except Exception as exc:  # noqa: BLE001 - one tenant's failure must not stop the others
                log.exception("Sweep failed for tenant %s", tenant_id)
                report.tenants.append(TenantSweep(tenant_id, error=f"{type(exc).__name__}: {exc}"))
        return report
