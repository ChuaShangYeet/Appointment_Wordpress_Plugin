"""Catalog schemas."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ServiceBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = None
    duration_minutes: int = Field(gt=0, le=1440)
    buffer_minutes: int = Field(default=0, ge=0, le=480)
    price: Decimal = Field(default=Decimal("0"), ge=0)
    #: The price is told after the business has seen the job; ``price`` is then what is known up front (0, or a starting price).
    price_after_inspection: bool = False
    policy_id: uuid.UUID | None = None
    active: bool = True
    #: Salon: an optional extra that lengthens a base service (exactly one base service per appointment).
    is_addon: bool = False
    #: Field service only.
    service_category: Literal["inspection", "standard"] | None = None
    location_type: Literal["in_office", "virtual", "on_site"] = "in_office"
    #: Minutes blocked before *and* after an on-site appointment for travel.
    travel_buffer_minutes: int = Field(default=0, ge=0, le=240)


class ServiceCreate(ServiceBase):
    #: Resources qualified for this service; empty means any active staff.
    resource_ids: list[uuid.UUID] = Field(default_factory=list)


#: Service settings that always hold a value: a change may leave one out (kept), but cannot empty it (a null would fail in the database).
_SERVICE_REQUIRED = ("name", "duration_minutes", "buffer_minutes", "price", "price_after_inspection", "active", "is_addon", "location_type", "travel_buffer_minutes")


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=1440)
    buffer_minutes: int | None = Field(default=None, ge=0, le=480)
    price: Decimal | None = Field(default=None, ge=0)
    price_after_inspection: bool | None = None
    policy_id: uuid.UUID | None = None
    active: bool | None = None
    resource_ids: list[uuid.UUID] | None = None
    is_addon: bool | None = None
    service_category: Literal["inspection", "standard"] | None = None
    location_type: Literal["in_office", "virtual", "on_site"] | None = None
    travel_buffer_minutes: int | None = Field(default=None, ge=0, le=240)

    @model_validator(mode="after")
    def _no_nulls(self) -> ServiceUpdate:
        emptied = [k for k in _SERVICE_REQUIRED if k in self.model_fields_set and getattr(self, k) is None]
        if emptied:
            raise ValueError(f"cannot be empty: {', '.join(emptied)}")
        return self


class ServiceOut(ServiceBase):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class SupportedVehicleIn(BaseModel):
    make: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=80)
    #: Optional model-year limits; when set, a vehicle must state a year inside them.
    year_from: int | None = Field(default=None, ge=1900, le=2100)
    year_to: int | None = Field(default=None, ge=1900, le=2100)

    @model_validator(mode="after")
    def _check(self) -> SupportedVehicleIn:
        if self.year_from and self.year_to and self.year_to < self.year_from:
            raise ValueError("year_to before year_from")
        return self


class SupportedVehicleOut(SupportedVehicleIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class ProductIn(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    category: str | None = Field(default=None, max_length=80)
    sku: str | None = Field(default=None, max_length=64)
    price: Decimal = Field(default=Decimal("0"), ge=0)
    stock_qty: int = Field(default=0, ge=0)
    stock_status: Literal["in_stock", "out_of_stock", "available_on_order"] = "in_stock"
    estimated_lead_time_days: int | None = Field(default=None, ge=0, le=365)

    @model_validator(mode="after")
    def _check(self) -> ProductIn:
        if self.stock_status == "available_on_order" and self.estimated_lead_time_days is None:
            raise ValueError("available_on_order parts need estimated_lead_time_days")
        return self


class ProductOut(ProductIn):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID


class ProductPublicOut(BaseModel):
    """Catalog entry shown on the booking page; the price is informational and stock is a status, not a count."""

    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    category: str | None
    price: Decimal
    stock_status: Literal["in_stock", "out_of_stock", "available_on_order"]
    estimated_lead_time_days: int | None = None


class ServicePartsIn(BaseModel):
    """Parts a service needs; replaces the previous list."""

    product_ids: list[uuid.UUID] = Field(default_factory=list)
