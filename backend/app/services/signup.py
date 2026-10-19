"""Self-service sign-up: a business sets itself up on the free plan (``POST /v1/signup``, then ``POST /v1/signup/verify``).

Nothing is created until the person has proved the email address is theirs:

1. ``POST /signup`` checks everything (country, time zone, address, the terms version) and keeps the request **in Redis for ``SIGNUP_TOKEN_TTL_SECONDS``**
   (a day), mails a link with a one-time token, and answers ``202`` whatever the address is. An address that already has a business gets the same answer and no mail,
   so the form cannot be used to find out who is a customer. One pending request per address (a newer one replaces the older), and a cooldown between requests.
2. ``POST /signup/verify`` (a POST on purpose: mail scanners that merely open links would otherwise use the token up) takes the token once and creates the business,
   its first site and its keys on the **free** plan, recording the owner's email and the terms version accepted. The keys are shown that once.

Off unless ``SELF_SERVICE_SIGNUP`` is set. Abuse is limited by the per-address rate limits (``signup`` and ``signup_verify`` buckets), the email proof, and one
business per email address, throw-away mailboxes refused (``app.core.disposable_emails``), and the Cloudflare Turnstile bot check (``app.services.captcha``) when ``CAPTCHA_PROVIDER`` is set: switch it on before opening sign-up to the internet.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.disposable_emails import is_disposable
from app.core.exceptions import Conflict, DisposableEmail, SignupDisabled, TooManyRequests, Unauthorized, ValidationFailed
from app.core.phone import normalise_email
from app.core.ratelimit import daily_cap, refund_daily
from app.core.redis import get_redis
from app.core.security import generate_magic_token, hash_api_key
from app.models import Tenant
from app.schemas.tenant import SignupRequest, TenantCreated
from app.services import onboarding
from app.services.delivery import Message, deliver
from app.services.notifications import DeliveryFailed, clean

log = logging.getLogger(__name__)

#: The plan a business that signs itself up starts on.
PLAN = "free"


def ensure_enabled() -> None:
    if not get_settings().self_service_signup:
        raise SignupDisabled("Self-service sign-up is not open on this server")


def _pending(email: str) -> str:
    return f"signup:email:{hash_api_key(email)[:24]}"


def _token_key(token: str) -> str:
    return f"signup:token:{hash_api_key(token)}"


def _link(token: str) -> str:
    url = get_settings().signup_verify_url
    return url.replace("{token}", token) if url else f"Your confirmation token: {token}"


async def start(body: SignupRequest) -> None:
    """Check a sign-up request, keep it for the confirmation and mail the link. Returns nothing: the answer must not depend on whether the address is known."""
    ensure_enabled()
    settings = get_settings()
    if body.accepted_terms_version != settings.terms_version:
        raise ValidationFailed(f"The current terms are version {settings.terms_version!r}; show them and send that version as accepted_terms_version")
    email = normalise_email(str(body.owner_email))
    if is_disposable(email, extra_blocked=settings.blocked_email_domains, allowed=settings.allowed_email_domains):  # (a fact about the domain, so it says nothing about who is registered)
        raise DisposableEmail("Please use an email address you keep: addresses from temporary-mail services are not accepted")
    r = get_redis()
    who = _pending(email)
    cooldown = settings.signup_resend_cooldown_seconds
    if cooldown > 0 and not await r.set(who + ":cooldown", "1", ex=cooldown, nx=True):
        raise TooManyRequests("A confirmation was sent recently; please wait before asking again")
    if not await daily_cap(who + ":day", settings.signup_requests_per_address_per_day):  # (before the lookup: a known address is told the same)
        raise TooManyRequests("Too many confirmations were asked for this address today; try again tomorrow")
    async with tenant_session(None) as db:
        known = await db.scalar(select(Tenant.id).where(Tenant.owner_email == email))
    if known is not None:
        log.info("sign-up for an address that already has a business: nothing sent")
        return
    token = generate_magic_token()
    key = _token_key(token)
    previous = await r.getset(who, key)  # one pending request per address: the older link stops working
    if previous:
        await r.delete(str(previous))
    await r.set(key, body.model_dump_json(), ex=settings.signup_token_ttl_seconds)
    await r.expire(who, settings.signup_token_ttl_seconds)
    name = clean(settings.platform_name)
    text = (
        f"Hello,\n\nConfirm your email address to set up {clean(body.name)} on {name}:\n\n{_link(token)}\n\n"
        f"The link works once and expires in {max(1, settings.signup_token_ttl_seconds // 3600)} hours. If you did not ask for it, ignore this email: nothing was created."
    )
    try:
        await deliver(Message(channel="email", recipient=email, body=text, subject=f"Confirm your email for {name}"))
    except Exception as exc:  # noqa: BLE001 - whatever went wrong, the person has no link: say so and let them try again at once
        log.error("sign-up confirmation could not be delivered: %s", type(exc).__name__)
        await r.delete(key, who, who + ":cooldown")
        await refund_daily(who + ":day")
        raise DeliveryFailed("We could not send the confirmation email; please try again shortly") from exc


async def confirm(db: AsyncSession, token: str) -> TenantCreated:
    """Create the business the confirmed request describes (once: the token is used up whatever happens next)."""
    ensure_enabled()
    r = get_redis()
    raw = await r.getdel(_token_key(token))  # atomic: two uses race, one wins
    if not raw:
        raise Unauthorized("Invalid or expired confirmation link")
    request = SignupRequest.model_validate_json(raw)
    email = normalise_email(str(request.owner_email))
    await r.delete(_pending(email))
    terms = get_settings().terms_version
    if request.accepted_terms_version != terms:
        raise ValidationFailed(f"The terms changed to version {terms!r} while you were confirming; please sign up again and accept them")
    try:
        created = await onboarding.create_business(db, request, plan=PLAN, owner_email=email, terms_version=terms)
    except IntegrityError as exc:  # the unique index on ``owner_email``: the address got a business since it was asked for (or two confirmations race)
        raise Conflict("A business already exists for this email address") from exc
    log.info("business %s signed itself up on the %s plan", created.tenant_id, PLAN)
    return created
