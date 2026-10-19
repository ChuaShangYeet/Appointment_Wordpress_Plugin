"""Getting a message to a person: the provider seam for email, SMS and WhatsApp (blueprint §15).

A :class:`DeliveryProvider` sends one :class:`Message` over one channel. Providers are registered per channel
(:func:`set_provider`); nothing is registered by default, so in production an unconfigured channel *fails visibly*
(``NoProviderError``, shown in the notification log, retried by the outbox) rather than pretending to have sent.
:func:`configure_providers` registers what the settings describe: an SMTP email provider when ``SMTP_HOST`` is set, and, in
development only, a provider that just logs messages so sign-in codes can be read off the console.

What exists: the seam, the logging provider, an SMTP email provider (stdlib ``smtplib``, run in a thread), a WhatsApp Cloud API
provider (Meta) and a Twilio SMS provider (both plain HTTPS calls with ``httpx``). All of them have only been exercised against
fakes: the SMTP provider against a fake server, the two gateways against a fake HTTP transport - no live message has been sent.
Each attempt is one request; retrying is the caller's job (the outbox back-off), so a provider never loops on its own.
"""
from __future__ import annotations

import asyncio
import logging
import re
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from app.core.config import Settings

log = logging.getLogger(__name__)


class DeliveryError(Exception):
    """A message could not be handed to the provider (or the provider refused it)."""


class NoProviderError(DeliveryError):
    """Nothing is configured to send over this channel."""


class PermanentDeliveryError(DeliveryError):
    """The provider answered that it will never take *this* message: the number is not on WhatsApp, the mailbox does not exist, the recipient opted out.

    Trying again later cannot help, so the notification log records it as ``refused`` and the outbox does not retry it (nor hold back the booking's other
    messages while it does). Only refusals about the recipient are permanent: a wrong password, a full queue or a server error is the business's or the
    provider's problem, and a retry once that is fixed can still succeed.
    """


@dataclass(frozen=True)
class Message:
    channel: str
    #: Address (email) or E.164 number (sms / whatsapp).
    recipient: str
    body: str
    #: Email only.
    subject: str | None = None
    #: WhatsApp: name of the Meta-approved template; ``params`` are its variables, in order.
    external_template: str | None = None
    params: tuple[str, ...] = field(default_factory=tuple)
    #: WhatsApp authentication templates: the value for the template's "copy code" button (the sign-in code itself).
    button_param: str | None = None


class DeliveryProvider(Protocol):
    name: str

    async def send(self, message: Message) -> str | None:
        """Send ``message``; return the provider's message id if it has one, raise to signal failure."""


_providers: dict[str, DeliveryProvider] = {}


def set_provider(channel: str, provider: DeliveryProvider | None) -> None:
    if provider is None:
        _providers.pop(channel, None)
    else:
        _providers[channel] = provider


def get_provider(channel: str) -> DeliveryProvider | None:
    return _providers.get(channel)


def clear_providers() -> None:
    _providers.clear()


async def deliver(message: Message) -> tuple[str, str | None]:
    """Send through the channel's provider: ``(provider name, provider message id)``."""
    provider = _providers.get(message.channel)
    if provider is None:
        raise NoProviderError(f"No provider is configured for {message.channel}")
    return provider.name, await provider.send(message)


def mask_recipient(channel: str, recipient: str) -> str:
    """``a***@example.com`` / ``+60****0001``: enough to recognise in a log, not to contact."""
    if channel == "email":
        local, _, domain = recipient.partition("@")
        return f"{local[:1]}***@{domain}"
    return f"{recipient[:3]}****{recipient[-4:]}" if len(recipient) > 7 else "****"


class LoggingProvider:
    """Writes the message to the application log instead of sending it (development)."""

    name = "log"

    async def send(self, message: Message) -> str | None:
        log.warning("DEV %s to %s: %s%s", message.channel, message.recipient, f"[{message.subject}] " if message.subject else "", message.body)
        return None


class SmtpEmailProvider:
    """Email through an SMTP server (``SMTP_*`` settings)."""

    name = "smtp"

    def __init__(self, host: str, port: int, sender: str, *, user: str | None = None, password: str | None = None,
                 starttls: bool = True, timeout: float = 10.0) -> None:
        self.host, self.port, self.sender = host, port, sender
        self.user, self.password, self.starttls, self.timeout = user, password, starttls, timeout

    def build(self, message: Message) -> EmailMessage:
        # header injection: a line break in an address or subject would let text smuggle in extra headers
        for value in (message.recipient, message.subject or "", self.sender):
            if "\r" in value or "\n" in value:
                raise DeliveryError("Line breaks are not allowed in email headers")
        mail = EmailMessage()
        mail["From"], mail["To"], mail["Subject"] = self.sender, message.recipient, message.subject or ""
        mail.set_content(message.body)
        return mail

    def _send_blocking(self, mail: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
            if self.starttls:
                smtp.starttls()
            if self.user:
                smtp.login(self.user, self.password or "")
            smtp.send_message(mail)

    async def send(self, message: Message) -> str | None:
        mail = self.build(message)
        try:
            await asyncio.to_thread(self._send_blocking, mail)
        except smtplib.SMTPRecipientsRefused as exc:  # (smtplib raises this only when every recipient was refused)
            codes = [code for code, _ in exc.recipients.values()]
            if codes and all(code >= 500 for code in codes):  # 5xx = never; 4xx (mailbox full, greylisting) = try again
                raise PermanentDeliveryError("SMTP delivery failed: the server refused the recipient") from exc
            raise DeliveryError(f"SMTP delivery failed: {type(exc).__name__}") from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise DeliveryError(f"SMTP delivery failed: {type(exc).__name__}") from exc
        return None


def _api_error(label: str, response: httpx.Response) -> str:
    """What a gateway said when it refused a message: its status plus its own error code and text, never the request (which holds the recipient and the credentials)."""
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error", body)  # Meta nests it under "error", Twilio does not
        if isinstance(error, dict):
            detail = " ".join(str(error[k]) for k in ("code", "message") if error.get(k) is not None)
    return f"{label} {response.status_code}: {detail[:200] or 'no details'}"


#: WhatsApp Cloud API error codes that mean this recipient will not get this message however often it is tried: message undeliverable (not on WhatsApp, blocked the
#: business, has not accepted the terms), re-engagement (plain text outside the 24-hour window), per-user marketing limit (Meta: wait a day), recipient is the sender.
WHATSAPP_REFUSED = frozenset({131026, 131047, 131049, 131021})
#: Twilio error codes for a recipient: invalid or unreachable number, not a mobile number, unsubscribed (STOP), cannot be routed.
TWILIO_REFUSED = frozenset({21211, 21214, 21217, 21610, 21612, 21614})


def _error_code(response: httpx.Response) -> int | None:
    try:
        body = response.json()
    except ValueError:
        return None
    error = body.get("error", body) if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, int) else None  # (a text or list is no code; a boolean never matches one)


def _api_failure(label: str, response: httpx.Response, refused: frozenset[int]) -> DeliveryError:
    """The error for a refusal by a gateway: permanent when it was a 4xx that names one of the ``refused`` recipient codes, else a plain one to be retried."""
    message = _api_error(label, response)
    if 400 <= response.status_code < 500 and _error_code(response) in refused:
        return PermanentDeliveryError(message)
    return DeliveryError(message)


_WHITESPACE_RUN = re.compile(r"[\r\n\t]+| {4,}")


def template_param(value: str) -> str:
    """A WhatsApp template variable: Meta refuses line breaks, tabs, runs of four spaces and empty values."""
    return _WHITESPACE_RUN.sub(" ", value).strip() or "-"


class WhatsAppCloudProvider:
    """WhatsApp through Meta's Cloud API (blueprint §15).

    A business can only *start* a conversation with a Meta-approved template, so a message with an ``external_template`` is sent as that
    template (``params`` fill its body variables in order, ``button_param`` its "copy code" button). A message without one is sent as plain
    text, which Meta delivers only inside the 24 hours after the customer last wrote to the business - otherwise the API refuses it and
    the refusal shows up in the notification log.
    """

    name = "whatsapp-cloud"

    def __init__(self, access_token: str, phone_number_id: str, *, api_version: str = "v21.0", base_url: str = "https://graph.facebook.com",
                 language: str = "en", timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = access_token
        self.phone_number_id, self.api_version, self.base_url = phone_number_id, api_version, base_url.rstrip("/")
        self.language, self.timeout, self.transport = language, timeout, transport

    @property
    def url(self) -> str:
        return f"{self.base_url}/{self.api_version}/{self.phone_number_id}/messages"

    def build(self, message: Message) -> dict[str, Any]:
        to = message.recipient.lstrip("+")
        if not message.external_template:
            return {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"preview_url": False, "body": message.body}}
        components: list[dict[str, Any]] = []
        if message.params:
            components.append({"type": "body", "parameters": [{"type": "text", "text": template_param(p)} for p in message.params]})
        if message.button_param is not None:
            components.append({"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": template_param(message.button_param)}]})
        template: dict[str, Any] = {"name": message.external_template, "language": {"code": self.language}}
        if components:
            template["components"] = components
        return {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}

    async def send(self, message: Message) -> str | None:
        payload = self.build(message)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(self.url, json=payload, headers={"Authorization": f"Bearer {self._token}"})
        except httpx.HTTPError as exc:  # the exception text can carry the URL and headers: keep only its type
            raise DeliveryError(f"WhatsApp request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise _api_failure("WhatsApp API", response, WHATSAPP_REFUSED)
        try:
            return str(response.json()["messages"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError):
            return None  # accepted, but without an id we can log


class TwilioSmsProvider:
    """SMS through Twilio's REST API: from a sender number, or from a messaging service (which picks the number)."""

    name = "twilio"

    def __init__(self, account_sid: str, auth_token: str, *, from_number: str | None = None, messaging_service_sid: str | None = None,
                 base_url: str = "https://api.twilio.com", timeout: float = 10.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not (from_number or messaging_service_sid):
            raise ValueError("Twilio needs a sender number or a messaging service")
        self.account_sid, self._token = account_sid, auth_token
        self.from_number, self.messaging_service_sid = from_number, messaging_service_sid
        self.base_url, self.timeout, self.transport = base_url.rstrip("/"), timeout, transport

    @property
    def url(self) -> str:
        return f"{self.base_url}/2010-04-01/Accounts/{self.account_sid}/Messages.json"

    def build(self, message: Message) -> dict[str, str]:
        form = {"To": message.recipient, "Body": message.body}
        if self.messaging_service_sid:
            form["MessagingServiceSid"] = self.messaging_service_sid
        else:
            form["From"] = self.from_number or ""
        return form

    async def send(self, message: Message) -> str | None:
        form = self.build(message)
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.post(self.url, data=form, auth=(self.account_sid, self._token))
        except httpx.HTTPError as exc:
            raise DeliveryError(f"SMS request failed: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise _api_failure("Twilio", response, TWILIO_REFUSED)
        try:
            sid = response.json().get("sid")
        except (ValueError, AttributeError):
            return None
        return str(sid) if sid else None


def configure_providers(settings: Settings) -> None:
    """Register the providers the settings describe (called when the API and the worker start)."""
    if settings.smtp_host:
        set_provider("email", SmtpEmailProvider(
            settings.smtp_host, settings.smtp_port, settings.smtp_from, user=settings.smtp_user or None,
            password=settings.smtp_password or None, starttls=settings.smtp_starttls,
        ))
    if settings.whatsapp_access_token.get_secret_value() and settings.whatsapp_phone_number_id:
        set_provider("whatsapp", WhatsAppCloudProvider(
            settings.whatsapp_access_token.get_secret_value(), settings.whatsapp_phone_number_id, api_version=settings.whatsapp_api_version,
            base_url=settings.whatsapp_base_url, language=settings.whatsapp_template_language, timeout=settings.delivery_timeout_seconds,
        ))
    if settings.sms_provider == "twilio" and settings.twilio_account_sid and settings.twilio_auth_token.get_secret_value():
        if settings.twilio_from or settings.twilio_messaging_service_sid:
            set_provider("sms", TwilioSmsProvider(
                settings.twilio_account_sid, settings.twilio_auth_token.get_secret_value(), from_number=settings.twilio_from or None,
                messaging_service_sid=settings.twilio_messaging_service_sid or None, base_url=settings.twilio_base_url,
                timeout=settings.delivery_timeout_seconds,
            ))
        else:
            log.error("SMS_PROVIDER=twilio needs TWILIO_FROM or TWILIO_MESSAGING_SERVICE_SID; no SMS provider was registered")
    if settings.environment == "development":
        for channel in ("email", "sms", "whatsapp"):
            if get_provider(channel) is None:
                set_provider(channel, LoggingProvider())


def configuration_problems(settings: Settings) -> list[str]:
    """What is missing for sign-in codes, links and booking messages to reach people (reported at start-up outside development)."""
    problems: list[str] = []
    if get_provider("email") is None:
        problems.append("no email provider (set SMTP_HOST): booking messages and emailed sign-in links cannot be sent")
    channel = settings.otp_channel
    provider = get_provider(channel)
    if provider is None:
        problems.append(f"OTP_CHANNEL={channel} but no {channel} provider is configured: customers cannot sign in with their phone")
    elif isinstance(provider, WhatsAppCloudProvider) and not settings.whatsapp_otp_template:
        problems.append("OTP_CHANNEL=whatsapp needs WHATSAPP_OTP_TEMPLATE (an approved Meta template): WhatsApp refuses to start a conversation with plain text")
    if settings.captcha_provider != "turnstile":
        problems.append("CAPTCHA_PROVIDER is not set: sign-up and guest booking are open to bots (rate limits and email confirmation only slow them down)")
    if not settings.magic_link_url:
        problems.append("MAGIC_LINK_URL is not set: emailed sign-in links carry a bare token instead of a link")
    if settings.self_service_signup:
        if get_provider("email") is None:
            problems.append("SELF_SERVICE_SIGNUP is on but there is no email provider (SMTP_HOST): nobody can confirm their address, so nobody can sign up")
        if not settings.signup_verify_url:
            problems.append("SELF_SERVICE_SIGNUP is on but SIGNUP_VERIFY_URL is not set: the confirmation email carries a bare token instead of a link")
        if not settings.owner_login_url:
            problems.append("SELF_SERVICE_SIGNUP is on but OWNER_LOGIN_URL is not set: the owner's sign-in email carries a bare token instead of a link")
        if not settings.terms_url:
            problems.append("SELF_SERVICE_SIGNUP is on but TERMS_URL is not set: the sign-up page has no terms to show (and to be accepted)")
    return problems


def report_configuration(settings: Settings) -> list[str]:
    """Log each :func:`configuration_problems` entry as an error (outside development and tests) and return them."""
    if settings.environment in ("development", "test"):
        return []
    problems = configuration_problems(settings)
    for problem in problems:
        log.error("delivery configuration: %s", problem)
    return problems
