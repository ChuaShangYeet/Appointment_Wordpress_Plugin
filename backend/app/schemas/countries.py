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
    postcode_digits: int
    postcode_example: str
    #: Empty where the country has none (Singapore): the address form then has no state field.
    states: list[StateOut]
    #: The data-protection law the business's customers' data falls under.
    privacy_law: str


def country_out(country: Country) -> CountryOut:
    return CountryOut(
        code=country.code, name=country.name, calling_code=country.calling_code, currency=country.currency,
        default_timezone=country.default_timezone,
        timezones=[TimezoneChoice(timezone=zone, label=label) for zone, label in country.timezones],
        postcode_digits=len(country.postcode_example), postcode_example=country.postcode_example,
        states=[StateOut(code=code, name=name) for code, name in country.states], privacy_law=country.privacy_law,
    )
