"""Outgoing webhooks: the URLs a business subscribed to booking events, and the deliveries made to them (blueprint §3, §14)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

#: Every event type the outbox carries, i.e. everything a webhook can subscribe to (``"*"`` = all of them).
WEBHOOK_EVENTS = (
    "booking.created", "booking.confirmed", "booking.rejected", "booking.cancelled", "booking.rescheduled",
    "booking.in_progress", "booking.waiting_for_parts", "booking.completed", "booking.no_show", "booking.reminder",
    "shift.updated", "review.created", "booking.schedule_conflict",
)
DELIVERY_STATUSES = ("pending", "delivered", "failed")


class WebhookEndpoint(UUIDMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "webhook_endpoints"

    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(String(200))
    #: AES-256-GCM ciphertext of the signing secret (it has to be readable to sign, so it cannot be hashed).
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    #: Event types, or ``["*"]`` for all.
    events: Mapped[list[str]] = mapped_column(ARRAY(Text))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    #: Failed attempts in a row; reset by a success. Reaching the limit switches the endpoint off.
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    disabled_reason: Mapped[str | None] = mapped_column(String(100))
    last_success_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookDelivery(UUIDMixin, TenantMixin, Base):
    """One event owed to one endpoint. Created when the event is published, sent (and retried) by the sweep."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        CheckConstraint("status IN ('pending','delivered','failed')", name="status"),
        #: the outbox delivers at least once; an event is owed to an endpoint once
        UniqueConstraint("endpoint_id", "outbox_event_id", name="uq_webhook_deliveries_endpoint_event"),
        Index("ix_webhook_deliveries_due", "next_attempt_at", postgresql_where=text("status = 'pending'")),
        Index("ix_webhook_deliveries_endpoint", "endpoint_id", "created_at"),
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"))
    #: Plain id (no foreign key): the outbox purges its events after a week.
    outbox_event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    event_type: Mapped[str] = mapped_column(String(60))
    #: The envelope that is sent (ids, statuses and times only), copied from the event.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    next_attempt_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: What the receiver answered (never its body) and, for a failure, why.
    response_status: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))
