"""Bot check in front of sign-up and guest booking (Cloudflare Turnstile).

The web page shows Turnstile's widget, which hands the browser a one-time token; the page sends it in the ``X-Captcha-Token`` header with the request, and
:func:`check` asks Cloudflare (``siteverify``) whether it is genuine before anything else happens. The widget alone protects nothing: a bot skips the page and
calls the API, which is why the check is made here. Off unless ``CAPTCHA_PROVIDER=turnstile`` (with its two keys).

* **What a token proves**: it was issued for this site key, within the last 5 minutes, and has not been used before (Cloudflare refuses a second use). The page
  names the form when it makes the widget (``action``: ``signup`` or ``booking``) and the check requires the same name back, so a token from one form cannot be
  spent on the other.
* **Who is exempt**: a request made with a *secret* API key (the WordPress server booking on a visitor's behalf) - it holds full administrator power anyway and
  cannot show a widget.
* **When Cloudflare cannot be reached** (or our secret is refused, which is our misconfiguration, not the visitor's fault): ``503 captcha_unavailable``, so nobody
  gets in unchecked - unless ``CAPTCHA_FAIL_OPEN=true``, which lets requests through (and logs an error) rather than turn customers away during an outage.
* The token is never logged, stored or put in an error message.

Only tested against a fake HTTP transport; no real Cloudflare account has been used.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.exceptions import CaptchaFailed, CaptchaRequired, CaptchaUnavailable

log = logging.getLogger(__name__)

#: Turnstile refuses longer tokens; so do we, without asking.
MAX_TOKEN_LENGTH = 2048
#: Error codes that mean the fault is ours (a wrong secret, a malformed request) or Cloudflare's, not the visitor's.
_NOT_THE_VISITORS_FAULT = frozenset({"missing-input-secret", "invalid-input-secret", "bad-request", "internal-error"})
_ASK_AGAIN = "The verification check failed or expired; please complete it again"


class CaptchaVerifier(Protocol):
    name: str

    async def verify(self, token: str, *, action: str, remote_ip: str | None = None) -> None:
        """Return if ``token`` is genuine and was made for ``action``; raise :class:`CaptchaFailed` if not, :class:`CaptchaUnavailable` if it could not be decided."""


class TurnstileVerifier:
    """Cloudflare Turnstile's ``siteverify`` endpoint."""

    name = "turnstile"

    def __init__(self, secret: str, *, verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify", timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._secret = secret
        self.verify_url, self.timeout, self.transport = verify_url, timeout, transport

    async def verify(self, token: str, *, action: str, remote_ip: str | None = None) -> None:
        form = {"secret": self._secret, "response": token}
        if remote_ip:
            form["remoteip"] = remote_ip
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport, trust_env=False) as client:
                response = await client.post(self.verify_url, data=form)
        except httpx.HTTPError as exc:
            log.warning("captcha: Turnstile could not be reached: %s", type(exc).__name__)
            raise CaptchaUnavailable("The verification check is not available just now; please try again shortly") from exc
        try:
            body = response.json()
            success = body["success"]
            codes = [str(c) for c in body.get("error-codes", [])]
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("captcha: unreadable answer from Turnstile (HTTP %s)", response.status_code)
            raise CaptchaUnavailable("The verification check is not available just now; please try again shortly") from exc
        if success is not True:
            if _NOT_THE_VISITORS_FAULT & set(codes):
                log.error("captcha: Turnstile refused our request (%s): check TURNSTILE_SECRET_KEY", ", ".join(codes))
                raise CaptchaUnavailable("The verification check is not available just now; please try again shortly")
            raise CaptchaFailed(_ASK_AGAIN, extra={"reason": codes[0] if codes else "failed"})
        if body.get("action") != action:  # the token was made for another form (or the page forgot to name the form)
            raise CaptchaFailed(_ASK_AGAIN, extra={"reason": "wrong-action"})


_verifier: CaptchaVerifier | None = None
_fail_open = False


def set_verifier(verifier: CaptchaVerifier | None, *, fail_open: bool = False) -> None:
    global _verifier, _fail_open
    _verifier, _fail_open = verifier, fail_open


def get_verifier() -> CaptchaVerifier | None:
    return _verifier


def configure(settings: Settings) -> None:
    """Register the verifier the settings describe (called when the API starts)."""
    if settings.captcha_provider == "turnstile":
        set_verifier(
            TurnstileVerifier(settings.turnstile_secret_key.get_secret_value(), verify_url=settings.turnstile_verify_url, timeout=settings.captcha_timeout_seconds),
            fail_open=settings.captcha_fail_open,
        )
    else:
        set_verifier(None)


def public_config(settings: Settings) -> dict[str, Any]:
    """What a page needs to show the widget (the site key is public by design; the secret never leaves the server)."""
    if settings.captcha_provider != "turnstile":
        return {"enabled": False, "provider": None, "site_key": None, "protects": []}
    return {"enabled": True, "provider": "turnstile", "site_key": settings.turnstile_site_key, "protects": ["signup", "booking"]}


async def check(token: str | None, *, action: str, remote_ip: str | None = None) -> None:
    """Make the bot check for ``action``; a no-op when no verifier is configured."""
    verifier = _verifier
    if verifier is None:
        return
    token = (token or "").strip()
    if not token:
        raise CaptchaRequired("Complete the verification check and send its token in the X-Captcha-Token header")
    if len(token) > MAX_TOKEN_LENGTH:
        raise CaptchaFailed(_ASK_AGAIN, extra={"reason": "invalid-input-response"})
    try:
        await verifier.verify(token, action=action, remote_ip=remote_ip)
    except CaptchaUnavailable:
        if not _fail_open:
            raise
        log.error("captcha: the check could not be made and CAPTCHA_FAIL_OPEN is on: letting the %s request through unchecked", action)
