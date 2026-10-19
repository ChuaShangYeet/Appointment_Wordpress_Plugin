"""Customer profiles, addresses and vehicles (scoped per tenant)."""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class Customer(UUIDMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "phone", name="uq_customers_tenant_phone"),
        CheckConstraint("gender IS NULL OR gender IN ('female','male','other')", name="gender"),
    )

    name: Mapped[str] = mapped_column(String(150))
    phone: Mapped[str] = mapped_column(String(32))
    email: Mapped[str | None] = mapped_column(String(254))
    #: Set when the person who signed in with the phone confirmed ``email`` through a link sent to it. Until then the email is only what somebody typed into a booking:
    #: it may be anyone's, so it does not sign in as this customer (it shows the bookings made with that address, nothing more).
    email_verified_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Spa and salon customers may say; ``female`` / ``male`` / ``other`` or unknown. Personal data: exported on request, cleared by erasure.
    gender: Mapped[str | None] = mapped_column(String(10))
    #: Set when the customer's personal data was erased at their request (PDPA); the row stays, anonymised.
    erased_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


#: email login looks customers up case-insensitively
Index("ix_customers_tenant_lower_email", Customer.tenant_id, func.lower(Customer.email))


class CustomerAddress(UUIDMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "customer_addresses"

    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"))
    line1: Mapped[str] = mapped_column(Text)
    city: Mapped[str | None] = mapped_column(String(100))
    postcode: Mapped[str | None] = mapped_column(String(16))
    #: The customer's map pin, or the point the typed address was looked up to (``app.services.geocoding``); measured against the service radius with PostGIS.
    latitude: Mapped[float | None] = mapped_column()
    longitude: Mapped[float | None] = mapped_column()


class Vehicle(UUIDMixin, TimestampMixin, TenantMixin, Base):
    __tablename__ = "vehicles"
    __table_args__ = (UniqueConstraint("tenant_id", "customer_id", "plate_no", name="uq_vehicles_customer_plate"),)

    #: Must match one of the workshop's SupportedVehicles (make/model) to be bookable.
    supported_vehicle_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supported_vehicles.id", ondelete="SET NULL")
    )

    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id", ondelete="CASCADE"))
    plate_no: Mapped[str] = mapped_column(String(20))
    make: Mapped[str | None] = mapped_column(String(80))
    model: Mapped[str | None] = mapped_column(String(80))
    year: Mapped[int | None] = mapped_column(Integer)
