"""Schemas for outgoing webhook endpoints and their deliveries."""
from __future__ import annotations

import datetime as dt
import uuid
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.webhooks import WEBHOOK_EVENTS


def _clean_events(events: list[str]) -> list[str]:
    unknown = sorted(set(events) - set(WEBHOOK_EVENTS) - {"*"})
    if unknown:
        raise ValueError(f"Unknown event(s): {', '.join(unknown)}")
    if "*" in events:
        return ["*"]
    return sorted(set(events))


class WebhookCreate(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    #: Event types to send, or ``["*"]`` for all of them.
    events: list[str] = Field(min_length=1)
    description: str | None = Field(default=None, max_length=200)

    @field_validator("events")
    @classmethod
    def _valid_events(cls, value: list[str]) -> list[str]:
        return _clean_events(value)


class WebhookUpdate(BaseModel):
    """Only the fields sent change. ``active: true`` switches an endpoint that was turned off back on."""

    model_config = ConfigDict(extra="forbid")

    url: str | None = Field(default=None, min_length=1, max_length=500)
    events: list[str] | None = Field(default=None, min_length=1)
    description: str | None = Field(default=None, max_length=200)
    active: bool | None = None

    @field_validator("events")
    @classmethod
    def _valid_events(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else _clean_events(value)

    @model_validator(mode="after")
    def _nothing_required_is_removed(self) -> WebhookUpdate:
        for name in ("url", "events", "active"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be removed")
        return self


class WebhookOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    url: str
    description: str | None
    events: list[str]
    active: bool
    consecutive_failures: int
    disabled_reason: str | None
    last_success_at: dt.datetime | None
    last_failure_at: dt.datetime | None
    created_at: dt.datetime


class WebhookCreated(WebhookOut):
    #: The signing secret. Shown once, here and when it is rotated; only a ciphertext is kept.
    secret: str


class SecretOut(BaseModel):
    secret: str


class DeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    outbox_event_id: uuid.UUID
    status: str
    attempts: int
    response_status: int | None
    error: str | None
    created_at: dt.datetime
    last_attempt_at: dt.datetime | None
    delivered_at: dt.datetime | None
    next_attempt_at: dt.datetime


class PingResult(BaseModel):
    delivered: bool
    status_code: int | None
    error: str | None
