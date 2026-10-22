"""What ``GET /v1/countries`` tells a sign-up or settings page: the countries, their time zones, states and postcode shape."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.countries import Country


class TimezoneChoice(BaseModel):
    timezone: str
    label: str


class StateOut(BaseModel):
    code: str
    name: str


class CountryOut(BaseModel):
    code: str
    name: str
    calling_code: str
    currency: str
    default_timezone: str
    timezones: list[TimezoneChoice]
    #: Malaysia, Singapore and Australia: an exact postcode (``postcode_digits``), a state from ``states`` (none for Singapore). Every other
    #: country (``false``): city needed, state, province or region typed freely and optional, postcode optional (2 to 10 letters, digits,
    #: spaces or hyphens).
    exact_address: bool
    #: ``null`` where the postcode is not checked exactly (``exact_address`` false).
    postcode_digits: int | None
    postcode_example: str | None
    #: Empty where the country has none (Singapore) or ``exact_address`` is false: the address form then has no list of states.
    states: list[StateOut]
    #: The data-protection law the business's customers' data falls under; ``null`` where the service does not name it.
    privacy_law: str | None


def country_out(country: Country) -> CountryOut:
    return CountryOut(
        code=country.code, name=country.name, calling_code=country.calling_code, currency=country.currency,
        default_timezone=country.default_timezone,
        timezones=[TimezoneChoice(timezone=zone, label=label) for zone, label in country.timezones],
        exact_address=country.exact_address,
        postcode_digits=len(country.postcode_example) if country.postcode_example else None, postcode_example=country.postcode_example,
        states=[StateOut(code=code, name=name) for code, name in country.states], privacy_law=country.privacy_law,
    )
