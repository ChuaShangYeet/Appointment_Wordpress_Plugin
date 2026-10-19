"""The countries the service is built for (no key needed: a sign-up form has no business yet)."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.countries import COUNTRIES
from app.schemas.countries import CountryOut, country_out

router = APIRouter(prefix="/countries", tags=["countries"])


@router.get("", response_model=list[CountryOut])
async def list_countries() -> list[CountryOut]:
    """Malaysia, Singapore and Australia: for each, the time zones to choose from, the states and postcode shape of an address, and the currency.

    The same for everyone and read from memory, so it is not rate-limited.
    """
    return [country_out(country) for country in COUNTRIES.values()]
