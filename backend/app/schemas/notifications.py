"""Schemas for notification templates and the notification log."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

Channel = Literal["email", "sms", "whatsapp"]
NotifiableEvent = Literal[
    "booking.created", "booking.confirmed", "booking.rejected", "booking.cancelled", "booking.rescheduled",
    "booking.waiting_for_parts", "booking.completed", "booking.reminder",
]


class TemplateIn(BaseModel):
    """Wording for one event on one channel. Placeholders are ``$name`` / ``${name}``; see ``GET /admin/notification-placeholders``."""

    subject: str | None = Field(default=None, min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=2000)
    active: bool = True
    #: WhatsApp only: the Meta-approved template to send instead of free text.
    external_template: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def _text_is_valid(self) -> TemplateIn:
        from app.services.notifications import validate_template

        validate_template(self.subject, self.body)
        return self


class TemplateOut(BaseModel):
    event: str
    channel: str
    subject: str | None
    body: str
    active: bool
    external_template: str | None
    #: ``custom`` = written by the business, ``default`` = the built-in wording (email only).
    source: Literal["custom", "default"]


class NotificationLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event: str
    channel: str
    booking_id: uuid.UUID | None
    customer_id: uuid.UUID | None
    recipient: str | None
    status: str
    provider: str | None
    error: str | None
    attempts: int
    created_at: dt.datetime
    sent_at: dt.datetime | None


#: Longest reminder lead time (30 days) and most reminders per booking: more than that is not a reminder, it is spam.
MAX_REMINDER_HOURS = 24 * 30
MAX_REMINDERS = 5


class ReminderSettingsIn(BaseModel):
    """How long before a confirmed booking the customer is reminded: up to five lead times in hours (``[]`` = no reminders)."""

    model_config = ConfigDict(extra="forbid")

    hours: list[StrictInt] = Field(max_length=MAX_REMINDERS)  # (strict: "24" and true are not hours)

    @field_validator("hours")
    @classmethod
    def _sensible_lead_times(cls, value: list[int]) -> list[int]:
        if any(h < 1 or h > MAX_REMINDER_HOURS for h in value):
            raise ValueError(f"Each lead time must be between 1 and {MAX_REMINDER_HOURS} hours")
        return sorted(set(value), reverse=True)


class ReminderSettingsOut(BaseModel):
    #: The lead times in force for this business, longest first.
    hours: list[int]
    #: ``custom`` = set by the business, ``default`` = the server's setting.
    source: Literal["custom", "default"]
    default_hours: list[int]
