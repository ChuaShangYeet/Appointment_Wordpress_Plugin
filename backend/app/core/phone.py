"""Phone-number normalisation: one customer, one spelling.

Customers are keyed by ``(tenant, phone)``, so ``+60 12-345 0001``, ``012-3450001`` and ``0060123450001`` must all
reach the same row. Numbers are stored as ``+<country code><number>`` with digits only.

``normalise_phone`` is a deliberately small normaliser: it fixes the spelling, and rejects what cannot be a phone number at
all. It still finds customers saved before numbers were checked per country. ``mobile_number`` is the strict check for a
new customer's number: a mobile of the chosen country by Google's numbering plans (``phonenumbers``), because sign-in codes
and updates go by WhatsApp or SMS.
"""
from __future__ import annotations

import re

import phonenumbers
from phonenumbers import PhoneNumberFormat, PhoneNumberType

from app.core.countries import COUNTRIES
from app.core.exceptions import InvalidPhone, ValidationFailed

#: E.164 allows at most 15 digits in all; below 7 is not a dialable number anywhere.
MIN_DIGITS, MAX_DIGITS = 7, 15
_SEPARATORS = re.compile(r"[\s\-().]")


def normalise_phone(raw: str, country_code: str) -> str:
    """``raw`` as typed -> ``+<digits>``; ``country_code`` (digits, no plus) completes a national number.

    * ``+`` or ``00`` in front: already international.
    * one leading ``0``: a national number with its trunk prefix, which is dropped (``0123456789`` -> ``+60123456789``).
    * otherwise: international without the plus if it starts with the country code (``60123456789``), else a national
      number without a trunk prefix (Singapore ``91234567`` -> ``+6591234567``).
    """
    text = _SEPARATORS.sub("", raw.strip())
    international = text.startswith("+")
    digits = text[1:] if international else text
    if not digits.isascii() or not digits.isdigit():
        raise ValidationFailed("That is not a valid phone number")
    if international:
        pass
    elif digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = country_code + digits[1:]
    elif not digits.startswith(country_code):
        digits = country_code + digits
    if digits.startswith("0") or not MIN_DIGITS <= len(digits) <= MAX_DIGITS:
        raise ValidationFailed("That is not a valid phone number")
    return "+" + digits


#: What WhatsApp and SMS can reach. Some countries (the United States, Canada) cannot tell a mobile from a landline by the number.
_MOBILE = {PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE}


def mobile_number(raw: str, region: str) -> str:
    """``raw`` as typed -> ``+<digits>`` when it is a mobile number; ``region`` (ISO code, ``MY``) reads a number typed without its country code.

    A number typed with ``+`` or ``00`` keeps its own country. The numbering plans describe the mobiles of every country in
    ``app.core.countries`` (a test checks it).
    """
    text = _SEPARATORS.sub("", raw.strip())
    if text.startswith("00"):
        text = "+" + text[2:]
    if not re.fullmatch(r"\+?[0-9]+", text):  # (the library would also read letters as digits and take "ext 5" as an extension)
        raise InvalidPhone("That is not a valid phone number")
    try:
        number = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        raise InvalidPhone("That is not a valid phone number") from None
    if not phonenumbers.is_valid_number(number):
        where = phonenumbers.region_code_for_country_code(number.country_code or 0)  # (the country the number was typed for: +44 is the UK whatever was chosen)
        if where not in COUNTRIES:
            where = region
        raise InvalidPhone(_not_valid(where, phonenumbers.country_code_for_region(where)))
    if phonenumbers.number_type(number) not in _MOBILE:
        raise InvalidPhone("That looks like a landline: enter a mobile number, because codes and updates are sent by WhatsApp or SMS")
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)


def _not_valid(region: str, calling_code: int) -> str:
    count = " or ".join(str(n) for n in mobile_lengths(region))
    example = mobile_example(region)
    such = f", such as {example}" if example else ""
    return f"That is not a mobile number in {COUNTRIES[region].name}: it has {count} digits after +{calling_code}{such}"


def mobile_lengths(region: str) -> list[int]:
    """How many digits a mobile number of the country has after the country code (Malaysia ``[9, 10]``); empty where unknown."""
    meta = phonenumbers.PhoneMetadata.metadata_for_region(region)
    return sorted(meta.mobile.possible_length) if meta is not None and meta.mobile is not None and meta.mobile.possible_length else []


def trunk_prefix(region: str) -> str | None:
    """What people in the country dial before a number at home and drop after the country code (Malaysia ``0``, Russia ``8``); ``None`` where nothing is dropped (Singapore)."""
    meta = phonenumbers.PhoneMetadata.metadata_for_region(region)
    return meta.national_prefix if meta is not None else None


def mobile_example(region: str) -> str | None:
    """A made-up mobile number of the country as people there write it (``012-345 6789``), for a form's placeholder."""
    number = phonenumbers.example_number_for_type(region, PhoneNumberType.MOBILE)
    return phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL) if number else None


def normalise_email(raw: str) -> str:
    """Emails are matched case-insensitively and without surrounding space."""
    return raw.strip().lower()
