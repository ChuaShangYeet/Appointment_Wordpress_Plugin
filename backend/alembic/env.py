"""Alembic environment (async engine, URL taken from application settings)."""
from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.models import Base

config = context.config
target_metadata = Base.metadata


def _include_object(obj: object, name: str | None, type_: str, reflected: bool, compare_to: object) -> bool:
    """Skip tables owned by extensions (PostGIS creates spatial_ref_sys in public)."""
    return not (type_ == "table" and reflected and name == "spatial_ref_sys")


def _do_run(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, compare_type=True, include_object=_include_object
    )
    with context.begin_transaction():
        context.run_migrations()
        # The migration bookkeeping is the owner's alone: the limited application role of deploy/postgres/01_roles.sql gets the
        # default privileges on every table the owner creates, which would include this one. (No-op where that role does not exist.)
        context.execute(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'booking_app') THEN "
            "REVOKE ALL ON TABLE alembic_version FROM booking_app; END IF; END $$"
        )


def _url() -> str:
    """Migrations run as the table owner (``MIGRATION_DATABASE_URL``) when one is configured, otherwise as ``DATABASE_URL``."""
    settings = get_settings()
    return settings.migration_database_url or settings.database_url


async def _run_online() -> None:
    engine = create_async_engine(_url())
    async with engine.connect() as conn:
        await conn.run_sync(_do_run)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(_run_online())
