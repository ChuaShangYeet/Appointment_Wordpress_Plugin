"""Every country, with Malaysia, Singapore and Australia in detail: what a country settles, and how a business is set up with one (migrations 0022, 0034)."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from app.core import countries
from app.core.countries import COUNTRIES, AddressProblem, check_address, find_state, format_address, normalise_postcode

MY, SG, AU = COUNTRIES["MY"], COUNTRIES["SG"], COUNTRIES["AU"]
US, GB, JP, DE, HK = COUNTRIES["US"], COUNTRIES["GB"], COUNTRIES["JP"], COUNTRIES["DE"], COUNTRIES["HK"]


# --------------------------------------------------------------------- the table itself
def test_the_three_known_in_detail_keep_their_own_rules() -> None:
    assert [c.code for c in countries.DETAILED] == ["MY", "SG", "AU"]
    assert [(c.calling_code, c.currency) for c in (MY, SG, AU)] == [("60", "MYR"), ("65", "SGD"), ("61", "AUD")]
    assert all(c.exact_address and c.privacy_law for c in (MY, SG, AU))
    assert [z for z, _ in MY.timezones] == ["Asia/Kuala_Lumpur"]  # (the hand-made entry, not the world's, which lists Kuching too)
    assert countries.BY_CALLING_CODE == {"60": MY, "65": SG, "61": AU}  # (only the three: 1 and 44 are more than one country)
    assert countries.get("SG") is SG
    with pytest.raises(KeyError):
        countries.get("XX")


def test_the_whole_world_is_there_with_its_phone_codes_and_currencies() -> None:
    assert len(COUNTRIES) > 230
    assert [(c.name, c.calling_code, c.currency) for c in (US, GB, JP, DE, HK)] == [
        ("United States", "1", "USD"), ("United Kingdom", "44", "GBP"), ("Japan", "81", "JPY"), ("Germany", "49", "EUR"), ("Hong Kong", "852", "HKD"),
    ]
    assert COUNTRIES["CA"].calling_code == "1" and COUNTRIES["BT"].currency == "BTN"
    assert "AQ" not in COUNTRIES and "BV" not in COUNTRIES  # (no phone code or no clock of its own: nobody runs a business there)
    for country in COUNTRIES.values():
        assert len(country.code) == 2 and country.code.isupper() and country.name
        assert country.calling_code.isdigit() and 1 <= len(country.calling_code) <= 3  # (tenants.phone_country_code holds three)
        assert len(country.currency) == 3 and country.currency.isupper()
    assert not US.exact_address and US.states == () and US.privacy_law is None and US.postcode_pattern is None


def test_the_default_zone_is_where_most_people_are() -> None:
    assert [COUNTRIES[c].default_timezone for c in ("US", "CA", "BR", "RU", "ID", "GB")] == [
        "America/New_York", "America/Toronto", "America/Sao_Paulo", "Europe/Moscow", "Asia/Jakarta", "Europe/London",
    ]
    assert len(US.timezones) > 20 and US.has_timezone("America/Los_Angeles") and not US.has_timezone("Asia/Kuala_Lumpur")
    assert GB.timezones == (("Europe/London", "United Kingdom"),)  # (one zone: named after the country)


def test_google_is_told_the_countrys_domain() -> None:
    assert (MY.region, US.region, GB.region, JP.region) == ("my", "us", "uk", "jp")


def test_every_listed_time_zone_exists_and_the_first_is_the_default() -> None:
    for country in COUNTRIES.values():
        assert country.default_timezone == country.timezones[0][0]
        for name, who in country.timezones:
            assert ZoneInfo(name) and who
            assert country.has_timezone(name)
    assert [z for z, _ in AU.timezones][:2] == ["Australia/Sydney", "Australia/Melbourne"]
    assert len({z for c in COUNTRIES.values() for z, _ in c.timezones}) == sum(len(c.timezones) for c in COUNTRIES.values())  # no zone in two countries


def test_a_zone_of_another_country_or_a_misspelt_one_is_not_a_choice() -> None:
    assert not MY.has_timezone("Asia/Singapore") and not SG.has_timezone("Asia/Kuala_Lumpur") and not AU.has_timezone("Asia/Kuala_Lumpur")
    assert not AU.has_timezone("australia/sydney") and not AU.has_timezone("Australia/Canberra")  # (Canberra keeps Sydney time: choose Sydney)


def test_the_clocks_of_each_country_change_when_they_should() -> None:
    import datetime as dt

    def offsets(zone: str) -> set[dt.timedelta | None]:
        z = ZoneInfo(zone)
        return {dt.datetime(2026, m, 1, 12, tzinfo=z).utcoffset() for m in (1, 7)}

    assert all(len(offsets(z)) == 1 for z, _ in MY.timezones + SG.timezones)
    changing = {z for z, _ in AU.timezones if len(offsets(z)) == 2}
    assert changing == {"Australia/Sydney", "Australia/Melbourne", "Australia/Adelaide", "Australia/Hobart", "Australia/Broken_Hill", "Australia/Lord_Howe"}


# --------------------------------------------------------------------- postcodes
@pytest.mark.parametrize(("country", "typed", "stored"), [
    (MY, "50450", "50450"), (MY, " 50 450 ", "50450"), (SG, "018956", "018956"), (AU, "2000", "2000"), (AU, "0800", "0800"),
])
def test_a_postcode_is_kept_without_its_spaces(country: countries.Country, typed: str, stored: str) -> None:
    assert normalise_postcode(country, typed) == stored


@pytest.mark.parametrize(("country", "typed"), [
    (MY, "5045"), (MY, "504500"), (MY, "5045a"), (MY, ""), (MY, "٥٠٤٥٠"), (SG, "50450"), (AU, "50450"), (AU, "200"), (AU, "NSW 2000"),
])
def test_a_postcode_of_the_wrong_shape_is_refused(country: countries.Country, typed: str) -> None:
    with pytest.raises(AddressProblem, match="postcode"):
        normalise_postcode(country, typed)


@pytest.mark.parametrize(("country", "typed", "stored"), [
    (GB, "sw1a  1aa", "SW1A 1AA"), (US, "10001", "10001"), (US, "10001-1234", "10001-1234"), (JP, "100-0001", "100-0001"), (DE, " 10115 ", "10115"),
    (COUNTRIES["CA"], "m5v 2t6", "M5V 2T6"),
])
def test_elsewhere_a_postcode_is_kept_in_capitals_with_single_spaces(country: countries.Country, typed: str, stored: str) -> None:
    assert normalise_postcode(country, typed) == stored


@pytest.mark.parametrize("typed", ["1", "12345678901", "SW1A_1AA", "-1234", "1234-", "١٢٣٤٥", ""])
def test_elsewhere_a_postcode_must_still_look_like_one(typed: str) -> None:
    with pytest.raises(AddressProblem, match="2 to 10 letters, digits, spaces or hyphens"):
        normalise_postcode(GB, typed)


def test_the_message_names_the_country_and_an_example() -> None:
    with pytest.raises(AddressProblem, match=r"Singapore postcode has 6 digits \(for example 018956\)"):
        normalise_postcode(SG, "123")


# --------------------------------------------------------------------- states
def test_a_state_is_found_by_code_or_name_in_any_case() -> None:
    assert find_state(MY, "SGR") == find_state(MY, "selangor") == find_state(MY, "  Sgr ") == "SGR"
    assert find_state(AU, "new south wales") == find_state(AU, "nsw") == "NSW"
    assert find_state(MY, "Negeri   Sembilan") == "NSN"


def test_a_state_of_another_country_is_refused_and_the_choices_are_listed() -> None:
    with pytest.raises(AddressProblem, match="not a state or territory of Australia.*NSW, VIC, QLD"):
        find_state(AU, "Selangor")
    with pytest.raises(AddressProblem):
        find_state(MY, "")


def test_all_sixteen_malaysian_and_eight_australian_states_are_there() -> None:
    assert len(MY.states) == 16 and len(AU.states) == 8 and SG.states == ()
    assert len({c for c, _ in MY.states}) == 16 and len({c for c, _ in AU.states}) == 8


# --------------------------------------------------------------------- an address as a whole
def addr(country: countries.Country, **parts: str | None) -> countries.BusinessAddress | None:
    base: dict[str, str | None] = {"line1": None, "line2": None, "city": None, "state": None, "postcode": None}
    return check_address(country, **{**base, **parts})


def test_no_address_at_all_is_not_an_error() -> None:
    assert addr(MY) is None and addr(SG, line1="  ", city="") is None


def test_a_malaysian_address() -> None:
    got = addr(MY, line1=" 12 Jalan Bukit ", line2="Taman Maju", city="Petaling Jaya", state="selangor", postcode="47 800")
    assert got == countries.BusinessAddress("12 Jalan Bukit", "Taman Maju", "Petaling Jaya", "SGR", "47800")
    assert format_address(MY, got) == "12 Jalan Bukit, Taman Maju, 47800 Petaling Jaya, Selangor"


def test_an_australian_address() -> None:
    got = addr(AU, line1="1 George St", city="Sydney", state="NSW", postcode="2000")
    assert got is not None and got.line2 is None
    assert format_address(AU, got) == "1 George St, Sydney NSW 2000"


def test_a_singapore_address_has_no_state_and_its_city_is_singapore() -> None:
    got = addr(SG, line1="1 Raffles Place", postcode="048616")
    assert got == countries.BusinessAddress("1 Raffles Place", None, "Singapore", None, "048616")
    assert format_address(SG, got) == "1 Raffles Place, Singapore 048616"
    assert addr(SG, line1="1 Raffles Place", city="Singapore", postcode="048616") == got


def test_elsewhere_the_street_and_city_are_enough() -> None:
    got = addr(JP, line1="1-1 Marunouchi", city="Chiyoda, Tokyo")
    assert got == countries.BusinessAddress("1-1 Marunouchi", None, "Chiyoda, Tokyo", None, None)
    assert format_address(JP, got) == "1-1 Marunouchi, Chiyoda, Tokyo"


def test_elsewhere_the_region_is_kept_as_written_and_goes_before_the_postcode() -> None:
    got = addr(COUNTRIES["CA"], line1="290 Bremner Blvd", city="Toronto", state="  Ontario ", postcode="m5v 3l9")
    assert got == countries.BusinessAddress("290 Bremner Blvd", None, "Toronto", "Ontario", "M5V 3L9")
    assert format_address(COUNTRIES["CA"], got) == "290 Bremner Blvd, Toronto, Ontario M5V 3L9"
    assert format_address(GB, addr(GB, line1="10 Downing St", city="London", postcode="SW1A 2AA")) == "10 Downing St, London, SW1A 2AA"


@pytest.mark.parametrize(("parts", "message"), [
    ({"city": "London", "postcode": "SW1A 2AA"}, "street"),
    ({"line1": "10 Downing St", "postcode": "SW1A 2AA"}, "city or town is needed"),
    ({"line1": "10 Downing St", "city": "London", "postcode": "SW1A_2AA"}, "2 to 10"),
])
def test_elsewhere_what_is_missing_is_said_plainly_too(parts: dict[str, str], message: str) -> None:
    with pytest.raises(AddressProblem, match=message):
        addr(GB, **parts)


@pytest.mark.parametrize(("country", "parts", "message"), [
    (MY, {"city": "Ipoh", "state": "PRK", "postcode": "30000"}, "street"),
    (MY, {"line1": "1 Jalan", "state": "PRK", "postcode": "30000"}, "city"),
    (MY, {"line1": "1 Jalan", "city": "Ipoh", "postcode": "30000"}, "state is needed"),
    (MY, {"line1": "1 Jalan", "city": "Ipoh", "state": "PRK"}, "postcode is needed"),
    (MY, {"line1": "1 Jalan", "city": "Ipoh", "state": "PRK", "postcode": "3000"}, "5 digits"),
    (MY, {"line1": "1 Jalan", "city": "Ipoh", "state": "NSW", "postcode": "30000"}, "not a state"),
    (AU, {"line1": "1 St", "city": "Perth", "state": "WA", "postcode": "60000"}, "4 digits"),
    (SG, {"line1": "1 Raffles Place", "state": "Central", "postcode": "048616"}, "no states"),
    (SG, {"line1": "1 Raffles Place", "postcode": "48616"}, "6 digits"),
    (SG, {"postcode": "048616"}, "street"),
])
def test_what_is_missing_or_does_not_fit_is_said_plainly(country: countries.Country, parts: dict[str, str], message: str) -> None:
    with pytest.raises(AddressProblem, match=message):
        addr(country, **parts)
