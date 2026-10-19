"""PDPA: the audit log of administrative actions and customers' access/erasure requests (blueprint §16)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, UUIDMixin


class AuditLog(UUIDMixin, TenantMixin, Base):
    """Who did what to which record, and when. Append-only: a database trigger refuses updates and deletes.

    Request bodies are never stored (they hold personal data and secrets); an entry says *that* something changed and
    which record it concerned, not what the new values were.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint("actor_type IN ('user','system')", name="actor_type"),
        Index("ix_audit_log_tenant_seq", "tenant_id", "seq"),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    actor_type: Mapped[str] = mapped_column(String(10))
    #: WordPress user id of the administrator; ``NULL`` for the system.
    actor_id: Mapped[str | None] = mapped_column(String(64))
    role: Mapped[str | None] = mapped_column(String(10))
    #: ``"PUT /v1/admin/operating-hours"`` for a request, or a named action such as ``"booking.health_notes.read"``.
    action: Mapped[str] = mapped_column(String(160))
    target_type: Mapped[str | None] = mapped_column(String(40))
    target_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class DataRequest(UUIDMixin, TenantMixin, Base):
    """A customer's request to see (``access``) or delete (``erasure``) their personal data, and how it was handled."""

    __tablename__ = "data_requests"
    __table_args__ = (
        CheckConstraint("kind IN ('access','erasure')", name="kind"),
        CheckConstraint("status IN ('pending','completed','rejected')", name="status"),
        # one open request of each kind per customer
        Index("uq_data_requests_open", "customer_id", "kind", unique=True, postgresql_where=text("status = 'pending'")),
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    requested_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: User id of the owner who completed or rejected it.
    processed_by: Mapped[str | None] = mapped_column(String(64))
    #: The reason, when rejected.
    note: Mapped[str | None] = mapped_column(Text)
