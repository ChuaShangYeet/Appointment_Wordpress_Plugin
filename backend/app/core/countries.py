"""The countries a business can be in: every country in the world, with Malaysia, Singapore and Australia known in detail.

A business picks its country when it signs up, and the country settles the rest in one place: which time zones it may choose from, the code that
completes a phone number typed without one, the shape of a postcode, the states of an address, the currency, and the bias given to address lookups
(:mod:`app.services.geocoding`). Nothing else names a country.

* **Two kinds of country.** Malaysia, Singapore and Australia are written out here by hand (``exact_address``): the exact postcode shape, the list
  of states, the privacy law. Every other country comes from :mod:`app.core.world` (made by ``scripts/make_world.py`` from the IANA time-zone
  database, libphonenumber and the Unicode CLDR) and gets the basic rules: the state, province or region is typed freely and optional, and the
  postcode is optional and checked loosely (letters, digits, spaces and hyphens), since no list says how every country writes one.
* **Time zones** are IANA names, checked against the country's own list (``zone.tab`` for the world). A business's clock is that zone, and
  ``zoneinfo`` applies its daylight saving; nothing else about time depends on the country. Malaysia and Singapore keep one clock all year;
  Australia has nine zones and daylight saving in the south-east (Sydney, Melbourne, Hobart, Adelaide), none in Queensland, Western Australia or
  the Northern Territory.
* **Postcodes** are checked by shape only, not against the post office's list of real ones.
* **Phone numbers** stay the business of :mod:`app.core.phone`; this module only says which calling code a country has.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from app.core.world import WORLD


class AddressProblem(ValueError):
    """A business or customer address that does not fit its country (the message says how, for the person who typed it)."""


@dataclass(frozen=True)
class Country:
    #: ISO 3166 (``MY``).
    code: str
    name: str
    #: Digits, no plus: what completes a national phone number.
    calling_code: str
    #: ISO 4217.
    currency: str
    #: ``(IANA name, who it is for)``; the first is the default.
    timezones: tuple[tuple[str, str], ...]
    #: ``None`` for a country with the basic rules: any postcode of :data:`LOOSE_POSTCODE`'s shape, or none.
    postcode_pattern: re.Pattern[str] | None
    postcode_example: str | None
    #: ``(code, name)`` of the states and territories an address names; empty when the country has none (Singapore) or has the basic rules.
    states: tuple[tuple[str, str], ...]
    #: The lookup bias for typed addresses (Google's ``region``: a country-code domain suffix).
    region: str
    #: ``None`` where the service does not know it (every country but the three).
    privacy_law: str | None = None
    #: Known in detail (Malaysia, Singapore, Australia): the postcode's exact shape, the list of states. The rest have the basic rules (above).
    exact_address: bool = False

    @property
    def default_timezone(self) -> str:
        return self.timezones[0][0]

    def has_timezone(self, name: str) -> bool:
        return any(name == zone for zone, _ in self.timezones)

    def state_name(self, code: str) -> str:
        return dict(self.states)[code]


DETAILED = (
    Country(
        code="MY", name="Malaysia", calling_code="60", currency="MYR",
        timezones=(("Asia/Kuala_Lumpur", "Malaysia (all states)"),),
        postcode_pattern=re.compile(r"\d{5}", re.ASCII), postcode_example="50450",
        states=(
            ("JHR", "Johor"), ("KDH", "Kedah"), ("KTN", "Kelantan"), ("MLK", "Melaka"), ("NSN", "Negeri Sembilan"), ("PHG", "Pahang"),
            ("PNG", "Pulau Pinang"), ("PRK", "Perak"), ("PLS", "Perlis"), ("SGR", "Selangor"), ("TRG", "Terengganu"), ("SBH", "Sabah"),
            ("SWK", "Sarawak"), ("KUL", "Kuala Lumpur"), ("LBN", "Labuan"), ("PJY", "Putrajaya"),
        ),
        region="my", privacy_law="Personal Data Protection Act 2010 (PDPA)", exact_address=True,
    ),
    Country(
        code="SG", name="Singapore", calling_code="65", currency="SGD",
        timezones=(("Asia/Singapore", "Singapore"),),
        postcode_pattern=re.compile(r"\d{6}", re.ASCII), postcode_example="018956",
        states=(), region="sg", privacy_law="Personal Data Protection Act 2012 (PDPA)", exact_address=True,
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
        region="au", privacy_law="Privacy Act 1988 (Australian Privacy Principles)", exact_address=True,
    ),
)


def _region(code: str) -> str:
    return "uk" if code == "GB" else code.lower()  # (Google's region is the country-code domain: the United Kingdom's is .uk, not .gb)


#: Every country, by ISO code: the world with the basic rules, and the three known in detail over them.
COUNTRIES: dict[str, Country] = {
    code: Country(
        code=code, name=name, calling_code=calling, currency=currency, timezones=zones,
        postcode_pattern=None, postcode_example=None, states=(), region=_region(code),
    )
    for code, name, calling, currency, zones in WORLD
} | {c.code: c for c in DETAILED}

#: Every country in the order of its name, as a list to pick from shows them (accents aside: Åland Islands among the A's).
IN_NAME_ORDER: tuple[Country, ...] = tuple(
    sorted(COUNTRIES.values(), key=lambda c: unicodedata.normalize("NFKD", c.name).encode("ascii", "ignore").decode().casefold())
)

#: Which country a calling code means, for a business set up with only a phone code, as before countries existed. Only the three there were then
#: (a code such as 1 or 44 is more than one country, so a business elsewhere names its country).
BY_CALLING_CODE: dict[str, Country] = {c.calling_code: c for c in DETAILED}

#: A postcode in a country with the basic rules: 2 to 10 letters, digits, spaces and hyphens (``SW1A 1AA``, ``10115``, ``100-0001``, ``H3Z 2Y7``).
LOOSE_POSTCODE = re.compile(r"[A-Z0-9][A-Z0-9 -]{0,8}[A-Z0-9]", re.ASCII)


def get(code: str) -> Country:
    """The country with this ISO code (``MY``); ``KeyError`` for a code that is not one."""
    return COUNTRIES[code]


def normalise_postcode(country: Country, raw: str) -> str:
    """``raw`` in its standard spelling, if it is the right shape for the country: without spaces for the three known in detail, in capitals with
    single spaces for the rest (``sw1a  1aa`` -> ``SW1A 1AA``)."""
    if country.postcode_pattern is None:
        text = " ".join(raw.split()).upper()
        if not LOOSE_POSTCODE.fullmatch(text):
            raise AddressProblem("A postcode has 2 to 10 letters, digits, spaces or hyphens")
        return text
    text = "".join(raw.split())
    if not country.postcode_pattern.fullmatch(text):
        digits = len(country.postcode_example or "")
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
    #: The state's code in a country known in detail, else the state, province or region as typed; ``None`` where there is none.
    state: str | None
    #: ``None`` only in a country with the basic rules, where it may be left out.
    postcode: str | None


def check_address(
    country: Country, *, line1: str | None, line2: str | None, city: str | None, state: str | None, postcode: str | None
) -> BusinessAddress | None:
    """A structured address as the country writes it, or ``None`` when none of it was given.

    All or nothing: street (``line1``), ``city`` and ``postcode`` are required once any part is given, and ``state`` too wherever the country has
    states. Singapore has none (and the city, Singapore, may be left out). The postcode and state are put in their standard spelling.
    A country with the basic rules needs the street and city; its state and postcode may be left out.
    """
    parts = [(line1 or "").strip(), (line2 or "").strip(), (city or "").strip(), (state or "").strip(), (postcode or "").strip()]
    if not any(parts):
        return None
    street, extra, town, region, code = parts
    if not street:
        raise AddressProblem("The street (address_line1) is needed")
    if not country.exact_address:
        if not town:
            raise AddressProblem("The city or town is needed")
        return BusinessAddress(street, extra or None, town, " ".join(region.split()) or None, normalise_postcode(country, code) if code else None)
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
    if not country.exact_address:  # street, city, then the region and postcode together (Toronto's "ON M5V 2T6")
        return ", ".join(part for part in (street, address.city, " ".join(p for p in (address.state, address.postcode) if p)) if part)
    if country.code == "AU":
        return f"{street}, {address.city} {address.state} {address.postcode}"
    if country.code == "SG":
        return f"{street}, {country.name} {address.postcode}"
    state = country.state_name(address.state) if address.state else ""
    return f"{street}, {address.postcode} {address.city}" + (f", {state}" if state else "")
