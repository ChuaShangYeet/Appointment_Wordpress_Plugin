"""The business's public profile and customers' reviews (blueprint §5-11, §12.3)."""
from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, SmallInteger, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin


class BusinessProfile(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """What the booking page shows about the business; one row per tenant.

    ``details`` holds what only one kind of business has (``cuisine_type`` for a restaurant, ``workshop_category`` for a
    workshop, ``profession_type`` for a professional). The business's location on the map and its service radius stay on
    the tenant (see ``PUT /admin/business-location``), so there is one place to change them.
    """

    __tablename__ = "business_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_business_profiles_tenant"),)

    business_name: Mapped[str] = mapped_column(String(150))
    description: Mapped[str | None] = mapped_column(Text)
    contact_no: Mapped[str | None] = mapped_column(String(32))
    contact_email: Mapped[str | None] = mapped_column(String(254))
    logo_image: Mapped[str | None] = mapped_column(Text)
    gallery_images: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default=text("'{}'::text[]"))
    social_media_links: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    #: The address on one line, as the booking page shows it. Written from the parts below when they are given.
    address: Mapped[str | None] = mapped_column(Text)
    #: The address in parts, checked against the business's country (``app.core.countries``); all ``NULL`` when only ``address`` was typed.
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    #: The state's code (``SGR``, ``NSW``); ``NULL`` for Singapore, which has none.
    state: Mapped[str | None] = mapped_column(String(3))
    postcode: Mapped[str | None] = mapped_column(String(10))
    google_maps_url: Mapped[str | None] = mapped_column(Text)
    #: The business's own review page, with ``{token}`` where the signed review token goes (completed-visit email).
    review_url: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class Review(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """A customer's rating of a completed appointment (blueprint §8: replaces ``customer_feedback`` on staff)."""

    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("booking_id", name="uq_reviews_booking"),
        CheckConstraint("rating BETWEEN 1 AND 5", name="rating"),
        CheckConstraint("status IN ('published','hidden')", name="status"),
        Index("ix_reviews_resource", "tenant_id", "resource_id", "created_at"),
    )

    booking_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"))
    customer_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("customers.id"))
    #: The staff member who served (``NULL`` where no staff member is involved, e.g. a restaurant).
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("resources.id", ondelete="SET NULL"))
    rating: Mapped[int] = mapped_column(SmallInteger)
    comment: Mapped[str | None] = mapped_column(Text)
    #: ``hidden`` reviews are kept but not shown publicly (moderation).
    status: Mapped[str] = mapped_column(String(10), default="published", server_default="published")
