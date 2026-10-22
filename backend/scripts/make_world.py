"""Writes ``app/core/world.py``: every country a business can sign up in, with its calling code, currency and time zones.

Run it again when the time-zone database or the phone codes change (rarely). It needs two libraries the service itself does not::

    pip install --target /tmp/worldlib phonenumbers babel tzdata
    PYTHONPATH=/tmp/worldlib python scripts/make_world.py      (from backend/)

Where each part comes from:

* **The countries and their time zones**: the IANA time-zone database (``tzdata``: ``iso3166.tab`` and ``zone.tab``), the same database
  ``zoneinfo`` reads when it works out a business's clock, so every zone listed is one the service can use. A country with no zone of its own
  (Bouvet Island) or no phone code (Antarctica, the uninhabited islands) is left out: nobody runs a business there.
* **Calling codes**: Google's libphonenumber (``phonenumbers``).
* **Names and currencies**: the Unicode CLDR (``babel``), in English, with the few fixes in ``NAMES`` and ``CURRENCIES``.
* **The default zone** is the first ``zone.tab`` lists for the country (usually the capital's), except where ``DEFAULT_ZONES`` says otherwise.

Malaysia, Singapore and Australia are written here too, but ``app/core/countries.py`` keeps its own hand-made entries for them (exact postcodes,
the list of states, the privacy law), which win.
"""
from __future__ import annotations

import datetime as dt
import os
import pathlib

import phonenumbers
import tzdata
from babel import Locale
from babel.numbers import get_territory_currencies

#: Shorter or plainer names than CLDR's.
NAMES = {
    "HK": "Hong Kong", "MO": "Macao", "CD": "Congo (Democratic Republic)", "CG": "Congo (Republic)", "PS": "Palestine",
}
#: Where CLDR lists more than one tender, the one prices are written in.
CURRENCIES = {"BT": "BTN", "LS": "LSL", "NA": "NAD", "PA": "USD"}
#: Where the first zone ``zone.tab`` gives is not where most people live.
DEFAULT_ZONES = {
    "BR": "America/Sao_Paulo", "CA": "America/Toronto", "RU": "Europe/Moscow", "UZ": "Asia/Tashkent", "UA": "Europe/Kyiv",
    "CD": "Africa/Kinshasa", "EC": "America/Guayaquil", "FM": "Pacific/Pohnpei", "KI": "Pacific/Tarawa", "PF": "Pacific/Tahiti",
}
TODAY = dt.date(2026, 10, 1)


def tab(name: str) -> list[list[str]]:
    path = pathlib.Path(os.path.dirname(tzdata.__file__), "zoneinfo", name)
    return [line.rstrip("\n").split("\t") for line in path.read_text(encoding="utf8").splitlines(keepends=True) if not line.startswith("#")]


def label(zone: str, comment: str) -> str:
    """What a person reads beside the zone: ``zone.tab``'s note (``Eastern (most areas)``), else the city the zone is named after."""
    return comment or zone.rsplit("/", 1)[-1].replace("_", " ")


def main() -> None:
    zones: dict[str, list[tuple[str, str]]] = {}
    for row in tab("zone.tab"):
        zones.setdefault(row[0], []).append((row[2], row[3] if len(row) > 3 else ""))
    english = Locale("en").territories
    rows = []
    for code, _ in sorted(tab("iso3166.tab")):
        calling = phonenumbers.country_code_for_region(code)
        currency = CURRENCIES.get(code) or next(iter(get_territory_currencies(code, TODAY, tender=True)), None)
        if not zones.get(code) or not calling or not currency:
            continue
        name = NAMES.get(code, english[code])
        listed = zones[code]
        first = DEFAULT_ZONES.get(code, listed[0][0])
        assert any(z == first for z, _ in listed), (code, first)
        ordered = sorted(listed, key=lambda z: z[0] != first)  # (the default first; the rest as zone.tab has them)
        choices = ((ordered[0][0], name),) if len(ordered) == 1 else tuple((z, label(z, note)) for z, note in ordered)
        rows.append((code, name, str(calling), currency, choices))
    out = pathlib.Path(__file__).resolve().parent.parent / "app" / "core" / "world.py"
    lines = [
        '"""Every country a business can sign up in: ``(code, name, calling code, currency, ((time zone, who it is for), ...))``, the default zone first.',
        "",
        "Written by ``scripts/make_world.py`` from the IANA time-zone database, libphonenumber and the Unicode CLDR: do not edit by hand, run it again.",
        '"""',
        "WORLD: tuple[tuple[str, str, str, str, tuple[tuple[str, str], ...]], ...] = (",
    ]
    lines += [f"    {row!r}," for row in rows]
    lines.append(")")
    out.write_text("\n".join(lines) + "\n", encoding="utf8")
    print(f"{len(rows)} countries -> {out}")


if __name__ == "__main__":
    main()
