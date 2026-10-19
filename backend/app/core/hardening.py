"""Start-up safety checks: outside development and tests the application refuses to run in a setup that silently switches protection off.

The one that matters most is Row-Level Security. Every tenant table has a policy keyed on ``app.tenant_id``, but PostgreSQL ignores
policies for superusers and for roles with ``BYPASSRLS``, and a table's owner can switch them off with ``ALTER TABLE``. Connect the
API as one of those and every business can read every other business's data - and nothing visibly breaks. So at start-up the
application asks the database itself (``row_security_active``) whether the policies really apply to the role it is connected as,
and stops if they do not. See ``deploy/postgres/01_roles.sql`` for the two-role setup that satisfies it.
"""
from __future__ import annotations

import logging

from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.core.config import Settings

log = logging.getLogger(__name__)

#: Tables that carry ``tenant_id`` (or identify the tenant) on purpose without Row-Level Security: they are read to *find* the tenant.
NO_RLS_TABLES = frozenset({"tenants", "sites", "api_keys"})
#: Words that mark a secret as one nobody chose.
PLACEHOLDERS = ("change-me", "changeme", "change_me", "replace-me", "placeholder", "example", "password")
#: Redis on this machine needs no password; one anywhere else does.
LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
#: Environments in which the checks do not stop the application (they run on throw-away data).
RELAXED_ENVIRONMENTS = frozenset({"development", "test"})


class StartupRefused(RuntimeError):
    """The configuration would run, but unsafely; the message says what to fix."""


def settings_problems(settings: Settings) -> list[str]:
    """Unsafe values in the settings (pure: nothing is contacted)."""
    problems: list[str] = []
    for name, value in (("JWT_SECRET", settings.jwt_secret), ("PLATFORM_ADMIN_TOKEN", settings.platform_admin_token)):
        if any(word in value.lower() for word in PLACEHOLDERS):
            problems.append(f"{name} is still a placeholder value")
    url = make_url(settings.database_url)
    if (url.username, url.password) == ("booking", "booking"):
        problems.append("DATABASE_URL still uses the example credentials booking:booking")
    redis = urlsplit(settings.redis_url)
    if redis.hostname is not None and redis.hostname not in LOCAL_HOSTS and not redis.password:
        problems.append("REDIS_URL has no password: anyone who can reach Redis could read the pending sign-up requests and the hashes of sign-in codes (redis://:PASSWORD@host:6379/0)")
    if "*" in settings.cors_origins:
        problems.append("CORS_ORIGINS contains '*': any website could call the API from a visitor's browser")
    if settings.webhook_allow_insecure:
        problems.append("WEBHOOK_ALLOW_INSECURE=true switches off the protection against webhooks that reach the internal network")
    if not settings.rate_limit_enabled:
        problems.append("RATE_LIMIT_ENABLED=false: the public endpoints (OTP requests above all) are not rate limited")
    return problems


async def database_problems(conn: AsyncConnection) -> tuple[list[str], list[str]]:
    """``(errors, warnings)`` about the role this connection is logged in as."""
    errors: list[str] = []
    warnings: list[str] = []
    role = (await conn.execute(text("SELECT rolname, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"))).one()
    if role.rolsuper:
        errors.append(f"the database role '{role.rolname}' is a superuser: Row-Level Security does not apply to it")
    if role.rolbypassrls:
        errors.append(f"the database role '{role.rolname}' has BYPASSRLS: Row-Level Security does not apply to it")
    unprotected: list[str] = list((await conn.execute(text(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname <> ALL(:skip) "
        "AND EXISTS (SELECT 1 FROM pg_attribute a WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped) "
        "AND NOT row_security_active(c.oid) ORDER BY c.relname"
    ), {"skip": sorted(NO_RLS_TABLES)})).scalars().all())
    if unprotected:
        shown = ", ".join(unprotected[:5]) + (f" and {len(unprotected) - 5} more" if len(unprotected) > 5 else "")
        errors.append(f"Row-Level Security is not in force for {len(unprotected)} tenant table(s) for this role: {shown}")
    owns = await conn.scalar(text(
        "SELECT EXISTS (SELECT 1 FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND pg_get_userbyid(c.relowner) = current_user)"
    ))
    if owns and not role.rolsuper:
        warnings.append(f"the application connects as '{role.rolname}', which owns the tables and could switch Row-Level Security off: run migrations "
                        "as a separate owner role (MIGRATION_DATABASE_URL, see deploy/postgres/01_roles.sql)")
    return errors, warnings


async def enforce(settings: Settings, engine: AsyncEngine) -> None:
    """Refuse to start (raise :class:`StartupRefused`) when the settings or the database role are unsafe; no-op in development and tests.

    ``ALLOW_UNSAFE_STARTUP=true`` turns the refusal into a critical log line - for an emergency, never as a setting to keep.
    """
    if settings.environment in RELAXED_ENVIRONMENTS:
        return
    errors = settings_problems(settings)
    async with engine.connect() as conn:
        db_errors, warnings = await database_problems(conn)
    errors += db_errors
    for warning in warnings:
        log.warning("start-up check: %s", warning)
    if not errors:
        return
    if settings.allow_unsafe_startup:
        for error in errors:
            log.critical("start-up check (ignored because ALLOW_UNSAFE_STARTUP=true): %s", error)
        return
    raise StartupRefused("Refusing to start: " + "; ".join(errors))
