"""The countries the service is built for: Malaysia, Singapore and Australia.

A business picks its country when it signs up, and the country settles the rest in one place: which time zones it may choose from, the code that
completes a phone number typed without one, the shape of a postcode, the states of an address, the currency, and the bias given to address lookups
(:mod:`app.services.geocoding`). Adding a country is adding an entry to :data:`COUNTRIES` (and a test); nothing else names one.

* **Time zones** are IANA names, checked against the country's own list. Malaysia and Singapore keep one clock all year; Australia has nine zones and
  daylight saving in the south-east (Sydney, Melbourne, Hobart, Adelaide), none in Queensland, Western Australia or the Northern Territory.
* **Postcodes** are checked by shape only (4, 5 or 6 digits), not against the post office's list of real ones.
* **Phone numbers** stay the business of :mod:`app.core.phone`; this module only says which calling code a country has.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CountryCode = Literal["MY", "SG", "AU"]


class AddressProblem(ValueError):
    """A business or customer address that does not fit its country (the message says how, for the person who typed it)."""


@dataclass(frozen=True)
class Country:
    code: CountryCode
    name: str
    #: Digits, no plus: what completes a national phone number.
    calling_code: str
    #: ISO 4217.
    currency: str
    #: ``(IANA name, who it is for)``; the first is the default.
    timezones: tuple[tuple[str, str], ...]
    postcode_pattern: re.Pattern[str]
    postcode_example: str
    #: ``(code, name)`` of the states and territories an address names; empty when the country has none (Singapore).
    states: tuple[tuple[str, str], ...]
    #: The lookup bias for typed addresses (Google's ``region``: a country-code domain suffix).
    region: str
    privacy_law: str

    @property
    def default_timezone(self) -> str:
        return self.timezones[0][0]

    def has_timezone(self, name: str) -> bool:
        return any(name == zone for zone, _ in self.timezones)

    def state_name(self, code: str) -> str:
        return dict(self.states)[code]


COUNTRIES: dict[str, Country] = {c.code: c for c in (
    Country(
        code="MY", name="Malaysia", calling_code="60", currency="MYR",
        timezones=(("Asia/Kuala_Lumpur", "Malaysia (all states)"),),
        postcode_pattern=re.compile(r"\d{5}", re.ASCII), postcode_example="50450",
        states=(
            ("JHR", "Johor"), ("KDH", "Kedah"), ("KTN", "Kelantan"), ("MLK", "Melaka"), ("NSN", "Negeri Sembilan"), ("PHG", "Pahang"),
            ("PNG", "Pulau Pinang"), ("PRK", "Perak"), ("PLS", "Perlis"), ("SGR", "Selangor"), ("TRG", "Terengganu"), ("SBH", "Sabah"),
            ("SWK", "Sarawak"), ("KUL", "Kuala Lumpur"), ("LBN", "Labuan"), ("PJY", "Putrajaya"),
        ),
        region="my", privacy_law="Personal Data Protection Act 2010 (PDPA)",
    ),
    Country(
        code="SG", name="Singapore", calling_code="65", currency="SGD",
        timezones=(("Asia/Singapore", "Singapore"),),
        postcode_pattern=re.compile(r"\d{6}", re.ASCII), postcode_example="018956",
        states=(), region="sg", privacy_law="Personal Data Protection Act 2012 (PDPA)",
    ),
    Country(
        code="AU", name="Australia", calling_code="61", currency="AUD",
        timezones=(
            ("Australia/Sydney", "New South Wales and the Australian Capital Territory (daylight saving)"),
            ("Australia/Melbourne", "Victoria (daylight saving)"),
            ("Australia/Brisbane", "Queensland (no daylight saving)"),
            ("Australia/Adelaide", "South Australia (daylight saving)"),
            ("Australia/Perth", "Western Australia (no daylight saving)"),
            ("Australia/Darwin", "Northern Territory (no daylight saving)"),
            ("Australia/Hobart", "Tasmania (daylight saving)"),
            ("Australia/Broken_Hill", "Broken Hill, New South Wales (South Australian time, daylight saving)"),
            ("Australia/Eucla", "Eucla, Western Australia (UTC+8:45, no daylight saving)"),
            ("Australia/Lord_Howe", "Lord Howe Island (30-minute daylight saving)"),
        ),
        postcode_pattern=re.compile(r"\d{4}", re.ASCII), postcode_example="2000",
        states=(
            ("NSW", "New South Wales"), ("VIC", "Victoria"), ("QLD", "Queensland"), ("SA", "South Australia"), ("WA", "Western Australia"),
            ("TAS", "Tasmania"), ("NT", "Northern Territory"), ("ACT", "Australian Capital Territory"),
        ),
        region="au", privacy_law="Privacy Act 1988 (Australian Privacy Principles)",
    ),
)}

#: Which country a calling code means (for a business set up with only a phone code, as before countries existed).
BY_CALLING_CODE: dict[str, Country] = {c.calling_code: c for c in COUNTRIES.values()}


def get(code: str) -> Country:
    """The country with this code (``MY``, ``SG`` or ``AU``); ``KeyError`` for any other."""
    return COUNTRIES[code]


def normalise_postcode(country: Country, raw: str) -> str:
    """``raw`` with its spaces taken out, if it is the right shape for the country."""
    text = "".join(raw.split())
    if not country.postcode_pattern.fullmatch(text):
        digits = len(country.postcode_example)
        raise AddressProblem(f"A {country.name} postcode has {digits} digits (for example {country.postcode_example})")
    return text


def find_state(country: Country, raw: str) -> str:
    """The state's code from its code or its name, in any letter case (``selangor`` and ``sgr`` are both ``SGR``)."""
    wanted = " ".join(raw.split()).casefold()
    for code, name in country.states:
        if wanted in (code.casefold(), name.casefold()):
            return code
    raise AddressProblem(f"{raw.strip()!r} is not a state or territory of {country.name}; use one of: " + ", ".join(code for code, _ in country.states))


@dataclass(frozen=True)
class BusinessAddress:
    line1: str
    line2: str | None
    city: str
    #: The state's code; ``None`` where the country has no states.
    state: str | None
    postcode: str


def check_address(
    country: Country, *, line1: str | None, line2: str | None, city: str | None, state: str | None, postcode: str | None
) -> BusinessAddress | None:
    """A structured address as the country writes it, or ``None`` when none of it was given.

    All or nothing: street (``line1``), ``city`` and ``postcode`` are required once any part is given, and ``state`` too wherever the country has
    states. Singapore has none (and the city, Singapore, may be left out). The postcode and state are put in their standard spelling.
    """
    parts = [(line1 or "").strip(), (line2 or "").strip(), (city or "").strip(), (state or "").strip(), (postcode or "").strip()]
    if not any(parts):
        return None
    street, extra, town, region, code = parts
    if not street:
        raise AddressProblem("The street (address_line1) is needed")
    if not code:
        raise AddressProblem("The postcode is needed")
    if country.states:
        if not town:
            raise AddressProblem("The city or suburb is needed")
        if not region:
            raise AddressProblem("The state is needed: one of " + ", ".join(c for c, _ in country.states))
        state_code: str | None = find_state(country, region)
    else:
        if region:
            raise AddressProblem(f"{country.name} has no states; leave the state out")
        state_code, town = None, town or country.name
    return BusinessAddress(street, extra or None, town, state_code, normalise_postcode(country, code))


def profile_columns(country: Country, address: BusinessAddress | None) -> dict[str, str | None]:
    """The ``business_profiles`` columns an address fills: its parts and the one-line ``address`` written from them (nothing for ``None``)."""
    if address is None:
        return {"address_line1": None, "address_line2": None, "city": None, "state": None, "postcode": None}
    return {
        "address": format_address(country, address), "address_line1": address.line1, "address_line2": address.line2,
        "city": address.city, "state": address.state, "postcode": address.postcode,
    }


def format_address(country: Country, address: BusinessAddress) -> str:
    """The address on one line, in the order the country writes it."""
    street = ", ".join(part for part in (address.line1, address.line2) if part)
    if country.code == "AU":
        return f"{street}, {address.city} {address.state} {address.postcode}"
    if country.code == "SG":
        return f"{street}, {country.name} {address.postcode}"
    state = country.state_name(address.state) if address.state else ""
    return f"{street}, {address.postcode} {address.city}" + (f", {state}" if state else "")
