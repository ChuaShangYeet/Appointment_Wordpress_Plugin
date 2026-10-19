"""The owner signs in with their email address: no password, no WordPress needed (blueprint §16, "the owner's account in the backend").

A business that signed itself up has an ``owner_email`` that was proved by a confirmation link before the business existed (``app.services.signup``). Asking to sign in mails a
link to that address; using it once gives an **owner** admin token - the same token the WordPress server gets from the secret key, with the same powers and the same short life.
With it the owner can do everything an owner does, including what a lost key needs: see which keys exist, make new ones (``POST /auth/api-keys/recover``) and revoke the old.

* The answer to "send me a link" is the same whether or not the address belongs to a business, so it cannot be used to find out who is registered.
* One live link per address (a newer one cancels the older), used once, valid ``OWNER_LOGIN_TTL_SECONDS`` (15 minutes); only a hash of it is kept in Redis.
* A pause between requests per address, a daily cap per address, and a limit per client address, like every message that costs the service something.
* A business without an ``owner_email`` (set up by the operator) cannot sign in this way until the operator sets one (``PUT /platform/tenants/{id}/owner-email``).
* The link is a POST target, not a GET, so that mail scanners which merely open links cannot use it up before its owner does.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import tenant_session
from app.core.exceptions import TooManyRequests, Unauthorized
from app.core.phone import normalise_email
from app.core.ratelimit import daily_cap, refund_daily
from app.core.redis import get_redis
from app.core.security import generate_magic_token, hash_api_key, issue_jwt
from app.models import Tenant
from app.schemas.tenant import OwnerSession
from app.services import users as users_service
from app.services.delivery import Message, deliver
from app.services.notifications import clean

log = logging.getLogger(__name__)


def _who(email: str) -> str:
    return f"owner:email:{hash_api_key(email)[:24]}"


def _token_key(token: str) -> str:
    return f"owner:token:{hash_api_key(token)}"


def _link(token: str) -> str:
    url = get_settings().owner_login_url
    return url.replace("{token}", token) if url else f"Your sign-in token: {token}"


async def start(raw_email: str) -> None:
    """Mail a sign-in link to ``raw_email`` if it is a business's owner address. Returns nothing: the answer is the same either way (but a request too soon, or
    too many in a day, is refused for every address alike)."""
    settings = get_settings()
    email = normalise_email(raw_email)
    r = get_redis()
    who = _who(email)
    cooldown = settings.owner_login_cooldown_seconds
    if cooldown > 0 and not await r.set(who + ":cooldown", "1", ex=cooldown, nx=True):
        raise TooManyRequests("A sign-in link was sent recently; please wait before asking for another")
    if not await daily_cap(who + ":day", settings.sign_in_requests_per_day):
        raise TooManyRequests("Too many sign-in links were asked for this address today; try again tomorrow")
    async with tenant_session(None) as db:
        found = (await db.execute(select(Tenant.id, Tenant.name).where(Tenant.owner_email == email, Tenant.status == "active"))).first()
    if found is None:
        return
    token = generate_magic_token()
    key = _token_key(token)
    previous = await r.getset(who, key)  # one live link per address: the older one stops working
    if previous:
        await r.delete(str(previous))
    await r.set(key, str(found.id), ex=settings.owner_login_ttl_seconds)
    await r.expire(who, settings.owner_login_ttl_seconds)
    text = (
        f"Hello,\n\nUse this link to sign in to the admin of {clean(found.name)}:\n\n{_link(token)}\n\n"
        f"The link works once and expires in {max(1, settings.owner_login_ttl_seconds // 60)} minutes. If you did not ask for it, ignore this email: nobody can sign in without it."
    )
    try:
        await deliver(Message(channel="email", recipient=email, body=text, subject=f"Your sign-in link for {clean(settings.platform_name)}"))
    except Exception as exc:  # noqa: BLE001 - the answer must not differ from the "unknown address" one, so a failed send is only logged
        log.error("owner sign-in link could not be delivered: %s", type(exc).__name__)
        await r.delete(key, who, who + ":cooldown")
        await refund_daily(who + ":day")


async def verify(token: str) -> OwnerSession:
    """Use the token from the emailed link: an owner admin token for the business."""
    r = get_redis()
    tenant_id = await r.getdel(_token_key(token))  # atomic: two uses race, one wins
    if not tenant_id:
        raise Unauthorized("Invalid or expired sign-in link")
    async with tenant_session(None) as db:
        tenant = await db.get(Tenant, uuid.UUID(str(tenant_id)))
    if tenant is None or tenant.status != "active" or not tenant.owner_email:
        raise Unauthorized("Invalid or expired sign-in link")
    user_id = f"owner:{tenant.owner_email}"
    async with tenant_session(tenant.id) as db:
        await users_service.record_login(db, tenant.id, user_id, "owner", None, None)  # (refuses an owner the business has disabled)
    settings = get_settings()
    access = issue_jwt(tenant_id=tenant.id, subject=user_id, role="owner")
    return OwnerSession(access_token=access, expires_in=settings.jwt_ttl_seconds, tenant_id=tenant.id)
