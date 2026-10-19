"""Self-service sign-up (no key: the business does not exist yet). See :mod:`app.services.signup`."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import DbDep, require_captcha
from app.core.config import get_settings
from app.schemas.tenant import SignupConfigOut, SignupRequest, SignupVerify, TenantCreated
from app.services import captcha, signup

router = APIRouter(prefix="/signup", tags=["signup"])


@router.get("/config", response_model=SignupConfigOut)
async def signup_config() -> SignupConfigOut:
    """Whether sign-up is open here and which version of the terms to show (and send back as ``accepted_terms_version``)."""
    settings = get_settings()
    return SignupConfigOut(
        enabled=settings.self_service_signup, terms_version=settings.terms_version, terms_url=settings.terms_url or None, captcha=captcha.public_config(settings),
    )


@router.post("", status_code=202, dependencies=[Depends(signup.ensure_enabled), Depends(require_captcha("signup"))])
async def request_signup(body: SignupRequest) -> dict[str, str]:
    """Ask to set a business up. Mails a confirmation link to ``owner_email``; nothing exists until it is used. Always ``202`` for a valid request, whether or not
    the address already has a business (no mail is sent then), so the form cannot be used to discover customers. 403 when sign-up is closed, 429 when asked again too soon.
    When a bot check is configured (``GET /captcha``) the widget's token goes in the ``X-Captcha-Token`` header: 403 ``captcha_required`` / ``captcha_failed``, 503 ``captcha_unavailable``."""
    await signup.start(body)
    return {"status": "sent"}


@router.post("/verify", response_model=TenantCreated, status_code=201)
async def verify_signup(body: SignupVerify, db: DbDep) -> TenantCreated:
    """Use the token from the emailed link: creates the business on the free plan and returns its keys, **shown this once**."""
    return await signup.confirm(db, body.token)
