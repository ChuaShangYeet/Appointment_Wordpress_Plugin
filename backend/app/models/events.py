"""Booking status history and the transactional outbox (blueprint §2.3, §12.3)."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Identity, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, UUIDMixin

ACTOR_TYPES = ("customer", "staff", "system")


class BookingStatusHistory(UUIDMixin, TenantMixin, Base):
    """Append-only record of every status a booking has been in, who moved it and when.

    ``from_status`` is ``NULL`` for the row written when the booking was created. ``actor_id`` is the customer id,
    the WordPress user id of the staff member, or ``NULL`` for the system (for example an expired hold).
    """

    __tablename__ = "booking_status_history"
    __table_args__ = (
        CheckConstraint("actor_type IN ('customer','staff','system')", name="actor_type"),
        Index("ix_booking_status_history_booking", "booking_id", "seq"),
    )

    #: Stable order within a booking even when two changes share a timestamp.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))
    actor_type: Mapped[str] = mapped_column(String(10))
    actor_id: Mapped[str | None] = mapped_column(String(64))
    changed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())


class OutboxEvent(UUIDMixin, TenantMixin, Base):
    """A domain event written in the same transaction as the change it describes.

    Publishing (customer and staff messages, outgoing webhooks, calendar sync) happens afterwards and at least once: see ``app.services.outbox``. The
    payload holds ids, statuses and times only - never names, phone numbers or health notes.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_events_pending", "available_at", postgresql_where=text("published_at IS NULL")),
    )

    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True))
    event_type: Mapped[str] = mapped_column(String(60))
    aggregate_type: Mapped[str] = mapped_column(String(30))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    #: Not before this time (retry back-off); ``published_at`` set once a publisher has accepted the event.
    available_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.clock_timestamp())
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
