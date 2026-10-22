"""Identity & tenancy: Tenant, Site, ApiKey.

These three tables are intentionally *not* row-level-secured: they are read
before a tenant is known (key lookup during ``resolve_tenant``).
"""
from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantMixin, TimestampMixin, UUIDMixin

BUSINESS_TYPES = ("spa", "salon", "restaurant", "automotive", "professional")


class Plan(Base):
    """What a business may have (``app.services.plans``). Not tenant data: one table for the whole service, changed by the operator, read by everyone.

    Every limit is a number or ``NULL`` for no limit; ``0`` means none at all.
    """

    __tablename__ = "plans"
    __table_args__ = tuple(
        CheckConstraint(f"{column} IS NULL OR {column} >= 0", name=column)
        for column in (
            "max_resources", "max_services", "max_bookings_per_month", "max_whatsapp_per_month", "max_sms_per_month",
            "max_sites", "max_domains", "max_live_keys_per_kind", "domain_change_days",
        )
    )

    code: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    max_resources: Mapped[int | None] = mapped_column(Integer)
    max_services: Mapped[int | None] = mapped_column(Integer)
    max_bookings_per_month: Mapped[int | None] = mapped_column(Integer)
    max_whatsapp_per_month: Mapped[int | None] = mapped_column(Integer)
    max_sms_per_month: Mapped[int | None] = mapped_column(Integer)
    #: Websites (``sites``) a business may have.
    max_sites: Mapped[int | None] = mapped_column(Integer)
    #: Distinct domains (the www and non-www spellings are one) its websites may be served from. A plan that sets this *derives* a site's allowed origins from its website address.
    max_domains: Mapped[int | None] = mapped_column(Integer)
    #: Live API keys of each kind (publishable, secret) at one time: one in use and a spare, so a key can be rotated.
    max_live_keys_per_kind: Mapped[int | None] = mapped_column(Integer)
    #: Days that must pass between two changes of the website address (``NULL`` = may change at will). The address given at sign-up is not a change.
    domain_change_days: Mapped[int | None] = mapped_column(Integer)


class Tenant(UUIDMixin, TimestampMixin, Base):
    """A client business. ``id`` doubles as its tenant id."""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint("latitude IS NULL OR latitude BETWEEN -90 AND 90", name="latitude"),
        CheckConstraint("longitude IS NULL OR longitude BETWEEN -180 AND 180", name="longitude"),
        CheckConstraint("service_radius_km IS NULL OR service_radius_km > 0", name="service_radius_km"),
        CheckConstraint("country ~ '^[A-Z]{2}$'", name="country"),  # (which codes are countries is app.core.countries' to say)
        #: one business per owner address (only those that signed themselves up have one)
        Index("uq_tenants_owner_email", "owner_email", unique=True, postgresql_where=text("owner_email IS NOT NULL")),
    )

    name: Mapped[str] = mapped_column(String(150))
    business_type: Mapped[str] = mapped_column(String(20), default="spa")
    #: ISO 3166 code (``MY``; ``app.core.countries``): the time zones it may choose from, its phone code, postcodes, states and currency follow from it.
    country: Mapped[str] = mapped_column(String(2), default="MY", server_default="MY")
    #: The plan whose limits apply (``plans.code``): ``free`` or ``paid`` to begin with. A business set up by the operator is ``paid`` unless said otherwise.
    plan: Mapped[str] = mapped_column(String(20), ForeignKey("plans.code"), default="paid", server_default="paid")
    #: Limits set for this business alone, by the operator: ``{"resources": 10, "sms_per_month": null}`` (``null`` = no limit; a limit that is not named is the plan's own).
    plan_overrides: Mapped[dict[str, int | None]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    #: An IANA name from the country's list; a change of clock (daylight saving) is the zone's own business, not a setting.
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kuala_Lumpur")
    status: Mapped[str] = mapped_column(String(20), default="active")
    #: The owner's email address, confirmed by a link before the business existed (self-service sign-up); ``NULL`` for one the operator set up. Stored lower-case.
    owner_email: Mapped[str | None] = mapped_column(String(254))
    #: The version of the terms accepted at sign-up, and when.
    terms_version: Mapped[str | None] = mapped_column(String(40))
    terms_accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: Calling code (digits, no plus) that completes a national phone number typed without one: see ``app.core.phone``.
    phone_country_code: Mapped[str] = mapped_column(String(3), default="60", server_default="60")
    #: Hours before a confirmed booking at which reminders go out. ``NULL`` = the server's ``REMINDER_HOURS``; ``[]`` = this business wants none.
    reminder_hours: Mapped[list[int] | None] = mapped_column(ARRAY(Integer))
    #: Default BookingPolicy (GlobalSettings.default_policy_id). Plain UUID to avoid a FK cycle.
    default_policy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    #: Business location (geo point) and how far on-site services travel from it (field service).
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    service_radius_km: Mapped[float | None] = mapped_column(Float)


class Site(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """A website/app connected to a tenant (e.g. one WordPress install)."""

    __tablename__ = "sites"

    name: Mapped[str] = mapped_column(String(150))
    domain: Mapped[str | None] = mapped_column(String(255))
    #: Per-site CORS allowlist, enforced when a publishable key is used from a browser.
    allowed_origins: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list, server_default="{}")
    #: HMAC secret for /v1/sync webhooks, AES-256-GCM encrypted.
    webhook_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    #: When the website address was last *changed* (not set for the first time): ``plans.domain_change_days`` counts from here.
    domain_changed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    #: The address the WordPress plugin last said it runs on (``POST /auth/token``, ``site_url``): a signal for the operator, never a reason to refuse anything.
    last_site_url: Mapped[str | None] = mapped_column(String(300))
    last_site_url_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ApiKey(UUIDMixin, TimestampMixin, TenantMixin, Base):
    """Hashed API key. ``publishable`` keys are browser-safe; ``secret`` keys are server-only."""

    __tablename__ = "api_keys"

    site_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("sites.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12))
    prefix: Mapped[str] = mapped_column(String(8))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class PlanWarning(UUIDMixin, TenantMixin, Base):
    """A business was told it is ``near`` (at the warning percentage) or has ``reached`` a limit. One row per limit, period, level and limit value, so it is told once."""

    __tablename__ = "plan_warnings"
    __table_args__ = (
        CheckConstraint("level IN ('near','reached')", name="level"),
        UniqueConstraint("tenant_id", "limit_name", "period", "level", "allowed", name="uq_plan_warnings_once"),
    )

    limit_name: Mapped[str] = mapped_column(String(40))
    #: ``2026-10`` for a limit per month (the business's own month), ``total`` for a limit on what it has.
    period: Mapped[str] = mapped_column(String(10))
    level: Mapped[str] = mapped_column(String(10))
    #: The limit at the time: a changed limit is warned about afresh.
    allowed: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=text("clock_timestamp()"))
