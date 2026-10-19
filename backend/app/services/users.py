"""Administrative users: recording who logs in, and disabling them (blueprint §4.3).

See :mod:`app.models.identity`. WordPress remains the source of truth for *who someone is and what they may do*; this
records it, so that an owner can also cut a person off without waiting for their token to expire.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import Conflict, Forbidden, NotFound, ValidationFailed
from app.models.identity import User, UserRole


async def record_login(
    db: AsyncSession, tenant_id: uuid.UUID, external_id: str, role: str, resource_id: uuid.UUID | None, display_name: str | None
) -> User:
    """Note that ``external_id`` was just given ``role``. Raises :class:`Forbidden` if the user has been disabled.

    Upserts, so two first logins at the same moment cannot both insert (or fail on the unique constraint).
    """
    now = dt.datetime.now(dt.UTC)
    insert = pg_insert(User).values(tenant_id=tenant_id, external_id=external_id, display_name=display_name, last_seen_at=now)
    row = (await db.execute(
        insert.on_conflict_do_update(
            constraint="uq_users_tenant_external_id",
            set_={"last_seen_at": now, "display_name": func.coalesce(insert.excluded.display_name, User.display_name)},
        ).returning(User.id, User.status)
    )).one()
    if row.status == "disabled":
        raise Forbidden("This user has been disabled")
    role_insert = pg_insert(UserRole).values(tenant_id=tenant_id, user_id=row.id, role=role, resource_id=resource_id)
    await db.execute(role_insert.on_conflict_do_update(constraint="uq_user_roles_user_id_role", set_={"resource_id": role_insert.excluded.resource_id}))
    user = await db.get(User, row.id, populate_existing=True)
    assert user is not None
    return user


async def is_disabled(db: AsyncSession, external_id: str) -> bool:
    """Whether the (tenant-scoped) session's tenant has disabled this user. Unknown users are not disabled."""
    return (await db.scalar(select(User.status).where(User.external_id == external_id))) == "disabled"


async def roles_by_user(db: AsyncSession, user_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[UserRole]]:
    out: dict[uuid.UUID, list[UserRole]] = {}
    if user_ids:
        for r in (await db.scalars(select(UserRole).where(UserRole.user_id.in_(user_ids)).order_by(UserRole.role))).all():
            out.setdefault(r.user_id, []).append(r)
    return out


async def set_status(db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, status: str, actor_external_id: str) -> User:
    """Disable or re-enable a user. Refuses to lock the business out: not yourself, and never the last active owner."""
    # One change at a time per business: two owners disabling each other at once must not leave nobody in charge.
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"users:{tenant_id}"})
    user = await db.get(User, user_id, with_for_update=True, populate_existing=True)
    if user is None:
        raise NotFound("User not found")
    if status == "disabled" and user.status != "disabled":
        if user.external_id == actor_external_id:
            raise ValidationFailed("You cannot disable your own account")
        is_owner = await db.scalar(select(UserRole.id).where(UserRole.user_id == user.id, UserRole.role == "owner").limit(1))
        if is_owner is not None:
            other_owners = await db.scalar(
                select(func.count(func.distinct(User.id))).join(UserRole, UserRole.user_id == User.id)
                .where(UserRole.role == "owner", User.status == "active", User.id != user.id)
            ) or 0
            if other_owners == 0:
                raise Conflict("At least one active owner is required")
    user.status = status
    await db.flush()
    return user
