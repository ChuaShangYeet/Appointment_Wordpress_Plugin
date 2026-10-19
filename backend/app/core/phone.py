"""Phone-number normalisation: one customer, one spelling.

Customers are keyed by ``(tenant, phone)``, so ``+60 12-345 0001``, ``012-3450001`` and ``0060123450001`` must all
reach the same row. Numbers are stored as ``+<country code><number>`` with digits only.

This is a deliberately small normaliser, not a validator of national numbering plans (that needs a library such as
``phonenumbers``): it fixes the spelling, and rejects what cannot be a phone number at all.
"""
from __future__ import annotations

import re

from app.core.exceptions import ValidationFailed

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


def normalise_email(raw: str) -> str:
    """Emails are matched case-insensitively and without surrounding space."""
    return raw.strip().lower()
