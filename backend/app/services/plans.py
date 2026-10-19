"""Plans and limits: what a business may have on its plan (free tier and paid tier, ``plans`` table, ``tenants.plan``).

The free tier is the same hosted backend with per-business limits; the paid tier has none. A limit is a number (``NULL`` = no limit) of

* **resources** (``max_resources``): *active* staff, tables and bays together; deactivating one frees its place;
* **services** (``max_services``): *active* services;
* **bookings a month** (``max_bookings_per_month``): bookings made in the business's own calendar month (its time zone), not counting cancelled or rejected
  ones (a freed slot is a freed place); bookings made by staff count like the others;
* **WhatsApp and SMS messages a month** (``max_whatsapp_per_month``, ``max_sms_per_month``): messages *sent* that month (sign-in codes, customer messages, the
  stylist alert). Over the limit a customer message is logged as ``skipped`` (it is not retried, and the booking is not affected) and a sign-in code is refused with
  a plan-limit error, so the customer signs in by the email link instead. Email is not limited.

* **websites, domains and live API keys** (``max_sites``, ``max_domains``, ``max_live_keys_per_kind``): what a free account is held to - one account, one business, one website. A
  site's domains are counted without the ``www.`` (``shop.example`` and ``www.shop.example`` are one), the keys are the live ones of each kind. These are *structural* limits:
  they are not warned about by email (a free business sits at 1 of 1 sites from the day it signs up).
* **how often the website address may change** (``domain_change_days``): a cooldown, not a count - see :func:`domain_change_due`.

Going over a limit is refused with 402 ``plan_limit_reached`` (``limit``, ``allowed``, ``used``, ``plan`` say which). Nothing is ever deleted: a business moved
to a smaller plan keeps what it has and simply cannot add more until it is back under (``GET /admin/plan`` lists what is over).

Counting happens under a per-business advisory lock held to the end of the transaction, so two requests cannot both take the last place. Messages have a lock of their
own (``messages:<business>``, so a slow gateway never holds up a booking) and a message is counted *before* it is handed to the gateway, so two senders cannot both take the
last one: the worker's lock lasts until its log row is committed, and a sign-in code writes a ``sending`` row (which counts) in a short transaction of its own, then settles it.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import origins
from app.core.config import get_settings
from app.core.exceptions import PlanLimitReached
from app.models import ApiKey, Booking, NotificationLog, Plan, PlanWarning, Resource, Service, Site, Tenant
from app.services import events
from app.services.availability import clock_on

#: Bookings in these states do not use up a place in the month.
FREED_STATUSES = ("cancelled", "rejected")
#: The channels that cost money per message, and the plan column that limits each.
METERED = {"whatsapp": "max_whatsapp_per_month", "sms": "max_sms_per_month"}
#: The limits, by the short names the API and ``tenants.plan_overrides`` use; the plan's column is ``max_`` + the name.
LIMIT_NAMES = ("resources", "services", "bookings_per_month", "whatsapp_per_month", "sms_per_month", "sites", "domains", "live_keys_per_kind")
#: The limits a business is warned about by email as it nears them (the others are structural: a free business is at 1 of 1 sites from the start).
WARNED = LIMIT_NAMES[:5]
#: Rules that are numbers too but not counts of anything (they have no usage): how many days must pass between two changes of the website address.
RULE_NAMES = ("domain_change_days",)
#: The limits that count again from zero each calendar month (the others count what the business has).
MONTHLY = frozenset({"bookings_per_month", "whatsapp_per_month", "sms_per_month"})
#: The outbox event that makes the business an email about a limit it is near or has reached (``app.services.notifications``).
LIMIT_WARNING_EVENT = "plan.limit_warning"
#: What each limit counts, for the message to the person who hit it.
LABELS = {
    "max_resources": "staff, tables and bays", "max_services": "services", "max_bookings_per_month": "bookings a month",
    "max_whatsapp_per_month": "WhatsApp messages a month", "max_sms_per_month": "SMS messages a month",
    "max_sites": "websites", "max_domains": "domains (www and non-www count as one)", "max_live_keys_per_kind": "live API keys of each kind (publishable and secret)",
}


@dataclass(frozen=True)
class Limits:
    resources: int | None
    services: int | None
    bookings_per_month: int | None
    whatsapp_per_month: int | None
    sms_per_month: int | None
    sites: int | None = None
    domains: int | None = None
    live_keys_per_kind: int | None = None
    #: Days between two changes of the website address (a rule, not a count).
    domain_change_days: int | None = None


@dataclass(frozen=True)
class Usage:
    resources: int
    services: int
    bookings_per_month: int
    whatsapp_per_month: int
    sms_per_month: int
    sites: int = 0
    domains: int = 0
    #: The most live keys of one kind (publishable or secret) the business has.
    live_keys_per_kind: int = 0


def overrides_of(tenant: Tenant | None) -> dict[str, int | None]:
    """The limits set for this business alone (``null`` = no limit); anything that is not a limit is ignored."""
    return {k: v for k, v in ((tenant.plan_overrides if tenant is not None else None) or {}).items() if k in LIMIT_NAMES or k in RULE_NAMES}


def limit_value(plan: Plan, tenant: Tenant | None, name: str) -> int | None:
    """The limit in force for ``name`` (``resources``, ``bookings_per_month``...): the business's own if one was set, else the plan's. ``None`` = no limit."""
    own = overrides_of(tenant)
    return own[name] if name in own else getattr(plan, name if name in RULE_NAMES else "max_" + name)


def limits_of(plan: Plan, tenant: Tenant | None = None) -> Limits:
    """The limits that apply: the plan's, with this business's own numbers where the operator set them."""
    return Limits(*(limit_value(plan, tenant, name) for name in LIMIT_NAMES + RULE_NAMES))


async def plan_of(db: AsyncSession, tenant: Tenant) -> Plan:
    plan = await db.get(Plan, tenant.plan)
    if plan is None:  # (a foreign key makes this impossible)
        raise RuntimeError(f"business {tenant.id} is on the unknown plan {tenant.plan!r}")
    return plan


def month_bounds(tenant: Tenant, now: dt.datetime) -> tuple[dt.datetime, dt.datetime]:
    """The calendar month ``now`` falls in, in the business's time zone, as UTC instants: ``[first moment, first moment of the next month)``."""
    tz = ZoneInfo(tenant.timezone)
    local = now.astimezone(tz)
    first = dt.date(local.year, local.month, 1)
    following = dt.date(local.year + (local.month == 12), local.month % 12 + 1, 1)
    return clock_on(first, dt.time.min, tz), clock_on(following, dt.time.min, tz)


async def _count(db: AsyncSession, query: object) -> int:
    return int(await db.scalar(query) or 0)  # type: ignore[call-overload]


async def messages_this_month(db: AsyncSession, tenant: Tenant, channel: str, now: dt.datetime) -> int:
    """Messages sent on ``channel`` since the month began. ``sent_at`` is set when a message went out, or - for a sign-in code - when its place was taken just before
    it was handed to the gateway (status ``sending``; a failure clears it again, a code whose result is never known keeps its place); nothing is dated after ``now``."""
    lo, _ = month_bounds(tenant, now)
    return await _count(db, select(func.count()).select_from(NotificationLog).where(NotificationLog.channel == channel, NotificationLog.sent_at >= lo))


async def bookings_this_month(db: AsyncSession, tenant: Tenant, now: dt.datetime) -> int:
    lo, _ = month_bounds(tenant, now)
    return await _count(db, select(func.count()).select_from(Booking).where(Booking.created_at >= lo, Booking.status.notin_(FREED_STATUSES)))


async def live_keys(db: AsyncSession, tenant: Tenant) -> dict[str, int]:
    """How many live (not revoked) API keys the business has of each kind. (``api_keys`` and ``sites`` are outside Row-Level Security: every query names the business.)"""
    rows = (await db.execute(
        select(ApiKey.kind, func.count()).where(ApiKey.tenant_id == tenant.id, ApiKey.revoked_at.is_(None)).group_by(ApiKey.kind)
    )).all()
    return {kind: int(n) for kind, n in rows}


async def site_usage(db: AsyncSession, tenant: Tenant) -> tuple[int, int, int]:
    """``(websites, distinct domains, most live keys of one kind)``."""
    sites = (await db.execute(select(Site.domain, Site.allowed_origins).where(Site.tenant_id == tenant.id))).all()
    hosts: set[str] = set()
    for domain, allowed in sites:
        hosts |= origins.hosts_of(domain, allowed)
    return len(sites), len(hosts), max((await live_keys(db, tenant)).values(), default=0)


async def usage(db: AsyncSession, tenant: Tenant, now: dt.datetime | None = None) -> Usage:
    now = now or dt.datetime.now(dt.UTC)
    sites, domains, keys = await site_usage(db, tenant)
    return Usage(
        sites=sites, domains=domains, live_keys_per_kind=keys,
        resources=await _count(db, select(func.count()).select_from(Resource).where(Resource.status == "active")),
        services=await _count(db, select(func.count()).select_from(Service).where(Service.active.is_(True))),
        bookings_per_month=await bookings_this_month(db, tenant, now),
        whatsapp_per_month=await messages_this_month(db, tenant, "whatsapp", now),
        sms_per_month=await messages_this_month(db, tenant, "sms", now),
    )


async def _lock(db: AsyncSession, tenant: Tenant) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"plan:{tenant.id}"})


async def _lock_messages(db: AsyncSession, tenant: Tenant) -> None:
    """Make senders for this business count one at a time, until this transaction ends (a separate key from the plan lock: bookings do not wait for messages)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"messages:{tenant.id}"})


def _refusal(plan: Plan, tenant: Tenant, limit: str, allowed: int, used: int) -> PlanLimitReached:
    own = " (with limits set for your business)" if limit.removeprefix("max_") in overrides_of(tenant) else ""
    return PlanLimitReached(
        f"The {plan.name} plan{own} allows {allowed} {LABELS[limit]} and {used} are in use; remove one or move to a bigger plan",
        extra={"limit": limit, "allowed": allowed, "used": used, "plan": plan.code},
    )


def month_label(tenant: Tenant, now: dt.datetime) -> str:
    """``2026-10``: the business's own calendar month."""
    local = now.astimezone(ZoneInfo(tenant.timezone))
    return f"{local.year:04d}-{local.month:02d}"


def warning_level(used: int, allowed: int | None) -> str | None:
    """``reached`` at the limit, ``near`` from ``PLAN_WARNING_PERCENT`` of it, else ``None`` (also for no limit, a limit of none at all, or warnings switched off)."""
    percent = get_settings().plan_warning_percent
    if allowed is None or allowed <= 0 or percent <= 0:
        return None
    if used >= allowed:
        return "reached"
    return "near" if used >= max(1, math.ceil(allowed * percent / 100)) else None


async def note_usage(db: AsyncSession, tenant: Tenant, name: str, used_after: int, allowed: int | None, now: dt.datetime) -> None:
    """Called when one more of ``name`` has just been allowed: if that brings the business near its limit (or to it) for the first time, queue the email that says so.

    Once per limit, month (or ``total``), level and limit value - recorded in ``plan_warnings`` in the same transaction as the thing that was added, so a rolled-back
    booking leaves no warning, and a business that stays near its limit is not told at every booking.
    """
    level = warning_level(used_after, allowed)
    if level is None or allowed is None:
        return
    period = month_label(tenant, now) if name in MONTHLY else "total"
    fresh = (await db.execute(
        pg_insert(PlanWarning).values(tenant_id=tenant.id, limit_name=name, period=period, level=level, allowed=allowed)
        .on_conflict_do_nothing(constraint="uq_plan_warnings_once").returning(PlanWarning.id)
    )).scalar()
    if fresh is not None:
        events.enqueue(db, tenant.id, LIMIT_WARNING_EVENT, "tenant", tenant.id, {"limit": name, "level": level, "used": used_after, "allowed": allowed, "period": period})


async def ensure_can_add_resource(db: AsyncSession, tenant: Tenant) -> None:
    """Raise :class:`PlanLimitReached` when one more *active* resource would pass the plan's limit."""
    plan = await plan_of(db, tenant)
    allowed = limit_value(plan, tenant, "resources")
    if allowed is None:
        return
    await _lock(db, tenant)
    used = await _count(db, select(func.count()).select_from(Resource).where(Resource.status == "active"))
    if used >= allowed:
        raise _refusal(plan, tenant, "max_resources", allowed, used)
    await note_usage(db, tenant, "resources", used + 1, allowed, dt.datetime.now(dt.UTC))


async def ensure_can_add_service(db: AsyncSession, tenant: Tenant) -> None:
    plan = await plan_of(db, tenant)
    allowed = limit_value(plan, tenant, "services")
    if allowed is None:
        return
    await _lock(db, tenant)
    used = await _count(db, select(func.count()).select_from(Service).where(Service.active.is_(True)))
    if used >= allowed:
        raise _refusal(plan, tenant, "max_services", allowed, used)
    await note_usage(db, tenant, "services", used + 1, allowed, dt.datetime.now(dt.UTC))


async def ensure_can_book(db: AsyncSession, tenant: Tenant, now: dt.datetime) -> None:
    """Raise :class:`PlanLimitReached` when one more booking would pass this month's limit. Holds the business's plan lock until the transaction ends."""
    plan = await plan_of(db, tenant)
    allowed = limit_value(plan, tenant, "bookings_per_month")
    if allowed is None:
        return
    await _lock(db, tenant)
    used = await bookings_this_month(db, tenant, now)
    if used >= allowed:
        raise _refusal(plan, tenant, "max_bookings_per_month", allowed, used)
    await note_usage(db, tenant, "bookings_per_month", used + 1, allowed, now)


async def ensure_can_add_site(db: AsyncSession, tenant: Tenant) -> None:
    """Raise :class:`PlanLimitReached` when one more website would pass the plan's limit. (No endpoint adds a site yet: this is what the first one will call.)"""
    plan = await plan_of(db, tenant)
    allowed = limit_value(plan, tenant, "sites")
    if allowed is None:
        return
    await _lock(db, tenant)
    used = int(await db.scalar(select(func.count()).select_from(Site).where(Site.tenant_id == tenant.id)) or 0)
    if used >= allowed:
        raise _refusal(plan, tenant, "max_sites", allowed, used)


async def ensure_can_issue_key(db: AsyncSession, tenant: Tenant, kind: str) -> None:
    """Raise :class:`PlanLimitReached` when one more live key of ``kind`` would pass the plan's limit (a recovery replaces the oldest instead: see ``api-keys/recover``)."""
    plan = await plan_of(db, tenant)
    allowed = limit_value(plan, tenant, "live_keys_per_kind")
    if allowed is None:
        return
    await _lock(db, tenant)
    used = (await live_keys(db, tenant)).get(kind, 0)
    if used >= allowed:
        raise _refusal(plan, tenant, "max_live_keys_per_kind", allowed, used)


def ensure_domain_count(plan: Plan, tenant: Tenant, count: int) -> None:
    """Raise :class:`PlanLimitReached` when ``count`` distinct domains would pass the plan's limit."""
    allowed = limit_value(plan, tenant, "domains")
    if allowed is not None and count > allowed:
        raise _refusal(plan, tenant, "max_domains", allowed, count)


def domain_change_due(plan: Plan, tenant: Tenant, site: Site) -> dt.datetime | None:
    """When the website address may next be changed, or ``None`` if it may be now (no cooldown on the plan, or it has not been changed since it was first set)."""
    days = limit_value(plan, tenant, "domain_change_days")
    if days is None or site.domain_changed_at is None:
        return None
    return site.domain_changed_at + dt.timedelta(days=days)


def cooldown_refusal(plan: Plan, tenant: Tenant, due: dt.datetime) -> PlanLimitReached:
    days = limit_value(plan, tenant, "domain_change_days")
    own = " (with limits set for your business)" if "domain_change_days" in overrides_of(tenant) else ""
    return PlanLimitReached(
        f"The {plan.name} plan{own} allows the website address to change once every {days} days; the next change is possible from {due:%Y-%m-%d %H:%M} UTC",
        extra={"limit": "domain_change_days", "allowed": days, "next_allowed_at": due.isoformat(), "plan": plan.code},
    )


async def message_allowed(db: AsyncSession, tenant: Tenant, channel: str, now: dt.datetime | None = None) -> bool:
    """Whether one more message may go out on ``channel`` this month (always true for email, and where the plan has no limit)."""
    column = METERED.get(channel)
    if column is None:
        return True
    allowed = limit_value(await plan_of(db, tenant), tenant, column.removeprefix("max_"))
    if allowed is None:
        return True
    now = now or dt.datetime.now(dt.UTC)
    await _lock_messages(db, tenant)  # held until the caller's transaction ends: the caller logs the message in it (or settles the place it just took) before then
    used = await messages_this_month(db, tenant, channel, now)
    if used >= allowed:
        return False
    await note_usage(db, tenant, column.removeprefix("max_"), used + 1, allowed, now)
    return True


async def ensure_can_send(db: AsyncSession, tenant: Tenant, channel: str, now: dt.datetime | None = None) -> None:
    """As :func:`message_allowed`, but raises :class:`PlanLimitReached` (for a message a person is waiting for, such as a sign-in code)."""
    if await message_allowed(db, tenant, channel, now):
        return
    plan = await plan_of(db, tenant)
    column = METERED[channel]
    allowed = limit_value(plan, tenant, column.removeprefix("max_"))
    assert allowed is not None  # (a message is refused only where there is a limit)
    raise _refusal(plan, tenant, column, allowed, allowed)


def warnings(limits: Limits, used: Usage) -> list[dict[str, object]]:
    """The limits the business is near or at right now (the same rule as the emails), for the admin to show."""
    out: list[dict[str, object]] = []
    for name in WARNED:
        allowed, count = getattr(limits, name), getattr(used, name)
        level = warning_level(count, allowed)
        if level is not None:
            out.append({"limit": name, "level": level, "used": count, "allowed": allowed, "percent": min(100, round(100 * count / allowed))})
    return out


def over_limit(limits: Limits, used: Usage) -> list[str]:
    """The limits a business is *over* (possible after a move to a smaller plan): what it has is kept, but nothing more can be added."""
    return [
        name for name in LIMIT_NAMES
        if (cap := getattr(limits, name)) is not None and getattr(used, name) > cap
    ]
