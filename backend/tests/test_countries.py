"""Malaysia, Singapore and Australia: what a country settles, and how a business is set up with one (migration 0022)."""
from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from app.core import countries
from app.core.countries import COUNTRIES, AddressProblem, check_address, find_state, format_address, normalise_postcode

MY, SG, AU = COUNTRIES["MY"], COUNTRIES["SG"], COUNTRIES["AU"]


# --------------------------------------------------------------------- the table itself
def test_the_three_countries_are_the_ones_the_service_is_built_for() -> None:
    assert sorted(COUNTRIES) == ["AU", "MY", "SG"]
    assert [(c.calling_code, c.currency) for c in (MY, SG, AU)] == [("60", "MYR"), ("65", "SGD"), ("61", "AUD")]
    assert countries.BY_CALLING_CODE == {"60": MY, "65": SG, "61": AU}
    assert countries.get("SG") is SG
    with pytest.raises(KeyError):
        countries.get("NZ")


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
