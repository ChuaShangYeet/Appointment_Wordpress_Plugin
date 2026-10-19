"""What only the platform operator does (``X-Platform-Token``): see the plans, move a business to another, give one business limits of its own."""
from __future__ import annotations

import logging
import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.api.deps import DbDep, require_platform
from app.core.database import tenant_session
from app.core.exceptions import Conflict, NotFound, ValidationFailed
from app.core.phone import normalise_email
from app.models import Plan, Tenant
from app.schemas.plans import (
    PlanOut,
    PlanOverridesIn,
    PlanUsageOut,
    TenantPlanDetail,
    TenantPlanIn,
    TenantPlanSet,
    UsageWarningOut,
    limits_out,
)
from app.schemas.sites import SiteSignalOut
from app.schemas.tenant import OwnerEmailIn, OwnerEmailSet
from app.services import plans as plans_service
from app.services import sites as sites_service

log = logging.getLogger(__name__)
router = APIRouter(prefix="/platform", tags=["platform"], dependencies=[Depends(require_platform)])


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(db: DbDep) -> list[PlanOut]:
    """The plans and their limits (``null`` = no limit). The numbers live in the ``plans`` table: change them with an ordinary UPDATE."""
    return [PlanOut.of(p) for p in (await db.scalars(select(Plan).order_by(Plan.code))).all()]


@router.put("/tenants/{tenant_id}/plan", response_model=TenantPlanSet)
async def set_tenant_plan(tenant_id: uuid.UUID, body: TenantPlanIn, db: DbDep) -> TenantPlanSet:
    """Move a business to another plan, at once. Nothing it has is removed: on a smaller plan it keeps what it has and cannot add more until under the limits."""
    if await db.get(Plan, body.plan) is None:
        raise ValidationFailed(f"There is no plan called {body.plan!r}")
    changed = await db.execute(update(Tenant).where(Tenant.id == tenant_id).values(plan=body.plan))
    if changed.rowcount == 0:  # type: ignore[attr-defined]
        raise NotFound("Business not found")
    log.info("business %s moved to the %s plan", tenant_id, body.plan)
    return TenantPlanSet(tenant_id=tenant_id, plan=body.plan)


async def _detail(db: DbDep, tenant: Tenant) -> TenantPlanDetail:
    plan = await db.get(Plan, tenant.plan)
    assert plan is not None  # (a foreign key makes this impossible)
    limits = plans_service.limits_of(plan, tenant)
    async with tenant_session(tenant.id) as tenant_db:  # (what the business has is under Row-Level Security: read as the business)
        used = await plans_service.usage(tenant_db, tenant)
    return TenantPlanDetail(
        tenant_id=tenant.id, plan=plan.code, plan_limits=PlanOut.of(plan).limits, overrides=plans_service.overrides_of(tenant), limits=limits_out(limits),
        usage=PlanUsageOut(**asdict(used)), over_limit=plans_service.over_limit(limits, used),
        warnings=[UsageWarningOut(**w) for w in plans_service.warnings(limits, used)],
        site=SiteSignalOut(**signal) if (signal := sites_service.signal(await sites_service.first_site(db, tenant))) is not None else None,
    )


@router.get("/tenants/{tenant_id}/plan", response_model=TenantPlanDetail)
async def get_tenant_plan(tenant_id: uuid.UUID, db: DbDep) -> TenantPlanDetail:
    """One business's plan, the limits set for it alone, the limits that apply, what it uses now, and what it is near or over."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Business not found")
    return await _detail(db, tenant)


@router.put("/tenants/{tenant_id}/overrides", response_model=TenantPlanDetail)
async def set_tenant_overrides(tenant_id: uuid.UUID, body: PlanOverridesIn, db: DbDep) -> TenantPlanDetail:
    """Give one business limits of its own (``{"resources": 10, "sms_per_month": null}``: ``null`` = no limit). Replaces the whole set: a limit not named is the plan's again,
    ``{}`` removes them all. Nothing the business has is removed if a limit goes below what it uses; it just cannot add more. Takes effect at once."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Business not found")
    tenant.plan_overrides = body.model_dump(exclude_unset=True)
    await db.flush()
    log.info("business %s now has its own limits: %s", tenant_id, tenant.plan_overrides)
    return await _detail(db, tenant)


@router.put("/tenants/{tenant_id}/owner-email", response_model=OwnerEmailSet)
async def set_owner_email(tenant_id: uuid.UUID, body: OwnerEmailIn, db: DbDep) -> OwnerEmailSet:
    """Set (or, with ``null``, remove) the address a business's owner signs in with (``POST /v1/owner/login``). A business that signed itself up has one already; one the operator
    set up has none until this is called (or ``owner_email`` was given at set-up). The address is not confirmed by the owner here: it is the operator's word. 409 if another business has it."""
    tenant = await db.get(Tenant, tenant_id)
    if tenant is None:
        raise NotFound("Business not found")
    tenant.owner_email = normalise_email(str(body.email)) if body.email else None
    try:
        await db.flush()
    except IntegrityError as exc:
        raise Conflict("Another business already has that owner email address") from exc
    log.info("business %s: owner email %s", tenant_id, "set" if tenant.owner_email else "removed")
    return OwnerEmailSet(tenant_id=tenant_id, owner_email=tenant.owner_email)
