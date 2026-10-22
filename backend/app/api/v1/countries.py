"""The countries a business can be in (no key needed: a sign-up form has no business yet)."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.countries import IN_NAME_ORDER
from app.schemas.countries import CountryOut, country_out

router = APIRouter(prefix="/countries", tags=["countries"])


@router.get("", response_model=list[CountryOut])
async def list_countries() -> list[CountryOut]:
    """Every country, by name: for each, the time zones to choose from, the phone code, the currency, and how an address is checked (exactly, with
    a list of states, for Malaysia, Singapore and Australia; with the basic rules for the rest: see ``exact_address``).

    The same for everyone and read from memory, so it is not rate-limited.
    """
    return [country_out(country) for country in IN_NAME_ORDER]
