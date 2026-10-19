"""What a page needs to show the bot check (no key: the sign-up page has no business yet)."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.config import get_settings
from app.schemas.tenant import CaptchaConfigOut
from app.services import captcha

router = APIRouter(prefix="/captcha", tags=["captcha"])


@router.get("", response_model=CaptchaConfigOut)
async def captcha_config() -> CaptchaConfigOut:
    """Whether a bot check is required, by which provider, and the (public) site key to build the widget with. The widget must be made with
    ``action`` set to ``signup`` or ``booking``, and the token it produces sent in the ``X-Captcha-Token`` header of ``POST /signup`` or ``POST /public/bookings``."""
    return CaptchaConfigOut(**captcha.public_config(get_settings()))
