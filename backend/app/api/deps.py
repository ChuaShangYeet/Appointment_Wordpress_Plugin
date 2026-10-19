"""FastAPI dependencies: DB session (tenant-scoped), tenant resolution, admin JWT auth.

Ordering matters: routers declare their auth dependency (``resolve_tenant`` or
``require_role``) as a router-level dependency, which FastAPI runs *before* route parameters,
so ``get_db`` can read the resolved tenant from ``request.state``.
"""
from __future__ import annotations

import hmac
import ipaddress
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import origins as origin_rules
from app.core import ratelimit
from app.core.config import get_settings
from app.core.logs import bind_tenant
from app.core.database import tenant_session
from app.core.exceptions import Forbidden, Unauthorized
from app.core.security import ROLE_RANK, decode_jwt, hash_api_key
from app.models import ApiKey, Site, Tenant
from app.services import audit, captcha
from app.services import users as users_service


@dataclass(frozen=True)
class Principal:
    """Authenticated caller resolved from a bearer JWT."""

    tenant_id: uuid.UUID
    user_id: str
    role: str
    resource_id: uuid.UUID | None = None
    #: A customer whose emailed sign-in link was for an unconfirmed email: they see and manage only the bookings made with this address.
    email_scope: str | None = None


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a session in one transaction (commit on success, rollback on error).

    If a tenant was resolved earlier in the request, the transaction is bound to it with
    ``set_config('app.tenant_id', ..., true)`` (the parameterised form of ``SET LOCAL``) so
    Row-Level Security confines every query to that tenant.
    """
    tenant_id: uuid.UUID | None = getattr(request.state, "tenant_id", None)
    async with tenant_session(tenant_id) as session:
        yield session


#: ``scope="function"`` makes the transaction commit *before* the response is sent, so a
#: client that receives 201 can rely on the booking being durable.
DbDep = Annotated[AsyncSession, Depends(get_db, scope="function")]


_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


async def audit_admin_action(request: Request, db: DbDep) -> AsyncIterator[None]:
    """Record every successful state-changing admin request in the audit log, in the request's own transaction.

    A function-scoped dependency: its exit code runs after the endpoint returned and before the session commits. If the
    endpoint raised, the exception is thrown in at the ``yield`` and nothing is recorded, so a refused or rolled-back
    request leaves no entry. Only the route, the actor and the record concerned are stored - never the request body.
    """
    yield
    principal: Principal | None = getattr(request.state, "principal", None)
    if principal is None or request.method in _SAFE_METHODS:
        return
    # the path as called, with each id replaced by its name: "PATCH /v1/admin/resources/{resource_id}"
    path = request.url.path
    target_type: str | None = None
    target_id: str | None = None
    for name, value in request.path_params.items():
        path = path.replace(str(value), "{" + name + "}", 1)
        if target_id is None and name.endswith("_id"):  # the first ``*_id`` in the path is the record concerned
            target_type, target_id = name[:-3], str(value)
    audit.record(
        db, tenant_id=principal.tenant_id, action=f"{request.method} {path}",
        actor_id=principal.user_id, role=principal.role, target_type=target_type, target_id=target_id,
    )


#: Router-level dependency for administrative routes (see :func:`audit_admin_action`).
AuditAdmin = Depends(audit_admin_action, scope="function")


async def _load_tenant(tenant_id: uuid.UUID) -> Tenant:
    async with tenant_session(None) as s:
        tenant = await s.get(Tenant, tenant_id)
    if tenant is None or tenant.status != "active":
        raise Unauthorized("Tenant is not active")
    return tenant


async def resolve_tenant(
    request: Request,
    x_site_key: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> Tenant:
    """Resolve the tenant from ``X-Site-Key`` (publishable) or ``X-API-Key`` (secret).

    For publishable keys the browser ``Origin`` must be in the site's allowlist (when one is set).
    """
    raw = x_site_key or x_api_key
    if not raw:
        raise Unauthorized("Missing X-Site-Key or X-API-Key header")
    expected_kind = "publishable" if x_site_key else "secret"
    async with tenant_session(None) as s:
        row = (await s.execute(
            select(ApiKey, Site)
            .outerjoin(Site, Site.id == ApiKey.site_id)
            .where(ApiKey.key_hash == hash_api_key(raw), ApiKey.revoked_at.is_(None))
        )).first()
    if row is None or row[0].kind != expected_kind:
        raise Unauthorized("Invalid API key")
    key, site = row
    origin = request.headers.get("origin")
    if expected_kind == "publishable" and origin and site is not None and site.allowed_origins:
        if origin.lower() not in {o.lower() for o in site.allowed_origins} and not origin_rules.is_dev_origin(origin):  # (a page on the developer's own machine is always fine)
            raise Forbidden("Origin not allowed for this site key")
    tenant = await _load_tenant(key.tenant_id)
    request.state.tenant = tenant
    request.state.tenant_id = tenant.id
    bind_tenant(tenant.id)
    request.state.site = site
    request.state.key_kind = key.kind
    await ratelimit.enforce_tenant(request, tenant.id)
    return tenant


def require_role(minimum: str) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Dependency factory: bearer JWT with at least ``minimum`` role (staff < manager < owner)."""

    async def dep(request: Request, authorization: str | None = Header(default=None)) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise Unauthorized("Missing bearer token")
        claims = decode_jwt(authorization[7:])
        role = claims["role"]
        if role == "customer" or ROLE_RANK[role] < ROLE_RANK[minimum]:
            raise Forbidden(f"Requires role '{minimum}' or higher")
        tenant = await _load_tenant(uuid.UUID(claims["tid"]))
        request.state.tenant = tenant
        request.state.tenant_id = tenant.id
        bind_tenant(tenant.id)
        await ratelimit.enforce_tenant(request, tenant.id)
        async with tenant_session(tenant.id) as s:  # a user an owner disabled loses the tokens they already hold, immediately
            if await users_service.is_disabled(s, str(claims["sub"])):
                raise Forbidden("This user has been disabled")
        rid = claims.get("rid")
        principal = Principal(tenant.id, str(claims["sub"]), role, uuid.UUID(rid) if rid else None)
        request.state.principal = principal
        return principal

    return dep


async def require_customer(request: Request, authorization: str | None = Header(default=None)) -> Principal:
    """Bearer JWT issued by the OTP flow (role ``customer``); must match the publishable-key tenant."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise Unauthorized("Missing bearer token")
    claims = decode_jwt(authorization[7:])
    if claims["role"] != "customer":
        raise Forbidden("Customer token required")
    tenant_id = uuid.UUID(claims["tid"])
    if getattr(request.state, "tenant_id", tenant_id) != tenant_id:
        raise Forbidden("Token belongs to a different tenant")
    em = claims.get("em")
    if em is not None and (not isinstance(em, str) or not em):
        raise Unauthorized("Invalid or expired token")
    return Principal(tenant_id, str(claims["sub"]), "customer", email_scope=em)


async def require_full_customer(principal: Annotated[Principal, Depends(require_customer)]) -> Principal:
    """A customer who signed in as themselves (phone code, or an email they have confirmed), not through an email link for an unconfirmed address."""
    if principal.email_scope is not None:
        raise Forbidden("This sign-in shows only the bookings made with your email address; sign in with your phone number to use your saved details")
    return principal


async def require_platform(x_platform_token: Annotated[str | None, Header()] = None) -> None:
    """The platform operator's own credential (``X-Platform-Token``): for what no single business may do (set one up, change its plan)."""
    expected = get_settings().platform_admin_token.encode()
    if not x_platform_token or not hmac.compare_digest(x_platform_token.encode(), expected):
        raise Unauthorized("Invalid platform token")


def require_captcha(action: str) -> Callable[..., Coroutine[Any, Any, None]]:
    """Dependency factory: the bot check for ``action`` (``signup`` or ``booking``), made before anything else is done. A request made with a secret API key is exempt."""

    async def dep(request: Request, x_captcha_token: Annotated[str | None, Header()] = None) -> None:
        if getattr(request.state, "key_kind", None) == "secret":
            return
        ip = ratelimit.request_ip(request)
        try:
            ipaddress.ip_address(ip)
        except ValueError:  # (an IPv6 client is grouped by /64 for limiting, and "unknown" is no address: Turnstile's remoteip is optional)
            ip = None  # type: ignore[assignment]
        await captcha.check(x_captcha_token, action=action, remote_ip=ip)

    return dep


def get_tenant(request: Request) -> Tenant:
    """The tenant resolved by ``resolve_tenant`` / ``require_role`` earlier in this request."""
    tenant: Tenant | None = getattr(request.state, "tenant", None)
    if tenant is None:
        raise Unauthorized("Tenant not resolved")
    return tenant
