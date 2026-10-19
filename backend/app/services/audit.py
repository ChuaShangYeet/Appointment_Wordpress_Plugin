"""The audit log (blueprint §16): a record of who did what to which record, kept for PDPA accountability.

Entries join the caller's transaction, so an action and its audit entry commit or roll back together: there is never a
committed change without its entry, nor an entry for something that did not happen. Administrative *requests* are
recorded automatically (see ``audit_admin_action`` in ``app.api.deps``); reads of personal data and the system's own
actions are recorded explicitly with :func:`record`.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.compliance import AuditLog


def record(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    action: str,
    actor_id: str | None = None,
    role: str | None = None,
    target_type: str | None = None,
    target_id: str | uuid.UUID | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """Add an entry; ``actor_id=None`` means the system acted. ``detail`` must not contain personal data."""
    entry = AuditLog(
        tenant_id=tenant_id, actor_type="user" if actor_id is not None else "system", actor_id=actor_id, role=role, action=action,
        target_type=target_type, target_id=str(target_id) if target_id is not None else None, detail=detail or {},
    )
    db.add(entry)
    return entry
