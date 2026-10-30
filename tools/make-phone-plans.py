"""Write the booking plugin's phone numbering plans from Google's libphonenumber data (the Python ``phonenumbers`` package).

The plugin checks that a customer's number is a mobile of the chosen country, as the FastAPI backend does with the same
library (``app/core/phone.py`` in the backend project). PHP has no such library in WordPress, and bundling one adds several
megabytes and can clash with another plugin that bundles a different version. So only what the check needs is written out:
for every country, its calling code, how a national number drops its trunk prefix, and the patterns of its mobiles and of
every other kind of number.

    python tools/make-phone-plans.py

writes three files (do not edit them by hand, run this again after ``pip install -U phonenumbers``):

* ``justsimple-booking/src/Booking/phone-plans.php``: the plans, read by ``Phone``.
* ``justsimple-booking/assets/front/phone-countries.json``: the booking form's country-code list (name, code, lengths, example).
* ``justsimple-booking/tests/phone-cases.json``: numbers in many spellings with the answer the backend's check gives,
  for ``tests/phone.php`` to prove the PHP check gives the same (the answers as short codes, see ``backend_check``).

Needs ``phonenumbers`` and ``babel`` (country names in English from the Unicode CLDR, as the backend's ``make_world.py``).
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import phonenumbers
from babel import Locale
from phonenumbers import PhoneMetadata, PhoneNumberFormat, PhoneNumberType

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "justsimple-booking"
NAMES = Locale("en").territories

# every kind of number besides mobiles and landlines, in the order libphonenumber tries them
OTHER = ("premium_rate", "toll_free", "shared_cost", "voip", "personal_number", "pager", "uan", "voicemail")

COUNTRIES = {r: NAMES[r] for r in sorted(phonenumbers.SUPPORTED_REGIONS) if r in NAMES}


def desc(d) -> list | None:
    """``[pattern, [lengths]]`` of a kind of number; ``None`` when the country has none of that kind."""
    if d is None or d.national_number_pattern is None:
        return None
    return [d.national_number_pattern, [n for n in d.possible_length]]


def plan(meta: PhoneMetadata) -> dict:
    g = meta.general_desc
    out = {
        "c": str(meta.country_code),
        "g": [g.national_number_pattern, list(g.possible_length), list(g.possible_length_local_only)],
        "f": desc(meta.fixed_line),
        "m": desc(meta.mobile),
        "o": [x for x in (desc(getattr(meta, k)) for k in OTHER) if x],
    }
    if meta.same_mobile_and_fixed_line_pattern:
        out["s"] = 1
    if meta.national_prefix_for_parsing:
        out["pp"] = meta.national_prefix_for_parsing
    if meta.national_prefix_transform_rule:
        out["pt"] = meta.national_prefix_transform_rule
    if meta.international_prefix:
        out["ip"] = meta.international_prefix
    if meta.leading_digits:
        out["ld"] = meta.leading_digits
    return out


def php(value) -> str:
    """A PHP literal: strings single-quoted (only \\ and ' escaped), lists as array(...)."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(value, list):
        return "array(" + ",".join(php(v) for v in value) + ")"
    if isinstance(value, dict):
        return "array(" + ",".join(f"{php(k)}=>{php(v)}" for k, v in value.items()) + ")"
    raise TypeError(value)


def mobile_lengths(region: str) -> list[int]:
    meta = PhoneMetadata.metadata_for_region(region)
    return sorted(meta.mobile.possible_length) if meta is not None and meta.mobile is not None and meta.mobile.possible_length else []


def mobile_example(region: str) -> str | None:
    number = phonenumbers.example_number_for_type(region, PhoneNumberType.MOBILE)
    return phonenumbers.format_number(number, PhoneNumberFormat.NATIONAL) if number else None


# --------------------------------------------------------------------- the backend's check, word for word, for the cases
_SEPARATORS = re.compile(r"[\s\-().]")
_MOBILE = {PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE}


def backend_check(raw: str, region: str) -> str:
    """``app.core.phone.mobile_number`` of the backend, its answer as a short code: ``+<digits>``; ``L`` a landline; ``G`` not a
    phone number at all; ``N <country>`` not a mobile of that country (the message names it, its lengths and an example)."""
    text = _SEPARATORS.sub("", raw.strip())
    if text.startswith("00"):
        text = "+" + text[2:]
    if not re.fullmatch(r"\+?[0-9]+", text):
        return "G"
    try:
        number = phonenumbers.parse(text, region)
    except phonenumbers.NumberParseException:
        return "G"
    if not phonenumbers.is_valid_number(number):
        where = phonenumbers.region_code_for_country_code(number.country_code or 0)
        if where not in COUNTRIES:
            where = region
        return "N " + where
    if phonenumbers.number_type(number) not in _MOBILE:
        return "L"
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)


def cases() -> list[list[str]]:
    """Every kind of example number of every country, typed the ways people type them, and some a digit short or long."""
    out: list[list[str]] = []
    seen: set[tuple[str, str]] = set()

    def add(raw: str, region: str) -> None:
        if (raw, region) not in seen:
            seen.add((raw, region))
            out.append([raw, region, backend_check(raw, region)])

    kinds = [PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE, PhoneNumberType.TOLL_FREE, PhoneNumberType.PREMIUM_RATE,
             PhoneNumberType.SHARED_COST, PhoneNumberType.VOIP, PhoneNumberType.PERSONAL_NUMBER, PhoneNumberType.PAGER,
             PhoneNumberType.UAN, PhoneNumberType.VOICEMAIL]
    for region in COUNTRIES:
        cc = phonenumbers.country_code_for_region(region)
        for kind in kinds:
            n = phonenumbers.example_number_for_type(region, kind)
            if n is None:
                continue
            e164 = phonenumbers.format_number(n, PhoneNumberFormat.E164)
            national = phonenumbers.format_number(n, PhoneNumberFormat.NATIONAL)
            digits = re.sub(r"\D", "", national)
            nsn = phonenumbers.national_significant_number(n)
            for raw, typed_in in (
                (e164, "MY"), (phonenumbers.format_number(n, PhoneNumberFormat.INTERNATIONAL), region), ("00" + e164[1:], region),
                (national, region), (digits, region), (nsn, region), (str(cc) + nsn, region),
                (e164[:-1], region), (e164 + "5", region), (digits[:-1], region), (digits + "5", region),
            ):
                add(raw, typed_in)
    for raw, region in (("+800 12", "MY"), ("+800 1234 5678", "MY"), ("+881 6 1234 5678", "SG"), ("+999 123 4567", "MY"), ("12ab345", "MY"),
                        ("", "MY"), ("+", "MY"), ("0", "MY"), ("00", "MY"), ("0060 12 345 6789", "US"), ("011 60 12 345 6789", "US"),
                        ("(201) 555-0123", "US"), ("+1 204 234 5678", "US"), ("204 234 5678", "US"), ("+1 284 345 6789", "MY"),
                        ("07400 123456", "GB"), ("+44 7400 12", "MY"), ("011 15 2345 6789", "AR"), ("+54 9 11 2345 6789", "AR"),
                        ("15 2345 6789", "AR"), ("312 345 6789", "IT"), ("06 1234 5678", "IT"), ("8 912 345 67 89", "RU"),
                        ("+7 912 345 67 89", "KZ"), ("1234567890123456789", "MY")):
        add(raw, region)
    return out


def main() -> None:
    plans = {region: plan(PhoneMetadata.metadata_for_region(region)) for region in COUNTRIES}
    for region, p in plans.items():
        p["n"] = COUNTRIES[region]
        p["l"] = mobile_lengths(region)
        example = mobile_example(region)
        if example:
            p["e"] = example
    nongeo = {}
    calling: dict[str, list[str]] = {}
    for code, regions in sorted(phonenumbers.COUNTRY_CODE_TO_REGION_CODE.items()):
        if regions == ("001",):
            meta = PhoneMetadata.metadata_for_nongeo_region(code)
            if meta is not None:
                nongeo[str(code)] = plan(meta)
                calling[str(code)] = ["001"]
            continue
        kept = [r for r in regions if r in plans]
        if kept:
            calling[str(code)] = kept

    out = PLUGIN / "src" / "Booking" / "phone-plans.php"
    out.write_text(
        "<?php\n// Written by tools/make-phone-plans.py from libphonenumber " + phonenumbers.__version__
        + " (Google's numbering plans) and CLDR country names: do not edit by hand, run it again.\n"
        "// Keys: n name, c calling code, g general [pattern, lengths, local-only lengths], f landlines, m mobiles, o other kinds\n"
        "// ([pattern, lengths] each), s mobiles and landlines share one pattern, pp/pt the trunk prefix and its rewrite, ip the\n"
        "// international prefix, ld leading digits (countries sharing a calling code), l mobile lengths, e a mobile as written there.\n"
        "defined( 'ABSPATH' ) || exit;\n"
        "return " + php({"regions": plans, "nongeo": nongeo, "calling": calling}) + ";\n",
        encoding="utf-8", newline="\n",
    )
    picker = [{"code": r, "name": p["n"], "calling": p["c"], "lengths": p["l"], "example": p.get("e"), "trunk": PhoneMetadata.metadata_for_region(r).national_prefix}
              for r, p in sorted(plans.items(), key=lambda kv: kv[1]["n"])]
    (PLUGIN / "assets" / "front" / "phone-countries.json").write_text(json.dumps(picker, ensure_ascii=False, separators=(",", ":")), encoding="utf-8", newline="\n")
    all_cases = cases()
    (PLUGIN / "tests" / "phone-cases.json").write_text(json.dumps({"library": phonenumbers.__version__, "cases": all_cases}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8", newline="\n")
    print(f"{len(plans)} countries, {len(nongeo)} worldwide codes, {len(all_cases)} cases; libphonenumber {phonenumbers.__version__}")


if __name__ == "__main__":
    main()
