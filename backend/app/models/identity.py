"""Administrative users of a business and the roles they have used (blueprint §4.3, §12.3).

Authentication itself is WordPress's: it exchanges the tenant's secret key for a short-lived JWT naming the logged-in
user and the role their WordPress capabilities map to. These tables record who those users are and what role they were
last given, and let an owner **disable** a user. A disabled user gets no new token, and the tokens they already hold
stop working at once (checked on every request), instead of lingering until they expire.
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

USER_STATUSES = ("active", "disabled")
USER_ROLES = ("owner", "manager", "staff")


class User(UUIDMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id", name="uq_users_tenant_external_id"),
        CheckConstraint("status IN ('active','disabled')", name="status"),
    )

    #: The WordPress user id the plugin puts in the token exchange (the ``sub`` of the JWT).
    external_id: Mapped[str] = mapped_column(String(100))
    display_name: Mapped[str | None] = mapped_column(String(150))
    status: Mapped[str] = mapped_column(String(10), default="active", server_default="active")
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class UserRole(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """A role WordPress has asserted for the user; ``resource_id`` pins a staff role to that person's own schedule."""

    __tablename__ = "user_roles"
    __table_args__ = (
        UniqueConstraint("user_id", "role", name="uq_user_roles_user_id_role"),
        CheckConstraint("role IN ('owner','manager','staff')", name="role"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(10))
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("resources.id", ondelete="SET NULL"))
