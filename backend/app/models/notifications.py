"""Customer notifications: editable message templates and the log of what was sent (blueprint §12.3, §15)."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

CHANNELS = ("email", "sms", "whatsapp")
#: Booking events a customer is told about. (``booking.created`` only when it starts out pending: see ``app.services.notifications``.)
NOTIFIABLE_EVENTS = (
    "booking.created", "booking.confirmed", "booking.rejected", "booking.cancelled", "booking.rescheduled",
    "booking.waiting_for_parts", "booking.completed", "booking.reminder", "booking.meeting_link_ready",
)
#: Sign-in messages, sent directly (their secret never passes through the outbox).
AUTH_EVENTS = ("auth.otp", "auth.magic_link", "auth.email_confirm")
#: ``refused``: the provider said it will never take this message (see ``PermanentDeliveryError``); it is not retried.
LOG_STATUSES = ("sending", "sent", "failed", "skipped", "refused")


class NotificationTemplate(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """A business's own wording for one event on one channel. Without a row the built-in default applies (email only)."""

    __tablename__ = "notification_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "event", "channel", name="uq_notification_templates_event_channel"),
        CheckConstraint("channel IN ('email','sms','whatsapp')", name="channel"),
    )

    event: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(12))
    #: Email only.
    subject: Mapped[str | None] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    #: ``False`` switches this event off on this channel (including the built-in default).
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    #: WhatsApp: the name of the Meta-approved template to send instead of free text.
    external_template: Mapped[str | None] = mapped_column(String(100))


class NotificationLog(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """One attempt series to tell one customer about one event on one channel. Holds no message text, and the recipient is masked."""

    __tablename__ = "notification_log"
    __table_args__ = (
        CheckConstraint("status IN ('sending','sent','failed','skipped','refused')", name="status"),
        CheckConstraint("channel IN ('email','sms','whatsapp')", name="channel"),
        #: the outbox delivers at least once; this makes the second delivery of an event a no-op per channel
        Index("uq_notification_log_event_channel", "outbox_event_id", "channel", unique=True, postgresql_where=text("outbox_event_id IS NOT NULL")),
        Index("ix_notification_log_booking", "booking_id"),
    )

    event: Mapped[str] = mapped_column(String(40))
    channel: Mapped[str] = mapped_column(String(12))
    #: Plain id (no foreign key): the outbox purges delivered events after a week, the log outlives them.
    outbox_event_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    booking_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="SET NULL"))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"))
    #: ``+60****0001`` / ``a***@example.com``: enough to recognise, not enough to contact.
    recipient: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10))
    provider: Mapped[str | None] = mapped_column(String(40))
    provider_message_id: Mapped[str | None] = mapped_column(String(160))
    error: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class BookingReminder(TenantMixin, Base):
    """A reminder that was queued for a booking (``kind`` is the lead time, e.g. ``24h``). Its primary key is what makes each one go out once."""

    __tablename__ = "booking_reminders"

    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), primary_key=True)
    kind: Mapped[str] = mapped_column(String(12), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
