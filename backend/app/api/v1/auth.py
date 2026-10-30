"""Onboarding, API-key management, JWT exchange and customer OTP login."""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.api.deps import AuditAdmin, DbDep, Principal, get_tenant, require_full_customer, require_platform, require_role, resolve_tenant
from app.core.config import get_settings
from app.core import ratelimit
from app.core.exceptions import Conflict, Forbidden, NotFound, PlanLimitReached, TooManyRequests, Unauthorized
from app.core.phone import normalise_email
from app.core.redis import get_redis
from app.services import notifications, onboarding, phones
from app.services import plans as plans_service
from app.services import sites as sites_service
from app.services import users as users_service
from app.core.security import generate_api_key, generate_magic_token, generate_otp, hash_api_key, issue_jwt
from app.models import ApiKey, Booking, Customer, Site
from app.schemas.tenant import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    EmailChange,
    EmailConfirm,
    MagicLinkRequest,
    MagicLinkVerify,
    OtpRequest,
    OtpVerify,
    RecoveredKeys,
    RecoverKeysIn,
    TenantCreate,
    TenantCreated,
    TokenRequest,
    TokenResponse,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])
otp_router = APIRouter(prefix="/public/auth", tags=["auth"], dependencies=[Depends(resolve_tenant)])
_owner = require_role("owner")


@router.post("/tenants", response_model=TenantCreated, status_code=201, dependencies=[Depends(require_platform)])
async def create_tenant(body: TenantCreate, db: DbDep) -> TenantCreated:
    """Platform-operator onboarding: tenant + first site + initial key pair (shown once). ``plan`` is ``paid`` unless the request says another. ``owner_email`` (optional)
    is the address the owner signs in with (``POST /v1/owner/login``); 409 if another business has it."""
    try:
        return await onboarding.create_business(db, body, plan=body.plan, owner_email=normalise_email(str(body.owner_email)) if body.owner_email else None)
    except IntegrityError as exc:
        raise Conflict("Another business already has that owner email address") from exc


@router.post("/token", response_model=TokenResponse, dependencies=[Depends(resolve_tenant)])
async def exchange_token(body: TokenRequest, request: Request, db: DbDep) -> TokenResponse:
    """Server-to-server: a WordPress site exchanges its *secret* key for a short-lived admin JWT.

    The user is recorded (see ``users``); one an owner has disabled gets no token (403).
    """
    if request.state.key_kind != "secret":
        raise Forbidden("Token exchange requires a secret API key (X-API-Key)")
    tenant = get_tenant(request)
    await users_service.record_login(db, tenant.id, body.user_id, body.role, body.resource_id, body.display_name)
    site = getattr(request.state, "site", None)
    await sites_service.note_site_url(db, site.id if site is not None else None, body.site_url)  # a signal for the operator: never a reason to refuse
    token = issue_jwt(tenant_id=tenant.id, subject=body.user_id, role=body.role, resource_id=body.resource_id)
    return TokenResponse(access_token=token, expires_in=get_settings().jwt_ttl_seconds)


@router.post("/api-keys", response_model=ApiKeyCreated, status_code=201, dependencies=[Depends(_owner), AuditAdmin])  # authenticate first: the audit entry needs the tenant
async def create_api_key(body: ApiKeyCreate, request: Request, principal: Annotated[Principal, Depends(_owner)], db: DbDep) -> ApiKeyCreated:
    """Owner-only. The plaintext key is returned once; only its SHA-256 is stored. ``site_id`` must be one of the business's own sites; left out it is the business's site
    (a key always belongs to a site, which is what ties it to the site's allowed origins). A plan can limit the live keys of each kind (402 at the limit: revoke one first)."""
    # (``sites`` and ``api_keys`` are read before a business is known, so Row-Level Security does not cover them: every query on them names the business)
    tenant = get_tenant(request)
    site_id = body.site_id
    if site_id is None:
        own = await sites_service.first_site(db, tenant)
        if own is None:
            raise Conflict("This business has no site to attach keys to")
        site_id = own.id
    elif await db.scalar(select(Site.id).where(Site.id == site_id, Site.tenant_id == principal.tenant_id)) is None:
        raise NotFound("Site not found")
    await plans_service.ensure_can_issue_key(db, tenant, body.kind)
    plain, prefix, digest = generate_api_key(body.kind)
    key = ApiKey(tenant_id=principal.tenant_id, site_id=site_id, kind=body.kind, prefix=prefix, key_hash=digest)
    db.add(key)
    await db.flush()
    return ApiKeyCreated(id=key.id, kind=key.kind, key=plain)


@router.post("/api-keys/recover", response_model=RecoveredKeys, status_code=201, dependencies=[Depends(_owner), AuditAdmin])
async def recover_api_keys(body: RecoverKeysIn, request: Request, principal: Annotated[Principal, Depends(_owner)], db: DbDep) -> RecoveredKeys:
    """Owner. Lost the keys? Sign in by email (``POST /owner/login``) and ask for a new pair here: a publishable and a secret key for the business's site, **shown this once**
    (put the secret key in the WordPress plugin, the publishable key in the web page). ``revoke_existing`` switches every other key off once the new ones exist, which is what
    to do if a key may have fallen into the wrong hands; otherwise the old ones keep working until revoked (``DELETE /auth/api-keys/{id}``) - except that a plan which limits the live
    keys of each kind (the free plan: two, one in use and a spare) keeps its limit: a recovery always works, and drops the oldest key of a kind that would be over it."""
    tenant = get_tenant(request)
    site = await sites_service.first_site(db, tenant)
    if site is None:
        raise Conflict("This business has no site to attach keys to")
    made: dict[str, str] = {}
    ids: list[uuid.UUID] = []
    for kind in ("publishable", "secret"):
        plain, prefix, digest = generate_api_key(kind)
        key = ApiKey(tenant_id=principal.tenant_id, site_id=site.id, kind=kind, prefix=prefix, key_hash=digest)
        db.add(key)
        await db.flush()
        made[kind] = plain
        ids.append(key.id)
    revoked = 0
    if body.revoke_existing:
        result = await db.execute(
            update(ApiKey).where(ApiKey.tenant_id == principal.tenant_id, ApiKey.revoked_at.is_(None), ApiKey.id.not_in(ids)).values(revoked_at=dt.datetime.now(dt.UTC))
        )
        revoked = int(result.rowcount or 0)  # type: ignore[attr-defined]
    else:
        revoked = await sites_service.trim_keys(db, tenant, ids)
    return RecoveredKeys(site_id=site.id, publishable_key=made["publishable"], secret_key=made["secret"], revoked=revoked)


@router.get("/api-keys", response_model=list[ApiKeyOut], dependencies=[Depends(_owner)])
async def list_api_keys(principal: Annotated[Principal, Depends(_owner)], db: DbDep) -> list[ApiKey]:
    """Owner-only. The business's keys, revoked ones included: which key is which (its first characters), where it is used, since when. Never the key."""
    q = select(ApiKey).where(ApiKey.tenant_id == principal.tenant_id).order_by(ApiKey.created_at, ApiKey.id)
    return list((await db.scalars(q)).all())


@router.delete("/api-keys/{key_id}", response_model=ApiKeyOut, dependencies=[Depends(_owner), AuditAdmin])
async def revoke_api_key(key_id: uuid.UUID, principal: Annotated[Principal, Depends(_owner)], db: DbDep) -> ApiKey:
    """Owner-only. Switch a key off at once (a leaked one, or the old one after a rotation: create the new key first). Revoking twice does nothing more.

    The business's last working secret key cannot be revoked: with it gone nobody could sign in as an administrator to make another.
    """
    key = await db.scalar(select(ApiKey).where(ApiKey.id == key_id, ApiKey.tenant_id == principal.tenant_id))
    if key is None:
        raise NotFound("API key not found")
    if key.revoked_at is None:
        if key.kind == "secret":  # (locked, so two owners revoking the last two secret keys at once cannot both succeed)
            live = (await db.scalars(select(ApiKey.id).where(
                ApiKey.tenant_id == principal.tenant_id, ApiKey.kind == "secret", ApiKey.revoked_at.is_(None)).with_for_update())).all()
            if len(live) <= 1:
                raise Conflict("This is the business's only secret key; create another one before revoking it")
        key.revoked_at = dt.datetime.now(dt.UTC)
        await db.flush()
    return key


# ----------------------------------------------------------------------------- customer OTP
def _otp_key(tenant_id: object, phone: str) -> str:
    return f"otp:{tenant_id}:{hash_api_key(phone)[:24]}"


@otp_router.post("/otp", status_code=202)
async def request_otp(body: OtpRequest, request: Request, db: DbDep) -> dict[str, str]:
    """Send a 6-digit code over ``OTP_CHANNEL`` (WhatsApp or SMS; written to the log in development). 402 when the plan's message allowance is used up, 503 when it could not be sent.

    422 when the number is new to the business and is not a mobile of the chosen country (``app.services.phones``)."""
    tenant = get_tenant(request)
    phone = await phones.customer_phone(db, tenant, body.phone, body.phone_country)
    r = get_redis()
    key = _otp_key(tenant.id, phone)
    # One code per cooldown window. Without this, asking for a fresh code (which resets the attempt
    # counter) would let an attacker make unlimited guesses and spam the owner of the phone.
    cooldown = get_settings().otp_resend_cooldown_seconds
    if cooldown > 0 and not await r.set(key + ":cooldown", "1", ex=cooldown, nx=True):
        raise TooManyRequests("A code was sent recently; please wait before requesting another")
    if not await ratelimit.daily_cap(key + ":day", get_settings().sign_in_requests_per_day):
        raise TooManyRequests("Too many codes were requested for this number today; try again tomorrow")
    code = generate_otp()
    await r.set(key, hash_api_key(code), ex=get_settings().otp_ttl_seconds)
    await r.delete(key + ":tries")
    try:
        await notifications.send_login_code(tenant, phone, code)
    except (notifications.DeliveryFailed, PlanLimitReached):
        await r.delete(key, key + ":cooldown")  # nothing was sent: the code is worthless and the customer may ask again at once
        await ratelimit.refund_daily(key + ":day")
        raise
    return {"status": "sent"}


@otp_router.post("/otp/verify", response_model=TokenResponse)
async def verify_otp(body: OtpVerify, request: Request, db: DbDep) -> TokenResponse:
    tenant = get_tenant(request)
    phone = await phones.customer_phone(db, tenant, body.phone, body.phone_country)
    r = get_redis()
    key = _otp_key(tenant.id, phone)
    if await r.incr(key + ":tries") > 5:
        raise Forbidden("Too many attempts; request a new code")
    await r.expire(key + ":tries", get_settings().otp_ttl_seconds)
    stored = await r.get(key)
    if not stored or not hmac.compare_digest(str(stored), hash_api_key(body.code)):
        raise Unauthorized("Invalid or expired code")
    await r.delete(key, key + ":tries")

    stmt = pg_insert(Customer).values(
        tenant_id=tenant.id, name=body.name or phone, phone=phone, email=body.email
    )
    # Only overwrite the stored name when the caller supplied one.
    update_set = {"name": stmt.excluded.name} if body.name else {"phone": stmt.excluded.phone}
    customer_id = (await db.execute(
        stmt.on_conflict_do_update(constraint="uq_customers_tenant_phone", set_=update_set).returning(Customer.id)
    )).scalar_one()
    token = issue_jwt(tenant_id=tenant.id, subject=str(customer_id), role="customer")
    return TokenResponse(access_token=token, expires_in=get_settings().customer_jwt_ttl_seconds)


# ----------------------------------------------------------------------------- customer magic link (email)
def _magic_key(tenant_id: object, *parts: str) -> str:
    return ":".join(["magic", str(tenant_id), *parts])


@otp_router.post("/magic-link", status_code=202)
async def request_magic_link(body: MagicLinkRequest, request: Request, db: DbDep) -> dict[str, str]:
    """Email a single-use sign-in link to the address a customer booked with.

    What the link gives depends on whether anyone has *confirmed* the address (``POST /public/auth/email``, done by someone signed in with the phone):

    * a confirmed address signs in as that customer, in full;
    * an unconfirmed one - what was typed into a booking, which anyone can do with anyone's phone number - signs in to a session limited to the bookings made with that
      address: it can list, cancel and move those and review them, and nothing else (no saved addresses or vehicles, profile or data requests).

    The answer is the same whether or not the address belongs to a customer, so this cannot be used to find out who is
    registered. The address must belong to exactly one customer; several customers sharing one address are told apart by
    the ``phone`` they booked with (which must match too). Only the newest link works, and it works once.
    """
    tenant = get_tenant(request)
    settings = get_settings()
    email = normalise_email(body.email)
    phone = phones.spelled(body.phone, phones.region(tenant, body.phone_country)) if body.phone else None
    r = get_redis()
    who = _magic_key(tenant.id, hash_api_key(email)[:24])
    if settings.otp_resend_cooldown_seconds > 0 and not await r.set(who + ":cooldown", "1", ex=settings.otp_resend_cooldown_seconds, nx=True):
        raise TooManyRequests("A link was sent recently; please wait before requesting another")
    if not await ratelimit.daily_cap(who + ":day", settings.sign_in_requests_per_day):
        raise TooManyRequests("Too many links were requested for this address today; try again tomorrow")
    confirmed = select(Customer.id).where(func.lower(Customer.email) == email, Customer.email_verified_at.is_not(None), Customer.erased_at.is_(None))
    if phone is not None:
        confirmed = confirmed.where(Customer.phone == phone)
    matches = list((await db.scalars(confirmed.limit(2))).all())
    limited_to: str | None = None
    if not matches:  # nobody confirmed this address: it only opens the bookings made with it
        made_with = (
            select(Booking.customer_id).join(Customer, Customer.id == Booking.customer_id)
            .where(func.lower(Booking.contact_email) == email, Customer.erased_at.is_(None))
        )
        if phone is not None:
            made_with = made_with.where(Customer.phone == phone)
        matches = list((await db.scalars(made_with.distinct().limit(2))).all())
        limited_to = email
    if len(matches) != 1:
        if matches:
            log.info("magic link not sent: several customers share one email (tenant %s); a phone is needed", tenant.id)
        return {"status": "sent"}
    token = generate_magic_token()
    token_key = _magic_key(tenant.id, "t", hash_api_key(token))
    previous = await r.getset(who, token_key)  # one live link per address: the older one stops working
    if previous:
        await r.delete(str(previous))
    await r.set(token_key, f"{matches[0]}|{limited_to}" if limited_to else str(matches[0]), ex=settings.magic_link_ttl_seconds)  # (what the link opens: all, or one address's bookings)
    await r.expire(who, settings.magic_link_ttl_seconds)
    try:
        await notifications.send_magic_link(tenant, matches[0], email, token)
    except notifications.DeliveryFailed:
        # the answer must not differ from the "unknown address" one, so a failed send is only logged (it is in the notification log too)
        log.error("magic link could not be delivered for tenant %s", tenant.id)
        await r.delete(token_key, who)
        await ratelimit.refund_daily(who + ":day")
    return {"status": "sent"}


@otp_router.post("/magic-link/verify", response_model=TokenResponse)
async def verify_magic_link(body: MagicLinkVerify, request: Request, db: DbDep) -> TokenResponse:
    """Exchange the token from the emailed link for a customer token. A POST on purpose: mail scanners that merely
    *open* links would use up a single-use GET link before its owner did."""
    tenant = get_tenant(request)
    r = get_redis()
    opened = await r.getdel(_magic_key(tenant.id, "t", hash_api_key(body.token)))  # atomic: two uses race, one wins
    if not opened:
        raise Unauthorized("Invalid or expired link")
    customer_id, _, limited_to = str(opened).partition("|")  # "<customer>" or "<customer>|<the one address its bookings were made with>"
    exists = await db.scalar(select(Customer.id).where(Customer.id == uuid.UUID(customer_id), Customer.erased_at.is_(None)))
    if exists is None:
        raise Unauthorized("Invalid or expired link")
    token = issue_jwt(tenant_id=tenant.id, subject=str(exists), role="customer", email=limited_to or None)
    return TokenResponse(access_token=token, expires_in=get_settings().customer_jwt_ttl_seconds)


# ----------------------------------------------------------------------------- confirming a customer's email
def _confirm_key(tenant_id: object, *parts: str) -> str:
    return ":".join(["emailconfirm", str(tenant_id), *parts])


@otp_router.post("/email", status_code=202)
async def request_email_confirmation(
    body: EmailChange, request: Request, db: DbDep, principal: Annotated[Principal, Depends(require_full_customer)]
) -> dict[str, str]:
    """Make an email address the signed-in customer's own: a link is sent to it, and following it (``POST /public/auth/email/confirm``) saves it on the customer and confirms it.

    Needs a full sign-in (phone code, or an address already confirmed), because it is the phone's owner who may say which email is theirs; a session that
    only an unconfirmed email link opened is refused. A confirmed address then signs in as the customer in full. Replaces whatever email the record held (the way to
    remove one somebody else typed in). Limited like sign-in codes; 503 when the email could not be sent.
    """
    tenant = get_tenant(request)
    settings = get_settings()
    email = normalise_email(str(body.email))
    customer = await db.get(Customer, uuid.UUID(principal.user_id))
    if customer is None or customer.erased_at is not None:
        raise NotFound("Customer not found")
    if customer.email_verified_at is not None and (customer.email or "").lower() == email:
        raise Conflict("This email address is already confirmed")
    r = get_redis()
    who = _confirm_key(tenant.id, "c", str(customer.id))
    if settings.otp_resend_cooldown_seconds > 0 and not await r.set(who + ":cooldown", "1", ex=settings.otp_resend_cooldown_seconds, nx=True):
        raise TooManyRequests("A link was sent recently; please wait before requesting another")
    if not await ratelimit.daily_cap(who + ":day", settings.sign_in_requests_per_day):
        raise TooManyRequests("Too many links were requested today; try again tomorrow")
    token = generate_magic_token()
    token_key = _confirm_key(tenant.id, "t", hash_api_key(token))
    previous = await r.getset(who, token_key)  # one live link per customer: the older one stops working
    if previous:
        await r.delete(str(previous))
    await r.set(token_key, f"{customer.id}|{email}", ex=settings.magic_link_ttl_seconds)
    await r.expire(who, settings.magic_link_ttl_seconds)
    try:
        await notifications.send_email_confirmation(tenant, customer.id, email, token)
    except notifications.DeliveryFailed:
        await r.delete(token_key, who, who + ":cooldown")  # nothing was sent: the link is worthless and they may ask again at once
        await ratelimit.refund_daily(who + ":day")
        raise
    return {"status": "sent"}


@otp_router.post("/email/confirm")
async def confirm_email(body: EmailConfirm, request: Request, db: DbDep) -> dict[str, str]:
    """Follow the emailed confirmation link: the address becomes the customer's email, confirmed. A POST for the same reason as the sign-in link (mail scanners open links)."""
    tenant = get_tenant(request)
    opened = await get_redis().getdel(_confirm_key(tenant.id, "t", hash_api_key(body.token)))  # atomic: two uses race, one wins
    if not opened:
        raise Unauthorized("Invalid or expired link")
    customer_id, _, email = str(opened).partition("|")
    customer = await db.get(Customer, uuid.UUID(customer_id), with_for_update=True, populate_existing=True)
    if customer is None or customer.erased_at is not None or not email:
        raise Unauthorized("Invalid or expired link")
    customer.email, customer.email_verified_at = email, dt.datetime.now(dt.UTC)
    await db.flush()
    return {"status": "confirmed"}
