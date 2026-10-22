"""Schemas for the business profile and reviews."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator

from app.core.countries import BusinessAddress, Country, check_address
from app.schemas.countries import TimezoneChoice


def _http_url(value: str) -> str:
    """Only plain web addresses: ``javascript:`` and friends would be dangerous to render on a booking page."""
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or len(value) > 500 or any(c.isspace() for c in value):
        raise ValueError("must be an http(s) address of at most 500 characters")
    return value


HttpUrlStr = Annotated[str, AfterValidator(_http_url)]

#: What only one kind of business has, kept in ``details`` (blueprint §7-11).
DETAIL_KEYS: dict[str, frozenset[str]] = {
    "restaurant": frozenset({"cuisine_type"}),
    "automotive": frozenset({"workshop_category"}),
    "professional": frozenset({"profession_type"}),
}


class AddressParts(BaseModel):
    """A business address in parts. All of it or none: street, city and postcode (and the state, where the country has them) are needed once any is
    given, and are checked against the business's country (``app.core.countries``). Singapore has no states. Outside Malaysia, Singapore and
    Australia only the street and city are needed, and the state and postcode are checked loosely."""

    address_line1: str | None = Field(default=None, max_length=200)
    address_line2: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    #: A state's code or its name (``SGR`` or ``Selangor``), stored as the code; elsewhere than Malaysia and Australia, the state, province or region as written.
    state: str | None = Field(default=None, max_length=100)
    postcode: str | None = Field(default=None, max_length=16)

    def address_for(self, country: Country) -> BusinessAddress | None:
        """The address as ``country`` writes it, ``None`` when no part was given; ``AddressProblem`` when it does not fit."""
        return check_address(
            country, line1=self.address_line1, line2=self.address_line2, city=self.city, state=self.state, postcode=self.postcode
        )


class BusinessProfileIn(AddressParts):
    model_config = ConfigDict(extra="forbid")

    business_name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=2000)
    contact_no: str | None = Field(default=None, max_length=32)
    contact_email: EmailStr | None = None
    logo_image: HttpUrlStr | None = None
    gallery_images: list[HttpUrlStr] = Field(default_factory=list, max_length=20)
    social_media_links: dict[Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,30}$")], HttpUrlStr] = Field(default_factory=dict)
    #: The address on one line. When the parts (``address_line1`` ...) are sent, this is written from them and what is sent here is ignored.
    address: str | None = Field(default=None, max_length=500)
    google_maps_url: HttpUrlStr | None = None
    #: The business's own review page. The completed-visit email links to it with ``{token}`` replaced by the customer's signed review token.
    review_url: HttpUrlStr | None = None
    details: dict[str, Annotated[str, StringConstraints(max_length=100)]] = Field(default_factory=dict)

    @field_validator("review_url")
    @classmethod
    def _carries_the_token(cls, url: str | None) -> str | None:
        if url is not None and "{token}" not in url:
            raise ValueError("must contain {token}, where the signed review token goes (e.g. https://shop.example.com/review?t={token})")
        return url

    @field_validator("social_media_links")
    @classmethod
    def _few_links(cls, links: dict[str, str]) -> dict[str, str]:
        if len(links) > 10:
            raise ValueError("at most 10 social media links")
        return links


class BusinessProfileOut(BusinessProfileIn):
    """The stored profile (or, before one is saved, the tenant's name and nothing else)."""


class BusinessLocationPublic(BaseModel):
    latitude: float
    longitude: float
    service_radius_km: float | None


class BusinessPublicOut(BusinessProfileOut):
    """``GET /v1/public/business``: what the booking page shows."""

    business_type: str
    timezone: str
    #: ISO 3166 code (``MY``), and the ISO 4217 currency prices are in (``MYR``).
    country: str
    currency: str
    location: BusinessLocationPublic | None = None


class BusinessSettingsOut(BaseModel):
    """What a business chose when it was set up, and the time zones it may still move between."""

    country: str
    country_name: str
    currency: str
    phone_country_code: str
    timezone: str
    timezone_choices: list[TimezoneChoice]


class BusinessSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str = Field(min_length=1, max_length=64)


class ReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rating: Annotated[int, Field(ge=1, le=5, strict=True)]
    comment: str | None = Field(default=None, max_length=2000)


class ReviewInvitationIn(BaseModel):
    """The signed token from the link in the completed-visit message (sent in the body, not the URL, so it stays out of access logs)."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=20, max_length=2000)


class ReviewInvitationOut(BaseModel):
    """Just enough for the review page to say what is being reviewed."""

    business_name: str
    service_names: list[str]
    start_at: dt.datetime
    #: ``open``: can be reviewed now; ``already_reviewed``: a review exists; ``not_completed``: the visit is not (or no longer) a completed one.
    status: Literal["open", "already_reviewed", "not_completed"]


class ReviewByInvitation(ReviewCreate):
    token: str = Field(min_length=20, max_length=2000)


class ReviewPublicOut(BaseModel):
    id: uuid.UUID
    rating: int
    comment: str | None
    #: First name only.
    reviewer: str
    resource_id: uuid.UUID | None
    created_at: dt.datetime


class ReviewSummary(BaseModel):
    count: int
    #: One decimal place; ``None`` when there are no reviews.
    average: float | None


class ReviewAdminOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    booking_id: uuid.UUID
    customer_id: uuid.UUID
    resource_id: uuid.UUID | None
    rating: int
    comment: str | None
    status: str
    created_at: dt.datetime


class ReviewModerate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["published", "hidden"]

