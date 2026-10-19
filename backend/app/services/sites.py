"""A business's website: its address, the browser origins derived from it, the keys that belong to it, and the address the plugin says it runs on.

The free plan is *one account, one business, one website* (``app.services.plans``: ``max_sites``, ``max_domains``, ``max_live_keys_per_kind``, ``domain_change_days``).
On a plan that sets ``max_domains`` the site's ``allowed_origins`` are **derived** from its website address (``shop.example`` -> ``https://shop.example`` and
``https://www.shop.example``) and nothing else can be added; on a plan that does not (paid, or set up by the operator) the owner keeps a free-form, validated list, and an empty
list lets any origin use the publishable key. Changing the address is a *change* only when the set of domains differs (the www spelling, case and a trailing slash are no-ops);
the address given at sign-up is not a change, and on a plan with ``domain_change_days`` two changes must be that many days apart.

This is honest about what it can do: the ``Origin`` header only exists in browsers, so a server that calls the API itself is not held by any of this. It is a product rule that
keeps one account to one website, not a wall (the limits that cost money are shared by the whole business however many websites it has).
"""
from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import origins
from app.core.exceptions import ValidationFailed
from app.models import ApiKey, Plan, Site, Tenant
from app.services import plans

log = logging.getLogger(__name__)


class _Keep:
    """"Leave the address as it is" (``None`` means "no address")."""


KEEP = _Keep()


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def first_site(db: AsyncSession, tenant: Tenant) -> Site | None:
    """The business's site (the first one: no endpoint adds a second yet)."""
    return await db.scalar(select(Site).where(Site.tenant_id == tenant.id).order_by(Site.created_at, Site.id).limit(1))


def hosts_of(site: Site) -> frozenset[str]:
    return origins.hosts_of(site.domain, site.allowed_origins)


def site_for_new_business(plan: Plan, domain: str | None, allowed_origins: list[str]) -> tuple[str | None, list[str]]:
    """``(domain, allowed_origins)`` to store for the first site of a new business, or :class:`ValidationFailed`.

    A plan that limits domains needs the website address and derives the origins from it (a list of origins is refused); any other plan keeps what was sent, checked.
    """
    try:
        if plan.max_domains is None:
            return (origins.host_of(domain, public=False) if domain else None), [origins.origin_of(o) for o in allowed_origins]
        if allowed_origins:
            raise ValidationFailed("On this plan the allowed origins follow the website address: send the website address (domain) and no list of origins")
        if not domain:
            raise ValidationFailed("A website address (domain) is needed on this plan")
        host = origins.host_of(domain)
        return host, origins.derive_origins([host])
    except origins.WebsiteProblem as exc:
        raise ValidationFailed(str(exc)) from exc


async def update_site(
    db: AsyncSession, tenant: Tenant, site: Site, *, domain: str | None | _Keep = KEEP, allowed_origins: list[str] | None = None, now: dt.datetime | None = None
) -> Site:
    """Change a site's website address and/or origins under the plan's rules (402 ``plan_limit_reached`` when they say no; nothing changes then).

    ``domain`` left out keeps the address; ``allowed_origins`` left out keeps the origins (on a plan that derives them, the extra domains it already has).
    """
    now = now or _utcnow()
    site = await db.get(Site, site.id, with_for_update=True, populate_existing=True) or site  # (two changes at once run one after the other)
    plan = await plans.plan_of(db, tenant)
    try:
        derived = plans.limit_value(plan, tenant, "domains") is not None
        wanted = site.domain if isinstance(domain, _Keep) else (origins.host_of(domain, public=derived) if domain else None)
        given = None if allowed_origins is None else [origins.origin_of(o) for o in allowed_origins]
    except origins.WebsiteProblem as exc:
        raise ValidationFailed(str(exc)) from exc
    if not derived:  # free-form: what is sent is what is kept
        if not isinstance(domain, _Keep):
            site.domain = wanted
        if given is not None:
            site.allowed_origins = given
        await db.flush()
        return site
    return await _update_derived(db, tenant, plan, site, wanted, given, now)


async def _update_derived(db: AsyncSession, tenant: Tenant, plan: Plan, site: Site, wanted: str | None, given: list[str] | None, now: dt.datetime) -> Site:
    cap = plans.limit_value(plan, tenant, "domains")
    assert cap is not None
    current = hosts_of(site)
    primary = origins.host_key(wanted) if wanted else None
    extras = origins.hosts_of(None, given) if given is not None else current - ({origins.host_key(site.domain)} if site.domain else set())
    hosts = frozenset(extras | ({primary} if primary else set()))
    if not hosts:
        raise ValidationFailed("A website address (domain) is needed on this plan")
    plans.ensure_domain_count(plan, tenant, len(hosts))
    if hosts != current and current:  # a change; the first setting of an address (none before) is not
        due = plans.domain_change_due(plan, tenant, site)
        if due is not None and now < due:
            raise plans.cooldown_refusal(plan, tenant, due)
        site.domain_changed_at = now
    site.domain = primary or sorted(hosts)[0]
    site.allowed_origins = origins.derive_origins(hosts)
    await db.flush()
    return site


def next_domain_change(plan: Plan, tenant: Tenant, site: Site, now: dt.datetime | None = None) -> dt.datetime | None:
    """When the address may next be changed, or ``None`` if it may be now."""
    due = plans.domain_change_due(plan, tenant, site)
    return due if due is not None and due > (now or _utcnow()) else None


async def trim_keys(db: AsyncSession, tenant: Tenant, keep_ids: list[uuid.UUID], now: dt.datetime | None = None) -> int:
    """Bring each kind of key down to the plan's limit by revoking the oldest live ones (never ``keep_ids``, the keys just made). Returns how many were revoked.

    For a recovery (``POST /auth/api-keys/recover``), which must always work: an owner who has lost their keys and is at the limit gets a new pair and loses the oldest.
    """
    plan = await plans.plan_of(db, tenant)
    cap = plans.limit_value(plan, tenant, "live_keys_per_kind")
    if cap is None:
        return 0
    revoked = 0
    for kind in ("publishable", "secret"):
        live = (await db.scalars(
            select(ApiKey.id).where(ApiKey.tenant_id == tenant.id, ApiKey.kind == kind, ApiKey.revoked_at.is_(None)).order_by(ApiKey.created_at, ApiKey.id).with_for_update()
        )).all()
        drop = [key_id for key_id in live[: max(0, len(live) - cap)] if key_id not in keep_ids]
        if drop:
            await db.execute(update(ApiKey).where(ApiKey.id.in_(drop)).values(revoked_at=now or _utcnow()))
            revoked += len(drop)
    return revoked


def site_url_matches(site: Site) -> bool | None:
    """Whether the address the plugin reported is one of the site's domains: ``None`` when there is nothing to compare, ``True`` for the developer's own machine."""
    host = origins.site_url_host(site.last_site_url)
    hosts = hosts_of(site)
    if host is None or not hosts:
        return None
    return host in hosts or host in origins.DEV_HOSTS


async def note_site_url(db: AsyncSession, site_id: uuid.UUID | None, value: str | None, now: dt.datetime | None = None) -> None:
    """Remember the address the plugin says it runs on (``POST /auth/token``, ``site_url``) when it changed; warn in the log if it is not the site's domain.

    A signal for the operator, never a reason to refuse: anything unreadable is ignored, and nothing here raises.
    """
    if site_id is None or not isinstance(value, str) or not value.strip():
        return
    text = value.strip()[:300]
    site = await db.get(Site, site_id)
    if site is None or site.last_site_url == text:
        return
    site.last_site_url, site.last_site_url_at = text, now or _utcnow()
    if site_url_matches(site) is False:
        log.warning("business %s: the plugin says it runs on %s, which is not the site's domain (%s)", site.tenant_id, origins.site_url_host(text), site.domain)


def signal(site: Site | None) -> dict[str, Any] | None:
    """What the operator sees about the site: its domain and origins, the plugin's reported address, and whether the two agree."""
    if site is None:
        return None
    return {
        "site_id": site.id, "domain": site.domain, "allowed_origins": list(site.allowed_origins), "domain_changed_at": site.domain_changed_at,
        "last_site_url": site.last_site_url, "last_site_url_at": site.last_site_url_at, "site_url_matches_domain": site_url_matches(site),
    }
