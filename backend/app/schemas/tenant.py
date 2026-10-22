"""Tenant onboarding and authentication schemas."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

from app.core import countries, origins
from app.core.countries import AddressProblem
from app.schemas.business import AddressParts

Role = Literal["owner", "manager", "staff"]


class BusinessSetup(AddressParts):
    """A new business, whoever asks for it. The country settles the rest: leave ``timezone`` and ``phone_country_code`` out and they follow it; give them
    and they must belong to it. Left out, the country follows ``phone_country_code`` (businesses set up before countries existed) or is Malaysia."""

    name: str = Field(min_length=1, max_length=150)
    business_type: Literal["spa", "salon", "restaurant", "automotive", "professional"] = "spa"
    #: ISO 3166 code: any country ``GET /v1/countries`` lists.
    country: str = Field(default="MY", min_length=2, max_length=2)
    timezone: str = "Asia/Kuala_Lumpur"
    #: Calling code (digits, no plus) that completes phone numbers customers type without one.
    phone_country_code: str = Field(default="60", pattern=r"^[1-9]\d{0,2}$")
    site_name: str = "Default site"
    domain: str | None = None
    allowed_origins: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _settle_the_country(self) -> "BusinessSetup":
        given = set(self.model_fields_set)
        if "country" not in given and "phone_country_code" in given:
            found = countries.BY_CALLING_CODE.get(self.phone_country_code)
            if found is None:
                raise ValueError(f"phone_country_code {self.phone_country_code} is none of Malaysia (60), Singapore (65) and Australia (61)")
            self.country = found.code
        if self.country not in countries.COUNTRIES:
            raise ValueError(f"{self.country!r} is not a country code the service knows (two letters, ISO 3166, such as MY; see GET /v1/countries)")
        country = countries.get(self.country)
        if "phone_country_code" in given and self.phone_country_code != country.calling_code:
            raise ValueError(f"phone_country_code {self.phone_country_code} is not {country.name}'s ({country.calling_code})")
        self.phone_country_code = country.calling_code
        if "timezone" not in given:
            self.timezone = country.default_timezone
        elif not country.has_timezone(self.timezone):
            choices = ", ".join(zone for zone, _ in country.timezones)
            raise ValueError(f"{self.timezone!r} is not one of {country.name}'s time zones ({choices}); set country if the business is elsewhere")
        try:
            self.address_for(country)
        except AddressProblem as exc:
            raise ValueError(str(exc)) from exc
        return self


class TenantCreate(BusinessSetup):
    """What the platform operator sends to set a business up."""

    #: The limits that apply (``GET /v1/platform/plans``): ``paid`` (none) unless said, ``free`` for the free tier.
    plan: str = "paid"
    #: The address the owner signs in with (``POST /v1/owner/login``); optional here (it can be set later), and unique across businesses.
    owner_email: EmailStr | None = None


class SignupRequest(BusinessSetup):
    """What a business sends to set itself up (``POST /v1/signup``): the same as the operator's, with the owner's email, the terms accepted and an address that
    is **required**. There is no ``plan``: a business that signs itself up starts on the free plan, and asking for another is refused."""

    model_config = ConfigDict(extra="forbid")

    owner_email: EmailStr
    #: Must be the current version (``GET /v1/signup/config``): the page shows the terms, the person ticks the box, the page sends the version it showed.
    accepted_terms_version: str = Field(min_length=1, max_length=40)

    @model_validator(mode="after")
    def _a_website_is_needed(self) -> "SignupRequest":
        """One account, one business, one website: the free plan needs the website's address, and the browser origins follow from it (a list of them is refused)."""
        if self.allowed_origins:
            raise ValueError("allowed_origins cannot be sent on sign-up: the website's address (domain) is all that is needed, the origins follow from it")
        if not self.domain or not self.domain.strip():
            raise ValueError("The business's website address (domain) is needed, such as shop.example")
        try:
            origins.host_of(self.domain)
        except origins.WebsiteProblem as exc:
            raise ValueError(str(exc)) from exc
        return self

    @model_validator(mode="after")
    def _an_address_is_needed(self) -> "SignupRequest":
        if not any((self.address_line1, self.address_line2, self.city, self.state, self.postcode)):
            raise ValueError("The business's address is needed (address_line1, city, state and postcode)")
        return self


class SignupVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=200)


class CaptchaConfigOut(BaseModel):
    """The bot check a page has to pass: build Turnstile's widget with ``site_key`` and ``action`` set to one of ``protects``."""

    enabled: bool
    provider: str | None
    site_key: str | None
    protects: list[str]


class SignupConfigOut(BaseModel):
    enabled: bool
    terms_version: str
    terms_url: str | None
    captcha: CaptchaConfigOut


class TenantCreated(BaseModel):
    """Secrets are shown exactly once; only hashes / ciphertext are stored."""

    tenant_id: uuid.UUID
    site_id: uuid.UUID
    publishable_key: str
    secret_key: str
    webhook_secret: str


class TokenRequest(BaseModel):
    """Sent server-to-server by the WordPress plugin, authenticated by ``X-API-Key`` (secret)."""

    user_id: str = Field(min_length=1, max_length=100)
    role: Role
    #: Staff tokens can be pinned to their own resource.
    resource_id: uuid.UUID | None = None
    #: Shown in the users list; optional (WordPress knows the name, the backend does not need it).
    display_name: str | None = Field(default=None, max_length=150)
    #: The address the plugin runs on (``https://shop.example``). A signal for the platform operator and nothing more: optional, never checked here, and anything at all
    #: (even a wrong type) is accepted - a request is never refused because of it.
    site_url: Any = None


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int


class OwnerLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr


class OwnerLoginVerify(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=200)


class OwnerSession(TokenResponse):
    """An owner admin token, and the business it is for."""

    tenant_id: uuid.UUID


class OwnerEmailIn(BaseModel):
    """The address a business's owner signs in with. ``null`` removes it (the owner can then no longer sign in by email)."""

    model_config = ConfigDict(extra="forbid")

    email: EmailStr | None


class OwnerEmailSet(BaseModel):
    tenant_id: uuid.UUID
    owner_email: str | None


class RecoverKeysIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Revoke every other key the business has, once the new pair exists (a lost key that someone else may hold). Off: the old keys keep working until revoked one by one.
    revoke_existing: bool = False


class RecoveredKeys(BaseModel):
    """A new pair of keys for the business's site, **shown this once**."""

    site_id: uuid.UUID
    publishable_key: str
    secret_key: str
    #: How many other keys were revoked (``revoke_existing``).
    revoked: int


class ApiKeyCreate(BaseModel):
    kind: Literal["publishable", "secret"]
    site_id: uuid.UUID | None = None


class ApiKeyCreated(BaseModel):
    id: uuid.UUID
    kind: str
    key: str


class ApiKeyOut(BaseModel):
    """A key as the owner sees it afterwards: which one it is (the first characters), never the key itself."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: str
    prefix: str
    site_id: uuid.UUID | None
    created_at: dt.datetime
    revoked_at: dt.datetime | None


class OtpRequest(BaseModel):
    phone: str = Field(min_length=6, max_length=32)


class OtpVerify(OtpRequest):
    code: str = Field(min_length=6, max_length=6)
    name: str | None = None
    email: EmailStr | None = None


class MagicLinkRequest(BaseModel):
    """Ask for a sign-in link by email. ``phone`` tells apart customers who share one email address."""

    email: EmailStr
    phone: str | None = Field(default=None, min_length=6, max_length=32)


class MagicLinkVerify(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class EmailChange(BaseModel):
    """The email a signed-in customer wants confirmed as their own; a link is sent to it."""

    email: EmailStr


class EmailConfirm(BaseModel):
    token: str = Field(min_length=20, max_length=200)
