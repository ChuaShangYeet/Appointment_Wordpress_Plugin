"""Schemas for administrative users."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict


class RoleOut(BaseModel):
    role: str
    #: A staff role pinned to that person's own schedule.
    resource_id: uuid.UUID | None


class UserOut(BaseModel):
    id: uuid.UUID
    #: The WordPress user id.
    external_id: str
    display_name: str | None
    status: str
    last_seen_at: dt.datetime | None
    roles: list[RoleOut]


class UserStatusIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "disabled"]
