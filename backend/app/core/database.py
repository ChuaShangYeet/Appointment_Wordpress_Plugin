"""Async SQLAlchemy engine, session factory and tenant scoping."""
from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core import availability_cache
from app.core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """Lazily create the process-wide engine."""
    global _engine
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, pool_size=10, max_overflow=20)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Lazily create the session factory (``expire_on_commit=False`` for detached reads)."""
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    """Dispose the engine (app shutdown / test teardown)."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def set_tenant(session: AsyncSession, tenant_id: uuid.UUID) -> None:
    """Bind the current transaction to a tenant for Row-Level Security.

    ``set_config(..., true)`` is the bind-parameter-safe equivalent of
    ``SET LOCAL app.tenant_id = ...`` and lasts until the transaction ends.
    """
    await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(tenant_id)})
    availability_cache.note_tenant(session, tenant_id)


@asynccontextmanager
async def tenant_session(tenant_id: uuid.UUID | None) -> AsyncIterator[AsyncSession]:
    """One transaction per session; commits on success, rolls back on error.

    With ``tenant_id`` the transaction is RLS-scoped; without it only the
    non-RLS identity tables (tenants, sites, api_keys) are usable.
    """
    async with get_sessionmaker()() as session:
        async with session.begin():
            if tenant_id is not None:
                await set_tenant(session, tenant_id)
            yield session
        # committed: only now may cached availability of the tenants this transaction changed be dropped
        await availability_cache.flush_invalidations(session)
