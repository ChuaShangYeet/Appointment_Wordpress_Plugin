"""Application settings, loaded from environment / ``.env``."""
from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration. Secrets have no defaults so a misconfigured deploy fails fast."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    #: ``development`` and ``test`` relax the start-up safety checks and let messages be logged instead of sent; any other value is treated as a real
    #: deployment. The default is the safe one: a deployment that forgets to say so is checked, not waved through.
    environment: str = "production"
    database_url: str = "postgresql+asyncpg://booking:booking@localhost:5432/booking"
    #: The role the migrations run as: the owner of the tables (see ``deploy/postgres/01_roles.sql``). Empty: ``database_url`` is used.
    #: ``database_url`` should then be the limited application role, which cannot change the schema or switch Row-Level Security off.
    migration_database_url: str = ""
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = Field(default_factory=list)
    #: Start even though the start-up checks (``app.core.hardening``) found something unsafe. Emergencies only: it can switch tenant isolation off.
    allow_unsafe_startup: bool = False

    jwt_secret: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    jwt_ttl_seconds: int = 900
    customer_jwt_ttl_seconds: int = 3600

    #: base64-encoded 32 random bytes (AES-256 key) for PDPA column encryption.
    pdpa_encryption_key: str
    #: static token guarding tenant onboarding (platform operator only).
    platform_admin_token: str = Field(min_length=16)

    @model_validator(mode="after")
    def _google_oauth_is_all_or_nothing(self) -> "Settings":
        parts = (self.google_oauth_client_id, self.google_oauth_client_secret.get_secret_value(), self.google_oauth_redirect_uri)
        if any(parts) and not all(parts):
            raise ValueError("GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET and GOOGLE_OAUTH_REDIRECT_URI go together: set all three or none")
        return self

    @model_validator(mode="after")
    def _captcha_has_its_keys(self) -> "Settings":
        if self.captcha_provider == "turnstile" and not (self.turnstile_site_key and self.turnstile_secret_key.get_secret_value()):
            raise ValueError("CAPTCHA_PROVIDER=turnstile needs TURNSTILE_SITE_KEY and TURNSTILE_SECRET_KEY")
        return self

    @field_validator("review_url")
    @classmethod
    def _review_url_carries_the_token(cls, value: str) -> str:
        if value and "{token}" not in value:
            raise ValueError("REVIEW_URL must contain {token}, where the signed review token goes")
        return value

    @field_validator("pdpa_encryption_key")
    @classmethod
    def _key_is_an_aes_256_key(cls, value: str) -> str:
        """Fail at start-up, not on the first health note: the key must be base64 for exactly 32 bytes."""
        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("PDPA_ENCRYPTION_KEY must be base64 (python -c \"import os,base64;print(base64.b64encode(os.urandom(32)).decode())\")") from exc
        if len(raw) != 32:
            raise ValueError(f"PDPA_ENCRYPTION_KEY must decode to 32 bytes (AES-256), not {len(raw)}")
        return value

    slot_step_minutes: int = 15
    sync_signature_tolerance_seconds: int = 300
    otp_ttl_seconds: int = 300
    #: Minimum gap between OTP requests for one phone; also bounds how fast codes can be guessed.
    otp_resend_cooldown_seconds: int = 60

    #: Sign-up refuses throw-away email addresses (``app.core.disposable_emails``). These two lists adjust the built-in one: domains to refuse as well, and domains to accept
    #: although they are on it (a domain and everything under it). JSON lists, e.g. ``BLOCKED_EMAIL_DOMAINS=["tempmail.example"]``.
    blocked_email_domains: list[str] = Field(default_factory=list)
    allowed_email_domains: list[str] = Field(default_factory=list)
    #: A business is emailed when it gets this close to one of its plan's limits (a percentage of the limit), and again when it reaches it. 0 = never.
    plan_warning_percent: int = Field(default=80, ge=0, le=99)
    #: At most this many sign-in codes (or links) are sent to one phone (or email address) for one business in a day, whatever the per-minute limits allow:
    #: a stranger who knows a number cannot fill the owner's phone, or run up the business's WhatsApp and SMS bill. 0 = no cap.
    sign_in_requests_per_day: int = Field(default=10, ge=0)
    #: The same for sign-up confirmation emails to one address (one is never sent for an address that already has a business, but the answer is the same).
    signup_requests_per_address_per_day: int = Field(default=5, ge=0)
    #: How long an emailed sign-in link works. Requests are limited like OTP codes (``otp_resend_cooldown_seconds``).
    magic_link_ttl_seconds: int = 900

    #: Channel the sign-in code goes out on (the customer has given only a phone number at that point).
    otp_channel: Literal["whatsapp", "sms"] = "whatsapp"
    #: Where the emailed link leads, with ``{token}`` where the token goes (the front end's sign-in page). Without it the email
    #: carries the bare token.
    magic_link_url: str = ""
    #: Where the link that confirms a customer's email leads (``{token}`` marks the token; the page POSTs it to ``/v1/public/auth/email/confirm``). Without it the email carries the bare token.
    email_confirm_url: str = ""
    #: Self-service sign-up (``app.services.signup``): a business sets itself up on the free plan after confirming its owner's email. Off unless switched on.
    self_service_signup: bool = False
    #: The owner signs in by email (``app.services.owner_login``): where the emailed link leads (``{token}`` marks the token; the page POSTs it to ``/v1/owner/login/verify``),
    #: how long it works, and the pause before another may be asked for. Without ``owner_login_url`` the email carries the bare token.
    owner_login_url: str = ""
    owner_login_ttl_seconds: int = Field(default=900, ge=60)
    owner_login_cooldown_seconds: int = Field(default=60, ge=0)
    #: Where the confirmation link leads (``{token}`` marks the token; the page POSTs it to ``/v1/signup/verify``); without it the email carries the bare token.
    signup_verify_url: str = ""
    signup_token_ttl_seconds: int = Field(default=86400, ge=60)
    signup_resend_cooldown_seconds: int = Field(default=60, ge=0)
    #: The version of the terms a sign-up must accept (``accepted_terms_version``), and where they are published. Change the version when the terms change.
    terms_version: str = Field(default="1", min_length=1, max_length=40)
    terms_url: str = ""
    #: The name of the service in the emails it sends before a business exists.
    platform_name: str = Field(default="JustSimple", min_length=1, max_length=60)
    #: The completed-visit message links to a review page. ``review_url`` is the platform-wide default page (``{token}`` marks where the signed
    #: token goes); a business sets its own in its profile (``review_url``). With neither, the message just says the visit can be rated after signing in.
    #: The link stops working ``review_link_days`` days after the message was made.
    review_url: str = ""
    review_link_days: int = Field(default=30, ge=1, le=365)
    #: A booking message that keeps failing is retried (with the outbox back-off) this many times, then left ``failed`` in the log.
    notify_max_attempts: int = 6
    #: Reminders go out this many hours before a confirmed booking starts (an empty list switches reminders off), each at most
    #: ``reminder_lateness_minutes`` after its moment: later than that it is skipped, not sent as a stale "tomorrow".
    reminder_hours: list[int] = Field(default_factory=lambda: [24, 2])
    reminder_lateness_minutes: int = 60
    #: A booking can be marked in-progress (the customer arrived) or completed from this many minutes before it starts, and no-show only from its start: before that the
    #: move would free a slot nobody has used yet and, for completed, send the thank-you message and review link ahead of the visit.
    status_early_minutes: int = Field(default=60, ge=0, le=1440)
    #: Rate limiting (``app.core.ratelimit``). ``rate_limit_trusted_proxies`` = how many reverse proxies sit in front of the API and
    #: append to ``X-Forwarded-For`` (0: ignore that header, the socket peer is the client). ``rate_limits`` overrides the defaults per bucket and tier:
    #: ``{"auth_send": {"ip": [5, 60]}}`` = 5 requests per 60 s per address; 0 requests switches that limit off.
    rate_limit_enabled: bool = True
    rate_limit_trusted_proxies: int = 0
    rate_limits: dict[str, dict[str, tuple[int, int]]] = Field(default_factory=dict)
    #: Outgoing webhooks. A delivery is tried this many times (with the outbox back-off), a webhook that fails this many attempts in a row is
    #: switched off, and at most ``webhook_batch`` deliveries are sent per tenant per sweep, ``webhook_concurrency`` at a time.
    webhook_max_attempts: int = 8
    webhook_disable_after_failures: int = 15
    webhook_timeout_seconds: float = 5.0
    webhook_batch: int = 50
    webhook_concurrency: int = 10
    webhook_log_retention_days: int = 30
    #: Development only: allow http:// and addresses on private networks as webhook targets (never in production: it defeats the SSRF protection).
    webhook_allow_insecure: bool = False
    #: Outgoing email. Without ``SMTP_HOST`` no email provider is registered (development logs messages instead).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "bookings@localhost"
    smtp_starttls: bool = True
    #: WhatsApp Cloud API (Meta). The WhatsApp provider is registered only when the token and the phone number id are both set.
    #: A business may only *start* a conversation with an approved template, so the sign-in code needs ``whatsapp_otp_template``
    #: (an approved template whose body takes the code as its one variable; ``whatsapp_otp_button`` adds the code to its
    #: "copy code" button, as Meta's authentication templates require). ``whatsapp_template_language`` is the language the templates were approved in.
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_api_version: str = "v21.0"
    whatsapp_base_url: str = "https://graph.facebook.com"
    whatsapp_template_language: str = "en"
    whatsapp_otp_template: str = ""
    whatsapp_otp_button: bool = True
    #: SMS gateway. ``twilio`` is the one built; it needs the account credentials and either a sender number or a messaging service.
    sms_provider: Literal["", "twilio"] = ""
    twilio_account_sid: str = ""
    twilio_auth_token: SecretStr = SecretStr("")
    twilio_from: str = ""
    twilio_messaging_service_sid: str = ""
    twilio_base_url: str = "https://api.twilio.com"
    #: How long a WhatsApp / SMS gateway may take to answer before the attempt counts as failed (the outbox retries it).
    delivery_timeout_seconds: float = 10.0
    #: Looking up typed addresses (``app.services.geocoding``). ``google`` needs ``google_maps_api_key``; each lookup leans towards the country of the
    #: business it is for. Without a provider customers must send their map-pin coordinates.
    geocoding_provider: Literal["", "google"] = ""
    google_maps_api_key: SecretStr = SecretStr("")
    geocoding_base_url: str = "https://maps.googleapis.com"
    geocoding_timeout_seconds: float = 5.0
    #: Following a Google Maps share link (``maps.app.goo.gl``) to the point it leads to: the time each hop may take (``app.services.maps_links``).
    maps_link_timeout_seconds: float = Field(default=5.0, gt=0, le=30)

    #: How long a computed availability answer may be served from Redis; 0 turns the cache off. Writes to
    #: anything availability depends on invalidate it at once, so this only bounds time-driven drift
    #: (a pending hold expiring, the minimum-notice cut-off moving).
    availability_cache_ttl_seconds: int = 30
    #: The background sweep (``python -m app.worker``) runs this often, and looks at a new booking only once it
    #: is this old, so that a change committing at the same moment has surely been seen (see ``app.services.sweep``).
    sweep_interval_seconds: int = 60
    sweep_grace_seconds: int = 60
    #: Published outbox events are kept this long before the sweep deletes them.
    outbox_retention_days: int = 7
    #: Health notes are cleared this many days after the booking ended (PDPA retention); 0 keeps them.
    health_notes_retention_days: int = 0
    #: Google Calendar sync (``app.services.calendar``): an OAuth client made in Google Cloud (type "web application"; its authorised redirect URI must be
    #: ``google_oauth_redirect_uri``, i.e. this server's ``/v1/calendar/oauth/callback``). All three empty: no calendar can be connected. ``calendar_done_url``
    #: is where the owner's browser lands afterwards (``?calendar=connected`` or ``?calendar=error&reason=...``); without it the callback answers with JSON.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: SecretStr = SecretStr("")
    google_oauth_redirect_uri: str = ""
    calendar_done_url: str = ""
    google_accounts_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    google_token_url: str = "https://oauth2.googleapis.com/token"
    google_revoke_url: str = "https://oauth2.googleapis.com/revoke"
    google_calendar_url: str = "https://www.googleapis.com/calendar/v3"
    calendar_timeout_seconds: float = Field(default=10.0, gt=0)
    #: Per tenant per sweep. An event that fails this many times is given up on; a calendar whose events fail this many times in a row is paused (and the business emailed).
    calendar_batch: int = Field(default=50, ge=1)
    calendar_max_attempts: int = Field(default=8, ge=1)
    calendar_pause_after_failures: int = Field(default=10, ge=1)
    #: How long the owner has between asking to connect and coming back from Google.
    calendar_state_ttl_seconds: int = Field(default=600, ge=60)
    #: Bot check in front of sign-up and guest booking (``app.services.captcha``). ``turnstile`` = Cloudflare Turnstile, which needs both keys from its dashboard
    #: (the site key is public and goes into the web page; the secret key stays here). Empty: no check. ``captcha_fail_open`` lets requests through when Cloudflare cannot
    #: be reached instead of refusing them (503): customers keep booking during an outage, and so can bots.
    captcha_provider: Literal["", "turnstile"] = ""
    turnstile_site_key: str = ""
    turnstile_secret_key: SecretStr = SecretStr("")
    turnstile_verify_url: str = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    captcha_timeout_seconds: float = Field(default=5.0, gt=0)
    captcha_fail_open: bool = False
    #: Serve the interactive API documentation (``/docs``, ``/redoc``, ``/openapi.json``). Unset: on in development and tests, off elsewhere
    #: (it lists every route, including the platform operator's).
    docs_enabled: bool | None = None
    #: Logging (``app.core.logs``): the level, and ``json`` (one object per line, for a log collector) or ``text``; ``auto`` = text in development and tests, json elsewhere.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["auto", "json", "text"] = "auto"
    #: Error reporting (``app.core.observability``): ERROR-level log lines are sent to Sentry when this is set. Empty: nothing is sent anywhere.
    sentry_dsn: SecretStr = SecretStr("")
    redis_timeout_seconds: float = 0.25
    #: After a Redis failure the cache is skipped for this long instead of timing out on every request.
    redis_backoff_seconds: float = 10.0


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
