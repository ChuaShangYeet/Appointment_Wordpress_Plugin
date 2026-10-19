"""Schemas for the audit log and PDPA data requests."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditEntryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    occurred_at: dt.datetime
    actor_type: str
    actor_id: str | None
    role: str | None
    action: str
    target_type: str | None
    target_id: str | None
    detail: dict[str, Any]


class DataRequestCreate(BaseModel):
    kind: Literal["access", "erasure"]


class DataRequestPublicOut(BaseModel):
    """What the customer sees of their own request."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    status: str
    requested_at: dt.datetime
    processed_at: dt.datetime | None
    note: str | None


class DataRequestOut(DataRequestPublicOut):
    customer_id: uuid.UUID
    processed_by: str | None


class DataRequestReject(BaseModel):
    note: str = Field(min_length=1, max_length=1000, description="Why the request cannot be carried out")


class DataRequestCompleted(BaseModel):
    request: DataRequestOut
    #: For an ``access`` request: everything held about the customer. ``None`` for an erasure.
    export: dict[str, Any] | None = None
