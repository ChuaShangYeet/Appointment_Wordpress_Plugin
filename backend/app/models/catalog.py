"""Catalog: services, retail products, supported vehicles."""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class Service(UUIDMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "services"
    __table_args__ = (
        CheckConstraint("service_category IS NULL OR service_category IN ('inspection','standard')", name="service_category"),
        CheckConstraint("location_type IN ('in_office','virtual','on_site')", name="location_type"),
        CheckConstraint("travel_buffer_minutes >= 0", name="travel_buffer_minutes"),
    )

    name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str | None] = mapped_column(Text)
    duration_minutes: Mapped[int] = mapped_column(Integer)
    #: Cleanup/turnaround time reserved after the service.
    buffer_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    #: The price is told after an inspection; ``price`` is then what is known up front (0, or a starting price).
    price_after_inspection: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    #: Overrides the tenant default policy when set.
    policy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("booking_policies.id", ondelete="SET NULL")
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    #: Salon: an add-on extends a base service's interval (an appointment has exactly one base service).
    is_addon: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    #: Field service: 'inspection' (inspection/consultation) or 'standard' (follow-up work).
    service_category: Mapped[str | None] = mapped_column(String(20))
    #: Field service: where it happens. On-site adds travel buffers and a service-radius check.
    location_type: Mapped[str] = mapped_column(String(20), default="in_office", server_default="in_office")
    #: Time blocked before *and* after an on-site appointment for travel.
    travel_buffer_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ServiceResource(TenantMixin, Base):
    """Which resources are qualified for a service. A service with no rows accepts any active staff."""

    __tablename__ = "service_resources"

    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    resource_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resources.id", ondelete="CASCADE"), primary_key=True
    )


class ServiceProduct(TenantMixin, Base):
    """A part a service needs (drives the lead-time disclaimer for automotive)."""

    __tablename__ = "service_products"

    service_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("services.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )


class Product(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """Inventory item: retail product or workshop part."""

    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("stock_status IN ('in_stock','out_of_stock','available_on_order')", name="stock_status"),
    )

    name: Mapped[str] = mapped_column(String(150))
    category: Mapped[str | None] = mapped_column(String(80))
    #: ``available_on_order`` parts trigger the lead-time disclaimer before an automotive booking.
    stock_status: Mapped[str] = mapped_column(String(20), default="in_stock", server_default="in_stock")
    estimated_lead_time_days: Mapped[int | None] = mapped_column(Integer)
    sku: Mapped[str | None] = mapped_column(String(64))
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    stock_qty: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class SupportedVehicle(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """Vehicle make/model a workshop accepts (Phase 3 compatibility matrix)."""

    __tablename__ = "supported_vehicles"

    make: Mapped[str] = mapped_column(String(80))
    model: Mapped[str] = mapped_column(String(80))
    year_from: Mapped[int | None] = mapped_column(Integer)
    year_to: Mapped[int | None] = mapped_column(Integer)
