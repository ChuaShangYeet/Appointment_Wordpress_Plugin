"""Setting a business up: the business, its booking rules, its first site and its first pair of keys. Used by the platform operator (``POST /auth/tenants``) and by
self-service sign-up (``POST /signup/verify``), which differ only in who asks and which plan the business starts on."""
from __future__ import annotations

import datetime as dt
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import countries
from app.core.database import set_tenant
from app.core.exceptions import ValidationFailed
from app.core.security import encrypt_field, generate_api_key
from app.models import ApiKey, BookingPolicy, BusinessProfile, Plan, Site, Tenant
from app.schemas.scheduling import PolicyCreate
from app.schemas.tenant import BusinessSetup, TenantCreated
from app.services import sites


async def create_business(
    db: AsyncSession, setup: BusinessSetup, *, plan: str, owner_email: str | None = None, terms_version: str | None = None
) -> TenantCreated:
    """Create the business and return its keys (shown this once; only hashes are kept). ``owner_email`` is stored as given: normalise it first."""
    plan_row = await db.get(Plan, plan)
    if plan_row is None:
        raise ValidationFailed(f"There is no plan called {plan!r}")
    domain, allowed_origins = sites.site_for_new_business(plan_row, setup.domain, setup.allowed_origins)  # (before anything is written: a plan that limits domains needs the website)
    country = countries.get(setup.country)
    tenant = Tenant(
        name=setup.name, business_type=setup.business_type, country=country.code, timezone=setup.timezone, phone_country_code=setup.phone_country_code,
        plan=plan, owner_email=owner_email, terms_version=terms_version, terms_accepted_at=dt.datetime.now(dt.UTC) if terms_version else None,
    )
    db.add(tenant)
    await db.flush()
    await set_tenant(db, tenant.id)  # the policy and profile are under row-level security: work as the new business
    # Without a default policy a business cannot show times or take bookings; start it on the same rules POST /admin/policies would give.
    policy = BookingPolicy(tenant_id=tenant.id, **PolicyCreate(name="Default").model_dump(exclude={"make_default"}))
    db.add(policy)
    await db.flush()
    tenant.default_policy_id = policy.id
    address = setup.address_for(country)  # (already checked when the request was read)
    if address is not None:
        db.add(BusinessProfile(tenant_id=tenant.id, business_name=setup.name, **countries.profile_columns(country, address)))
    webhook_secret = secrets.token_urlsafe(32)
    site = Site(
        tenant_id=tenant.id, name=setup.site_name, domain=domain, allowed_origins=allowed_origins,
        webhook_secret_enc=encrypt_field(webhook_secret, tenant.id),
    )
    db.add(site)
    await db.flush()
    keys: dict[str, str] = {}
    for kind in ("publishable", "secret"):
        plain, prefix, digest = generate_api_key(kind)
        db.add(ApiKey(tenant_id=tenant.id, site_id=site.id, kind=kind, prefix=prefix, key_hash=digest))
        keys[kind] = plain
    return TenantCreated(
        tenant_id=tenant.id, site_id=site.id, publishable_key=keys["publishable"], secret_key=keys["secret"], webhook_secret=webhook_secret,
    )
