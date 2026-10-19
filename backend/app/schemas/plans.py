"""Plans and limits as the API shows them."""
from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models import Plan
from app.schemas.sites import DomainChangeOut, SiteSignalOut
from app.services.plans import Limits


class PlanLimitsOut(BaseModel):
    """``null`` = no limit; ``0`` = none at all."""

    resources: int | None
    services: int | None
    bookings_per_month: int | None
    whatsapp_per_month: int | None
    sms_per_month: int | None
    #: Websites, distinct domains (www and non-www are one) and live API keys of each kind (publishable, secret) a business may have.
    sites: int | None
    domains: int | None
    live_keys_per_kind: int | None
    #: Days that must pass between two changes of the website address (``null`` = may change at will).
    domain_change_days: int | None


class PlanOut(BaseModel):
    code: str
    name: str
    limits: PlanLimitsOut

    @classmethod
    def of(cls, plan: Plan, limits: Limits | None = None) -> "PlanOut":
        """The plan; ``limits`` are what applies to a particular business (the plan's own numbers, with that business's overrides) - the plan's when left out."""
        return cls(
            code=plan.code, name=plan.name,
            limits=PlanLimitsOut(
                resources=plan.max_resources, services=plan.max_services, bookings_per_month=plan.max_bookings_per_month,
                whatsapp_per_month=plan.max_whatsapp_per_month, sms_per_month=plan.max_sms_per_month,
                sites=plan.max_sites, domains=plan.max_domains, live_keys_per_kind=plan.max_live_keys_per_kind, domain_change_days=plan.domain_change_days,
            ) if limits is None else limits_out(limits),
        )


def limits_out(limits: Limits) -> PlanLimitsOut:
    return PlanLimitsOut(
        resources=limits.resources, services=limits.services, bookings_per_month=limits.bookings_per_month,
        whatsapp_per_month=limits.whatsapp_per_month, sms_per_month=limits.sms_per_month,
        sites=limits.sites, domains=limits.domains, live_keys_per_kind=limits.live_keys_per_kind, domain_change_days=limits.domain_change_days,
    )


class PlanUsageOut(BaseModel):
    resources: int
    services: int
    bookings_per_month: int
    whatsapp_per_month: int
    sms_per_month: int
    sites: int
    domains: int
    #: The most live keys of one kind the business has.
    live_keys_per_kind: int


class UsageWarningOut(BaseModel):
    """A limit the business is near (``PLAN_WARNING_PERCENT`` of it) or has reached."""

    limit: str
    level: Literal["near", "reached"]
    used: int
    allowed: int
    percent: int


class TenantPlanOut(BaseModel):
    #: The plan; its ``limits`` are the ones that apply to this business (the plan's, with any the operator set for it alone).
    plan: PlanOut
    #: The limits whose numbers were set for this business alone.
    customised: list[str]
    usage: PlanUsageOut
    #: The limits the business is over (possible after a move to a smaller plan): what it has is kept, but nothing more can be added.
    over_limit: list[str]
    #: The limits it is near or at now. The business is also emailed, once, when it gets there.
    warnings: list[UsageWarningOut]
    #: How often the website address may change, and when the next change is possible.
    domain_change: DomainChangeOut


class PlanOverridesIn(BaseModel):
    """Limits for one business that differ from its plan. A limit named here (a number, or ``null`` for no limit) replaces the plan's; one left out is the plan's. This replaces the whole set."""

    model_config = ConfigDict(extra="forbid")

    resources: int | None = Field(default=None, ge=0)
    services: int | None = Field(default=None, ge=0)
    bookings_per_month: int | None = Field(default=None, ge=0)
    whatsapp_per_month: int | None = Field(default=None, ge=0)
    sms_per_month: int | None = Field(default=None, ge=0)
    sites: int | None = Field(default=None, ge=0)
    domains: int | None = Field(default=None, ge=0)
    live_keys_per_kind: int | None = Field(default=None, ge=0)
    #: Days between two changes of the website address (``null`` = may change at will).
    domain_change_days: int | None = Field(default=None, ge=0)


class TenantPlanDetail(BaseModel):
    tenant_id: uuid.UUID
    plan: str
    #: What the plan alone says, what was set for this business, and what applies (the two together).
    plan_limits: PlanLimitsOut
    overrides: dict[str, int | None]
    limits: PlanLimitsOut
    usage: PlanUsageOut
    over_limit: list[str]
    warnings: list[UsageWarningOut]
    #: The business's site and the address its plugin last reported: a signal only (a mismatch blocks nothing).
    site: SiteSignalOut | None


class TenantPlanIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: str


class TenantPlanSet(BaseModel):
    tenant_id: uuid.UUID
    plan: str
