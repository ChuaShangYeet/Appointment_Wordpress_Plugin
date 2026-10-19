"""Redis cache for availability answers (blueprint §13.1: per tenant and date, invalidated by booking and settings events).

**Keys.** Every tenant has a *generation token* (``avail:gen:{tenant}``) and every date a second one
(``avail:gen:{tenant}:d:{date}``). A cached answer lives under ``avail:{tenant}:{tenant token}:{date token}:{hash of the request}``,
so replacing the tenant's token invalidates all of its answers at once and replacing a date's token invalidates that date alone; the
orphaned entries simply expire. A missing token (Redis was flushed, evicted or - for dates - expired it) is replaced by a fresh random
one, so an old entry can never be matched by accident. Date tokens expire after 15 minutes, which is far longer than an entry lives.

**Invalidation.** Nothing calls "invalidate" by hand. SQLAlchemy session hooks notice any write to a table
availability depends on (:data:`TRACKED_TABLES`) - ORM changes at flush time, and bulk ``update()`` /
``delete()`` statements as they run - and remember what it touched. After the transaction *commits*,
:func:`flush_invalidations` replaces the tokens, so a caller that has been told "booked" never
reads a stale answer. Only writes that are committed invalidate; a rolled-back one changes nothing.

* A change to a **booking or its reservations** - a new booking, a cancellation, a move, a hold that ran out - only affects the dates
  around it, so only those dates' tokens are replaced and every other date stays cached. Availability for a date D reads reservations that
  overlap ``[D 00:00, D+2 days 00:00)`` (a business day can run past midnight), so a reservation ``[lo, hi)`` can change the answer for
  dates from the one before ``lo``'s to the one of ``hi``'s; the dates replaced are a little wider than that on both sides (measured in UTC dates, which
  differ from the business's own by at most a day, so no time zone is needed here). A reservation spanning more than
  :data:`MAX_SPAN_DAYS` days, an unbounded one, or a bulk statement whose rows cannot be determined falls back to the whole tenant.
  Bulk ``update()`` / ``delete()`` statements give no dates by themselves, so the hook reads the periods of the rows the statement is about to change.
* **Anything else** that availability depends on (resources, shifts, days off, opening hours, settings, policies, services, stock, the tenant row)
  replaces the tenant's token, as before: those changes can affect any date.

**Why this ordering is safe.** A reader takes both tokens *before* computing and stores its answer under
them. If a write commits in between, the token changes and the stored answer is never read again;
if the write committed before the tokens were read, the computation sees it.

**Failure is never visible.** Redis being slow, down or wiped only means cache misses: reads and writes
swallow Redis errors. After a failure the cache is skipped for at least the entry TTL, because writes made
during the outage could not invalidate entries stored before it.

What the TTL still bounds: things that change with time alone - a pending hold expiring (the slot only
reappears when the entry does) and the minimum-notice cut-off moving (the endpoint re-filters on read).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import secrets
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from redis.exceptions import RedisError
from sqlalchemy import event, select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import ORMExecuteState, Session

from app.core.config import get_settings
from app.core.redis import get_redis

log = logging.getLogger(__name__)

#: Tables whose contents change what availability returns. ``tests/test_availability_cache.py`` records what
#: every vertical's availability request really reads and fails if a table is missing from this set.
TRACKED_TABLES = frozenset({
    "bookings", "booking_allocations",  # what is taken (and until when a pending hold lasts)
    "resources", "resource_shifts", "days_off", "operating_hours",
    "time_slot_settings", "table_open_rules", "booking_policies",
    "services", "service_resources", "service_products", "products",  # who can do what, and the parts notice
    "tenants",  # timezone, default policy, business location
})
#: Of those, the tables whose rows belong to particular dates (see the module notes); a change to any other tracked table can affect every date.
DATE_SCOPED_TABLES = frozenset({"bookings", "booking_allocations"})

#: Of a booking's columns only these change what is free; flags, notes, links and the like do not.
BOOKING_COLUMNS_THAT_MATTER = frozenset({"status", "start_at", "end_at", "hold_expires_at"})

#: Days of margin around a reservation, in UTC dates: 2 for what availability reads (a day before ``lo``, up to the day of ``hi``), 1 for the
#: difference between a UTC date and the business's own, and one more each way for safety.
MARGIN_BEFORE_DAYS = 3
MARGIN_AFTER_DAYS = 2
#: A reservation longer than this, or a transaction touching more dates than ``MAX_DATES``, clears the whole tenant instead.
MAX_SPAN_DAYS = 14
MAX_DATES = 200
#: How long a date's token lives. It only has to outlive the entries stored under it (``availability_cache_ttl_seconds``).
DATE_TOKEN_TTL_SECONDS = 900

_INFO_TENANT = "tenant_id"  # set by ``set_tenant``
_INFO_DIRTY = "availability_dirty_tenants"  # {tenant: set of dates, or None = the whole tenant}

_down_until = 0.0  # time.monotonic() before which Redis is not used


def reset_breaker() -> None:
    """Forget a previous Redis failure (tests)."""
    global _down_until
    _down_until = 0.0


def _ttl() -> int:
    return get_settings().availability_cache_ttl_seconds


def _usable() -> bool:
    return _ttl() > 0 and time.monotonic() >= _down_until


def _trip(exc: BaseException) -> None:
    global _down_until
    s = get_settings()
    _down_until = time.monotonic() + max(s.redis_backoff_seconds, float(s.availability_cache_ttl_seconds))
    log.warning("Availability cache disabled after a Redis error (%s: %s)", type(exc).__name__, exc)


# --------------------------------------------------------------------------- reading and storing
@dataclass(frozen=True)
class Lookup:
    """``key`` is where to store a fresh answer (``None``: do not cache); ``payload`` is a hit."""

    key: str | None
    payload: str | None


def _text(value: bytes | str | None) -> str | None:
    return value.decode() if isinstance(value, bytes) else value


def _request_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:32]


def _tenant_token_key(tenant_id: uuid.UUID) -> str:
    return f"avail:gen:{tenant_id}"


def _date_token_key(tenant_id: uuid.UUID, day: str) -> str:
    return f"avail:gen:{tenant_id}:d:{day}"


async def lookup_many(tenant_id: uuid.UUID, params: list[dict[str, Any]]) -> list[Lookup]:
    """Take the tenant's and the dates' current tokens *once* and look for a cached answer to each of ``params``.

    Every answer is stored under the tokens read here, so a write that lands while the caller is still computing
    (and replaces a token) leaves what it stores unreachable.
    """
    if not params or not _usable():
        return [Lookup(None, None) for _ in params]
    try:
        r = get_redis()
        days = sorted({str(p.get("date", "")) for p in params})
        token_keys = [_tenant_token_key(tenant_id), *(_date_token_key(tenant_id, d) for d in days)]
        pipe = r.pipeline(transaction=False)
        pipe.set(token_keys[0], secrets.token_hex(8), nx=True)  # (a missing token is replaced by a new random one, never by an old value)
        for key in token_keys[1:]:
            pipe.set(key, secrets.token_hex(8), nx=True, ex=DATE_TOKEN_TTL_SECONDS)
        pipe.mget(token_keys)
        tokens = [_text(t) for t in (await pipe.execute())[-1]]
        by_day = dict(zip(days, tokens[1:], strict=True))
        keys = [f"avail:{tenant_id}:{tokens[0]}:{by_day[str(p.get('date', ''))]}:{_request_hash(p)}" for p in params]
        return [Lookup(k, _text(v)) for k, v in zip(keys, await r.mget(keys), strict=True)]
    except (RedisError, OSError) as exc:
        _trip(exc)
        return [Lookup(None, None) for _ in params]


async def lookup(tenant_id: uuid.UUID, params: dict[str, Any]) -> Lookup:
    """One request's cached answer (see :func:`lookup_many`)."""
    return (await lookup_many(tenant_id, [params]))[0]


async def store(key: str | None, payload: str) -> None:
    if key is None or not _usable():
        return
    try:
        await get_redis().set(key, payload, ex=_ttl())
    except (RedisError, OSError) as exc:
        _trip(exc)


async def invalidate(tenant_id: uuid.UUID, dates: Iterable[dt.date] | None = None) -> None:
    """Drop every cached answer of one tenant, or - with ``dates`` - only those of the given dates."""
    if not _usable():
        return
    try:
        r = get_redis()
        if dates is None:
            await r.set(_tenant_token_key(tenant_id), secrets.token_hex(8))
            return
        pipe = r.pipeline(transaction=False)
        for day in dates:
            pipe.set(_date_token_key(tenant_id, day.isoformat()), secrets.token_hex(8), ex=DATE_TOKEN_TTL_SECONDS)
        await pipe.execute()
    except (RedisError, OSError) as exc:
        _trip(exc)


# --------------------------------------------------------------------------- which dates a reservation can affect
def dates_around(lo: dt.datetime | None, hi: dt.datetime | None) -> set[dt.date] | None:
    """The dates whose cached answers a reservation ``[lo, hi)`` can change; ``None`` = too many or unknown (clear the whole tenant)."""
    if lo is None or hi is None or hi < lo:
        return None
    first, last = lo.astimezone(dt.UTC).date(), hi.astimezone(dt.UTC).date()
    if (last - first).days > MAX_SPAN_DAYS:
        return None
    return {first + dt.timedelta(days=n) for n in range(-MARGIN_BEFORE_DAYS, (last - first).days + MARGIN_AFTER_DAYS + 1)}


# --------------------------------------------------------------------------- noticing writes

def _mark(session: Session, tenant_id: uuid.UUID | None, dates: set[dt.date] | None = None) -> None:
    """Remember a tenant as changed: for just ``dates``, or - with ``None`` - for every date."""
    if tenant_id is None:
        return
    dirty: dict[uuid.UUID, set[dt.date] | None] = session.info.setdefault(_INFO_DIRTY, {})
    if dates is None:
        dirty[tenant_id] = None
    elif tenant_id not in dirty:
        dirty[tenant_id] = set(dates)
    elif dirty[tenant_id] is not None:
        dirty[tenant_id].update(dates)  # type: ignore[union-attr]
        if len(dirty[tenant_id]) > MAX_DATES:  # type: ignore[arg-type]
            dirty[tenant_id] = None


def _changes_availability(obj: Any) -> bool:
    """Whether an updated row was modified in a way availability can notice."""
    insp = sa_inspect(obj)
    if getattr(type(obj), "__tablename__", None) == "bookings":
        return any(insp.attrs[name].history.has_changes() for name in BOOKING_COLUMNS_THAT_MATTER)
    return bool(insp.session and insp.session.is_modified(obj))


def _history(obj: Any, name: str) -> list[Any]:
    """The values an attribute has now and had before this flush."""
    hist = sa_inspect(obj).attrs[name].history
    return [v for v in (*hist.added, *hist.unchanged, *hist.deleted) if v is not None]


def _value(obj: Any, name: str, old: bool) -> Any:
    """An attribute's value after this flush, or - with ``old`` - before it."""
    hist = sa_inspect(obj).attrs[name].history
    if old and hist.deleted:
        return hist.deleted[0]
    return (hist.added or hist.unchanged or [None])[0]


def _object_dates(obj: Any, table: str) -> set[dt.date] | None:
    """The dates a changed booking / reservation row can affect, counting where it was as well as where it is."""
    out: set[dt.date] = set()
    if table == "booking_allocations":
        periods = _history(obj, "period")
        if not periods:
            return None
        for period in periods:
            span = dates_around(period.lower, period.upper)
            if span is None:
                return None
            out |= span
        return out
    # a booking that moved: where it was and where it is now, separately (not everything in between)
    for start, end in {(_value(obj, "start_at", old), _value(obj, "end_at", old)) for old in (False, True)}:
        span = dates_around(start, end)
        if span is None:
            return None
        out |= span
    return out


@event.listens_for(Session, "after_flush")
def _after_flush(session: Session, _ctx: Any) -> None:
    """ORM inserts, updates and deletes of tracked rows (the session still shows pre-flush state here)."""
    changed = [*session.new, *session.deleted, *(o for o in session.dirty if _changes_availability(o))]
    for obj in changed:
        table = getattr(type(obj), "__tablename__", None)
        if table in TRACKED_TABLES:
            tenant = (obj.id if table == "tenants" else getattr(obj, "tenant_id", None)) or session.info.get(_INFO_TENANT)
            _mark(session, tenant, _object_dates(obj, table) if table in DATE_SCOPED_TABLES else None)


def _bulk_dates(state: ORMExecuteState, table: str) -> set[dt.date] | None:
    """The dates of the rows an ``update()`` / ``delete()`` is about to change, read first (``None``: cannot tell, clear the tenant)."""
    where = getattr(state.statement, "whereclause", None)
    if where is None:
        return None
    from app.models import Booking, BookingAllocation

    columns = (Booking.start_at, Booking.end_at) if table == "bookings" else (BookingAllocation.period,)
    try:
        rows = state.session.execute(select(*columns).where(where).execution_options(autoflush=False)).all()
    except Exception:  # noqa: BLE001 - whatever the statement was, if it cannot be read first the safe answer is "every date"
        log.warning("availability cache: could not read the rows of a bulk %s statement; clearing the tenant", table, exc_info=True)
        return None
    out: set[dt.date] = set()
    for row in rows:
        span = dates_around(row[0].lower, row[0].upper) if table == "booking_allocations" else dates_around(row[0], row[1])
        if span is None:
            return None
        out |= span
    return out


def _bulk_update_matters(state: ORMExecuteState, table: str) -> bool:
    """Whether a bulk update to ``bookings`` touches a column availability reads (anything else - links, notes, flags - does not)."""
    if table != "bookings" or not state.is_update:
        return True
    try:
        names = {getattr(k, "key", k) for k in (getattr(state.statement, "_values", None) or {})}
    except Exception:  # noqa: BLE001
        return True
    return not names or bool(names & BOOKING_COLUMNS_THAT_MATTER)


@event.listens_for(Session, "do_orm_execute")
def _on_execute(state: ORMExecuteState) -> None:
    """Bulk ``insert()`` / ``update()`` / ``delete()`` statements, which never pass through the flush hook."""
    if not (state.is_insert or state.is_update or state.is_delete):
        return
    table = getattr(getattr(state.statement, "table", None), "name", None)
    if table not in TRACKED_TABLES:
        return
    tenant = state.session.info.get(_INFO_TENANT)
    if table in DATE_SCOPED_TABLES and not state.is_insert:
        if not _bulk_update_matters(state, table):
            return
        _mark(state.session, tenant, _bulk_dates(state, table))
    else:
        _mark(state.session, tenant)


def note_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Remember which tenant a session is scoped to (used to attribute bulk statements)."""
    session.sync_session.info[_INFO_TENANT] = tenant_id


async def flush_invalidations(session: AsyncSession) -> None:
    """Call after the session's transaction has committed: invalidate what it changed."""
    dirty: dict[uuid.UUID, set[dt.date] | None] | None = session.sync_session.info.pop(_INFO_DIRTY, None)
    for tenant_id, dates in (dirty or {}).items():
        await invalidate(tenant_id, dates)
