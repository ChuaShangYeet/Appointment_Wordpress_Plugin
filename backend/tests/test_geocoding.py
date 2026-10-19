"""Looking up typed addresses (blueprint §11.2, §15): the provider, bookings and saved addresses with no map pin, the business's own location."""
from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core import ratelimit
from app.core.config import Settings
from app.core.database import tenant_session
from app.core.exceptions import AddressNotFound, AddressNotPrecise, GeocodingFailed, GeocodingUnavailable
from app.models import Booking, CustomerAddress, Tenant
from app.schemas.public import AddressIn, AddressUpdate
from app.services import geocoding
from app.services.geocoding import GoogleGeocoder, Location, address_text
from tests.conftest import Seed, at
from tests.test_customer_cancel import login, otp  # noqa: F401  (``otp`` is a fixture)
from tests.test_field_service import KL, NEAR, SEREMBAN, Practice, make_practice, pbook, pin, practice  # noqa: F401  (``practice`` is a fixture)
from tests.test_ratelimit import clock, limited, redis  # noqa: F401  (fixtures)

KEY = "AIza-secret-maps-key-0123456789"
HOME = "12 Jalan Bukit, 50450 Kuala Lumpur"


def found(lat: float, lng: float, kind: str = "ROOFTOP", *, partial: bool = False, formatted: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"formatted_address": formatted or f"{lat:.4f},{lng:.4f}, Malaysia", "geometry": {"location": {"lat": lat, "lng": lng}, "location_type": kind}}
    if partial:
        result["partial_match"] = True
    return {"status": "OK", "results": [result]}


class FakeMaps:
    """A fake Geocoding API: answers by the address it is asked for (``ZERO_RESULTS`` for anything it does not know); remembers every request."""

    def __init__(self) -> None:
        self.places: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []
        self.failure: Any = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.failure, Exception):
            raise self.failure
        if isinstance(self.failure, httpx.Response):
            return self.failure
        return httpx.Response(200, json=self.places.get(request.url.params["address"], {"status": "ZERO_RESULTS", "results": []}))

    def provider(self, **kw: Any) -> GoogleGeocoder:
        return GoogleGeocoder(KEY, transport=httpx.MockTransport(self.handler), **kw)

    def asked(self) -> list[str]:
        return [r.url.params["address"] for r in self.requests]


@pytest.fixture
def maps(monkeypatch: pytest.MonkeyPatch) -> FakeMaps:
    fake = FakeMaps()
    monkeypatch.setattr(geocoding, "_provider", fake.provider())
    return fake


# --------------------------------------------------------------------- the provider
async def test_the_lookup_is_one_get_with_the_address_the_key_and_a_region() -> None:
    fake = FakeMaps()
    fake.places["12 Jalan Bukit"] = found(3.1, 101.7, formatted="12, Jalan Bukit, Kuala Lumpur")
    result = await fake.provider().geocode("12 Jalan Bukit", "my")
    assert result == Location(3.1, 101.7, "12, Jalan Bukit, Kuala Lumpur", exact=True)
    (request,) = fake.requests
    assert request.method == "GET" and request.url.path == "/maps/api/geocode/json" and request.url.host == "maps.googleapis.com"
    assert dict(request.url.params) == {"address": "12 Jalan Bukit", "key": KEY, "region": "my"}
    other = FakeMaps()
    await other.provider().geocode("x")
    assert "region" not in other.requests[0].url.params  # no bias when the lookup names no country
    custom = FakeMaps()
    await GoogleGeocoder(KEY, base_url="https://maps.example.test/", transport=httpx.MockTransport(custom.handler)).geocode("x", "sg")
    assert str(custom.requests[0].url).startswith("https://maps.example.test/maps/api/geocode/json?") and custom.requests[0].url.params["region"] == "sg"


@pytest.mark.parametrize(("kind", "partial", "exact"), [
    ("ROOFTOP", False, True), ("RANGE_INTERPOLATED", False, True), ("GEOMETRIC_CENTER", False, True),
    ("APPROXIMATE", False, False), ("ROOFTOP", True, False), ("", False, False), ("SOMETHING_NEW", False, False),
])
async def test_only_a_point_on_the_ground_with_a_full_match_counts_as_exact(kind: str, partial: bool, exact: bool) -> None:
    fake = FakeMaps()
    fake.places["a"] = found(3.1, 101.7, kind, partial=partial)
    result = await fake.provider().geocode("a")
    assert result is not None and result.exact is exact


async def test_a_result_without_a_location_type_is_not_exact() -> None:
    fake = FakeMaps()
    fake.places["a"] = {"status": "OK", "results": [{"formatted_address": "x", "geometry": {"location": {"lat": 3.1, "lng": 101.7}}}]}
    result = await fake.provider().geocode("a")
    assert result is not None and result.exact is False and result.formatted_address == "x"
    fake.places["b"] = {"status": "OK", "results": [{"geometry": {"location": {"lat": "3.5", "lng": "101.5"}, "location_type": "ROOFTOP"}}]}
    parsed = await fake.provider().geocode("b")
    assert parsed is not None and (parsed.latitude, parsed.longitude, parsed.formatted_address) == (3.5, 101.5, "")


async def test_the_first_result_is_the_one_used() -> None:
    fake = FakeMaps()
    fake.places["a"] = {"status": "OK", "results": [found(1.0, 2.0)["results"][0], found(3.0, 4.0)["results"][0]]}
    result = await fake.provider().geocode("a")
    assert result is not None and (result.latitude, result.longitude) == (1.0, 2.0)


async def test_nothing_found_is_none_not_an_error() -> None:
    assert await FakeMaps().provider().geocode("nowhere") is None


@pytest.mark.parametrize("status", ["OVER_QUERY_LIMIT", "REQUEST_DENIED", "INVALID_REQUEST", "UNKNOWN_ERROR", "OVER_DAILY_LIMIT", ""])
async def test_a_refusal_by_the_provider_is_a_failure_that_names_nothing_private(status: str, caplog: pytest.LogCaptureFixture) -> None:
    fake = FakeMaps()
    fake.failure = httpx.Response(200, json={"status": status, "error_message": "The provided API key is invalid. " + "x" * 400, "results": []})
    with caplog.at_level(logging.WARNING, logger="app.services.geocoding"), pytest.raises(GeocodingFailed) as caught:
        await fake.provider().geocode("12 Jalan Rahsia")
    assert caught.value.status_code == 503 and caught.value.code == "geocoding_failed"
    assert KEY not in caught.value.message and "Rahsia" not in caught.value.message and (not status or status not in caught.value.message)
    log = caplog.text
    assert status in log and "provided API key is invalid" in log  # the operator can see why...
    assert KEY not in log and "Rahsia" not in log and "x" * 250 not in log  # ...without the key, the address, or a wall of text


@pytest.mark.parametrize("answer", [
    httpx.Response(500, text="oops"), httpx.Response(403, json={"status": "OK"}), httpx.Response(503, json=found(3.1, 101.7)), httpx.Response(429, json=found(3.1, 101.7)), httpx.Response(200, text="not json"), httpx.Response(200, json=[1, 2]),
    httpx.Response(200, json={}), httpx.Response(200, json={"status": "OK", "results": []}), httpx.Response(200, json={"status": "OK"}),
    httpx.Response(200, json={"status": "OK", "results": [{"geometry": {}}]}),
    httpx.Response(200, json={"status": "OK", "results": [{"geometry": {"location": {"lat": "north", "lng": 1}}}]}),
    httpx.Response(200, json={"status": "OK", "results": [{"geometry": {"location": {"lat": 91, "lng": 1}}}]}),
    httpx.Response(200, json={"status": "OK", "results": [{"geometry": {"location": {"lat": 1, "lng": -181}}}]}),
    httpx.Response(200, content=b'{"status": "OK", "results": [{"geometry": {"location": {"lat": NaN, "lng": 1}}}]}', headers={"content-type": "application/json"}),
])
async def test_an_unusable_answer_is_a_failure(answer: httpx.Response) -> None:
    fake = FakeMaps()
    fake.failure = answer
    with pytest.raises(GeocodingFailed):
        await fake.provider().geocode("a")


async def test_a_network_failure_reports_only_its_type(caplog: pytest.LogCaptureFixture) -> None:
    for failure in (httpx.ConnectError(f"cannot reach https://maps.googleapis.com/?key={KEY}&address=12 Jalan Rahsia"), httpx.ReadTimeout("slow")):
        fake = FakeMaps()
        fake.failure = failure
        with caplog.at_level(logging.WARNING, logger="app.services.geocoding"), pytest.raises(GeocodingFailed):
            await fake.provider().geocode("12 Jalan Rahsia")
        assert type(failure).__name__ in caplog.text and KEY not in caplog.text and "Rahsia" not in caplog.text
        caplog.clear()


async def test_the_timeout_is_passed_to_the_http_client() -> None:
    fake = FakeMaps()
    await fake.provider(timeout=2.5).geocode("a")
    await fake.provider().geocode("a")
    assert [r.extensions["timeout"]["read"] for r in fake.requests] == [2.5, 5.0]


# --------------------------------------------------------------------- the module
def test_the_address_is_written_as_one_line() -> None:
    assert address_text("12 Jalan Bukit") == "12 Jalan Bukit"
    assert address_text("12 Jalan Bukit", "Kuala Lumpur") == "12 Jalan Bukit, Kuala Lumpur"
    assert address_text("12 Jalan Bukit", None, "50450") == "12 Jalan Bukit, 50450"
    assert address_text("12 Jalan Bukit", "Kuala Lumpur", "50450") == "12 Jalan Bukit, 50450 Kuala Lumpur"
    assert address_text("  12 Jalan Bukit  ", "  ", "") == "12 Jalan Bukit"
    assert address_text("  12 Jalan Bukit  ", "KL") == "12 Jalan Bukit, KL"  # the line is trimmed before it is joined to the area
    assert address_text("12 Jalan\nBukit\t1", "KL") == "12 Jalan Bukit 1, KL"


async def test_looking_up_needs_a_provider_and_a_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geocoding, "_provider", None)
    with pytest.raises(GeocodingUnavailable) as none:
        await geocoding.locate("12 Jalan Bukit")
    assert none.value.status_code == 422 and none.value.code == "geocoding_unavailable" and "latitude and longitude" in none.value.message
    fake = FakeMaps()
    monkeypatch.setattr(geocoding, "_provider", fake.provider())
    with pytest.raises(AddressNotFound) as missing:
        await geocoding.locate("12 Jalan Bukit")
    assert missing.value.status_code == 422 and missing.value.code == "address_not_found"


async def test_a_vague_match_is_returned_by_locate_and_refused_by_locate_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMaps()
    fake.places["Taman Desa"] = found(3.1, 101.7, "APPROXIMATE", formatted="Taman Desa, Kuala Lumpur")
    fake.places["12 Jalan Bukit"] = found(3.2, 101.6)
    monkeypatch.setattr(geocoding, "_provider", fake.provider())
    vague = await geocoding.locate("Taman Desa")
    assert vague.exact is False
    with pytest.raises(AddressNotPrecise) as refused:
        await geocoding.locate_exact("Taman Desa")
    assert refused.value.status_code == 422 and refused.value.code == "address_not_precise" and refused.value.extra == {"formatted_address": "Taman Desa, Kuala Lumpur"}
    assert (await geocoding.locate_exact("12 Jalan Bukit")).exact is True


async def test_the_provider_is_sent_clean_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeMaps()
    monkeypatch.setattr(geocoding, "_provider", fake.provider())
    with pytest.raises(AddressNotFound):
        await geocoding.locate("  12 Jalan\r\nBukit\x00  ")
    assert fake.asked() == ["12 Jalan Bukit"]


def test_the_provider_is_configured_from_the_settings(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geocoding, "_provider", None)
    geocoding.configure(Settings(_env_file=None, geocoding_provider="google", google_maps_api_key=KEY, geocoding_timeout_seconds=3.0,
                                 geocoding_base_url="https://maps.example.test"))  # type: ignore[call-arg]
    provider = geocoding.get_provider()
    assert isinstance(provider, GoogleGeocoder) and provider._key == KEY
    assert (provider.timeout, provider.base_url) == (3.0, "https://maps.example.test")
    with caplog.at_level(logging.WARNING, logger="app.services.geocoding"):
        geocoding.configure(Settings(_env_file=None, geocoding_provider="google"))  # type: ignore[call-arg]
    assert geocoding.get_provider() is None and "GOOGLE_MAPS_API_KEY" in caplog.text  # a half-configured provider does not linger
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="app.services.geocoding"):
        geocoding.configure(Settings(_env_file=None, environment="development"))  # type: ignore[call-arg]
        geocoding.configure(Settings(_env_file=None, environment="test"))  # type: ignore[call-arg]
    assert caplog.records == []
    with caplog.at_level(logging.WARNING, logger="app.services.geocoding"):
        geocoding.configure(Settings(_env_file=None, environment="production"))  # type: ignore[call-arg]
    assert geocoding.get_provider() is None and "no geocoding provider" in caplog.text
    assert KEY not in repr(Settings(_env_file=None, google_maps_api_key=KEY))  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        Settings(_env_file=None, geocoding_provider="bing")  # type: ignore[call-arg]


def test_a_pin_has_both_coordinates_or_neither() -> None:
    assert AddressIn(line1="x").latitude is None and AddressIn(line1="x", latitude=3.1, longitude=101.7).longitude == 101.7
    for bad in ({"latitude": 3.1}, {"longitude": 101.7}):
        with pytest.raises(ValidationError, match="both latitude and longitude"):
            AddressIn(line1="x", **bad)
    for bad in ({"latitude": 3.1}, {"longitude": 101.7}):
        with pytest.raises(ValidationError, match="both latitude and longitude"):
            AddressUpdate(**bad)
    assert AddressUpdate(latitude=3.1, longitude=101.7).latitude == 3.1 and AddressUpdate(line1="y").line1 == "y"


def test_geocoding_is_its_own_rate_limit_bucket() -> None:
    assert ratelimit.classify("POST", "/v1/public/geocode") == "geocode" and ratelimit.classify("POST", "/v1/public/geocode/") == "geocode"
    assert ratelimit.classify("POST", "/v1/public/geocodes") == "public_write"
    assert ratelimit.DEFAULTS["geocode"]["ip"].requests <= 10 and ratelimit.DEFAULTS["geocode"]["tenant"].requests < ratelimit.DEFAULTS["public_write"]["tenant"].requests


# --------------------------------------------------------------------- POST /public/geocode
async def geocode(s: Seed, address: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return await s.client.post("/v1/public/geocode", json={"address": address}, headers=s.pk if headers is None else headers)


async def test_the_page_can_look_an_address_up_and_see_whether_it_is_in_the_service_area(practice: Practice, maps: FakeMaps, postgis: None) -> None:
    maps.places[HOME] = found(*NEAR, formatted="12, Jalan Bukit, 50450 Kuala Lumpur")
    maps.places["Seremban town"] = found(*SEREMBAN, "RANGE_INTERPOLATED")
    r = await geocode(practice, HOME)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["latitude"], body["longitude"], body["formatted_address"], body["exact"], body["within_service_area"]) == (NEAR[0], NEAR[1], "12, Jalan Bukit, 50450 Kuala Lumpur", True, True)
    assert 1 < body["distance_km"] < 5  # about 3 km from the business
    assert body["distance_km"] == round(body["distance_km"], 2)  # two decimals, not a long float
    far = (await geocode(practice, "Seremban town")).json()
    assert far["within_service_area"] is False and far["distance_km"] > 25 and far["exact"] is True
    assert maps.asked() == [HOME, "Seremban town"]
    async with tenant_session(practice.tenant_id) as db:
        assert await db.scalar(select(func.count()).select_from(CustomerAddress)) == 0  # looking up stores nothing


async def test_a_vague_result_is_shown_but_flagged(practice: Practice, maps: FakeMaps, postgis: None) -> None:
    maps.places["Taman Desa"] = found(*NEAR, "APPROXIMATE", formatted="Taman Desa, Kuala Lumpur")
    maps.places["Jalan Bukit"] = found(*NEAR, partial=True)
    for text in ("Taman Desa", "Jalan Bukit"):
        r = await geocode(practice, text)
        assert r.status_code == 200 and r.json()["exact"] is False and r.json()["within_service_area"] is True


async def test_without_a_service_area_there_is_nothing_to_compare(client: httpx.AsyncClient, maps: FakeMaps) -> None:
    bare = await make_practice(client, radius=None)
    maps.places[HOME] = found(*NEAR)
    body = (await geocode(bare, HOME)).json()
    assert body["within_service_area"] is None and body["distance_km"] is None and body["latitude"] == NEAR[0]


async def test_only_businesses_that_go_to_the_customer_look_addresses_up(seeded: Seed, maps: FakeMaps) -> None:
    maps.places[HOME] = found(*NEAR)
    r = await geocode(seeded, HOME)
    assert r.status_code == 422 and "does not go to the customer" in r.json()["detail"]
    assert maps.requests == []  # and the paid lookup was never made


async def test_the_lookup_says_what_went_wrong(practice: Practice, maps: FakeMaps, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = await geocode(practice, "Nowhere Road 99")
    assert missing.status_code == 422 and missing.json()["error"] == "address_not_found"
    maps.failure = httpx.ConnectError(f"boom {KEY}")
    down = await geocode(practice, HOME)
    assert down.status_code == 503 and down.json()["error"] == "geocoding_failed" and KEY not in down.text and "boom" not in down.text
    monkeypatch.setattr(geocoding, "_provider", None)
    none = await geocode(practice, HOME)
    assert none.status_code == 422 and none.json()["error"] == "geocoding_unavailable"


async def test_the_lookup_checks_its_input_and_its_caller(practice: Practice, maps: FakeMaps) -> None:
    for bad in ("ab", "x" * 301, ""):
        assert (await geocode(practice, bad)).status_code == 422, bad
    assert (await practice.client.post("/v1/public/geocode", json={"address": HOME, "extra": 1}, headers=practice.pk)).status_code == 422
    assert (await practice.client.post("/v1/public/geocode", json={}, headers=practice.pk)).status_code == 422
    assert (await geocode(practice, HOME, headers={})).status_code == 401
    assert maps.requests == []
    maps.places["12 Jalan Bukit"] = found(*NEAR)
    assert (await geocode(practice, "  12 Jalan\r\nBukit\x00 ")).status_code == 200 and maps.asked() == ["12 Jalan Bukit"]


async def test_lookups_are_limited_tightly_without_touching_other_calls(practice: Practice, maps: FakeMaps, limited: Any) -> None:
    maps.places[HOME] = found(*NEAR)
    codes = [(await geocode(practice, HOME)).status_code for _ in range(12)]
    assert codes == [200] * 10 + [429] * 2  # ten a minute per address
    assert len(maps.requests) == 10  # a refused call never reaches the provider
    assert (await practice.client.get("/v1/public/services", headers=practice.pk)).status_code == 200  # other calls have their own allowance


# --------------------------------------------------------------------- bookings with a typed address
def typed(line1: str = "12 Jalan Bukit", city: str | None = "Kuala Lumpur", postcode: str | None = "50450") -> dict[str, Any]:
    return {k: v for k, v in {"line1": line1, "city": city, "postcode": postcode}.items() if v is not None}


async def addresses(s: Seed) -> list[tuple[str, float | None, float | None]]:
    async with tenant_session(s.tenant_id) as db:
        return [(a.line1, a.latitude, a.longitude) for a in (await db.scalars(select(CustomerAddress).order_by(CustomerAddress.created_at))).all()]


async def booking_count(s: Seed) -> int:
    async with tenant_session(s.tenant_id) as db:
        return await db.scalar(select(func.count()).select_from(Booking)) or 0


async def test_an_on_site_booking_with_a_typed_address_is_placed_by_the_server(practice: Practice, maps: FakeMaps, postgis: None) -> None:
    maps.places[HOME] = found(*NEAR)
    r = await pbook(practice, at(practice.day, 10), "site", address=typed())
    assert r.status_code == 201, r.text
    assert maps.asked() == [HOME]  # "line1, postcode city"
    assert await addresses(practice) == [("12 Jalan Bukit", NEAR[0], NEAR[1])]
    assert r.json()["resource_ids"] and (await practice.client.get("/v1/admin/bookings", headers=practice.admin)).json()[0]["address_id"]


async def test_the_radius_check_uses_the_looked_up_point(practice: Practice, maps: FakeMaps, postgis: None) -> None:
    maps.places[HOME] = found(*SEREMBAN)
    r = await pbook(practice, at(practice.day, 10), "site", address=typed())
    assert r.status_code == 422 and r.json()["error"] == "outside_service_area" and r.json()["distance_km"] > 25
    assert await booking_count(practice) == 0 and await addresses(practice) == []  # nothing was kept


@pytest.mark.parametrize(("place", "code", "status"), [
    (None, "address_not_found", 422),
    (found(*NEAR, "APPROXIMATE"), "address_not_precise", 422),
    (found(*NEAR, partial=True), "address_not_precise", 422),
])
async def test_an_address_that_cannot_be_placed_precisely_makes_no_booking(practice: Practice, maps: FakeMaps, postgis: None, place: Any, code: str, status: int) -> None:
    if place is not None:
        maps.places[HOME] = place
    r = await pbook(practice, at(practice.day, 10), "site", address=typed())
    assert r.status_code == status and r.json()["error"] == code
    assert await booking_count(practice) == 0 and await addresses(practice) == []


async def test_a_failing_lookup_makes_no_booking(practice: Practice, maps: FakeMaps, monkeypatch: pytest.MonkeyPatch, postgis: None) -> None:
    maps.failure = httpx.ReadTimeout("slow")
    r = await pbook(practice, at(practice.day, 10), "site", address=typed())
    assert r.status_code == 503 and r.json()["error"] == "geocoding_failed"
    monkeypatch.setattr(geocoding, "_provider", None)
    r = await pbook(practice, at(practice.day, 10), "site", address=typed())
    assert r.status_code == 422 and r.json()["error"] == "geocoding_unavailable"
    assert await booking_count(practice) == 0 and await addresses(practice) == []


async def test_a_pin_is_used_as_it_is_and_nothing_is_looked_up(practice: Practice, maps: FakeMaps, postgis: None) -> None:
    r = await pbook(practice, at(practice.day, 10), "site", address={**typed(), "latitude": NEAR[0], "longitude": NEAR[1]})
    assert r.status_code == 201, r.text
    assert maps.requests == [] and await addresses(practice) == [("12 Jalan Bukit", NEAR[0], NEAR[1])]
    r = await pbook(practice, at(practice.day, 15), "site", address=pin(NEAR))  # (12:00 would run into the professional's break with the travel time)
    assert r.status_code == 201 and maps.requests == []


async def test_half_a_pin_is_refused(practice: Practice, maps: FakeMaps, postgis: None) -> None:
    for half in ({"latitude": NEAR[0]}, {"longitude": NEAR[1]}):
        r = await pbook(practice, at(practice.day, 10), "site", address={**typed(), **half})
        assert r.status_code == 422, half
    assert maps.requests == [] and await booking_count(practice) == 0


async def test_a_typed_address_is_not_looked_up_for_an_office_visit(practice: Practice, maps: FakeMaps) -> None:
    r = await pbook(practice, at(practice.day, 10), "office", address=typed())
    assert r.status_code == 422 and "on-site" in r.json()["detail"] and maps.requests == []


# --------------------------------------------------------------------- saved addresses
async def test_a_saved_address_without_a_pin_is_looked_up(seeded: Seed, otp: Any, maps: FakeMaps) -> None:
    maps.places[HOME] = found(*NEAR)
    me = await login(seeded)
    created = await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)
    assert created.status_code == 201, created.text
    assert (created.json()["latitude"], created.json()["longitude"]) == NEAR and maps.asked() == [HOME]
    pinned = await seeded.client.post("/v1/public/me/addresses", json={**typed("Office"), "latitude": 1.5, "longitude": 103.7}, headers=me)
    assert pinned.status_code == 201 and (pinned.json()["latitude"], pinned.json()["longitude"]) == (1.5, 103.7) and len(maps.requests) == 1  # a pin needs no lookup
    assert (await seeded.client.post("/v1/public/me/addresses", json={**typed("Half"), "latitude": 1.5}, headers=me)).status_code == 422


async def test_a_saved_address_that_cannot_be_placed_is_not_saved(seeded: Seed, otp: Any, maps: FakeMaps, monkeypatch: pytest.MonkeyPatch) -> None:
    me = await login(seeded)
    assert (await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)).json()["error"] == "address_not_found"
    maps.places[HOME] = found(*NEAR, "APPROXIMATE")
    assert (await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)).json()["error"] == "address_not_precise"
    maps.failure = httpx.ConnectError("down")
    assert (await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)).status_code == 503
    monkeypatch.setattr(geocoding, "_provider", None)
    assert (await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)).json()["error"] == "geocoding_unavailable"
    assert await addresses(seeded) == []


async def test_changing_the_words_of_an_address_moves_its_pin(seeded: Seed, otp: Any, maps: FakeMaps) -> None:
    maps.places[HOME] = found(*NEAR)
    maps.places["99 Jalan Baru, 50450 Kuala Lumpur"] = found(*SEREMBAN)
    maps.places["99 Jalan Baru, 40000 Shah Alam"] = found(*SEREMBAN)
    maps.places["99 Jalan Baru, Shah Alam"] = found(*NEAR)
    me = await login(seeded)
    aid = (await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)).json()["id"]
    assert len(maps.requests) == 1
    patch = lambda body: seeded.client.patch(f"/v1/public/me/addresses/{aid}", json=body, headers=me)  # noqa: E731
    moved = await patch({"line1": "99 Jalan Baru"})
    assert moved.status_code == 200 and (moved.json()["latitude"], moved.json()["longitude"]) == SEREMBAN
    assert maps.asked()[-1] == "99 Jalan Baru, 50450 Kuala Lumpur"  # the parts that did not change are kept
    count = len(maps.requests)
    same = await patch({"line1": "99 Jalan Baru", "city": "Kuala Lumpur", "postcode": "50450"})  # nothing really changed
    assert same.status_code == 200 and len(maps.requests) == count
    postcode = await patch({"postcode": "40000", "city": "Shah Alam"})
    assert postcode.status_code == 200 and maps.asked()[-1] == "99 Jalan Baru, 40000 Shah Alam" and len(maps.requests) == count + 1
    cleared = await patch({"postcode": None})  # removing the postcode is a change too: the address is looked up without it
    assert cleared.status_code == 200 and maps.asked()[-1] == "99 Jalan Baru, Shah Alam" and (cleared.json()["latitude"], cleared.json()["longitude"]) == NEAR


async def test_a_new_pin_in_the_same_change_is_used_instead_of_a_lookup(seeded: Seed, otp: Any, maps: FakeMaps) -> None:
    maps.places[HOME] = found(*NEAR)
    me = await login(seeded)
    aid = (await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)).json()["id"]
    r = await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"line1": "Somewhere else", "latitude": 1.5, "longitude": 103.7}, headers=me)
    assert r.status_code == 200 and (r.json()["latitude"], r.json()["longitude"]) == (1.5, 103.7) and len(maps.requests) == 1
    only_pin = await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"latitude": 1.6, "longitude": 103.8}, headers=me)
    assert only_pin.status_code == 200 and len(maps.requests) == 1  # a pin on its own never needs one
    assert (await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"latitude": 1.6}, headers=me)).status_code == 422


async def test_a_change_that_cannot_be_placed_leaves_the_address_as_it_was(seeded: Seed, otp: Any, maps: FakeMaps) -> None:
    maps.places[HOME] = found(*NEAR)
    maps.places["99 Jalan Baru, 50450 Kuala Lumpur"] = found(*SEREMBAN, "APPROXIMATE")
    me = await login(seeded)
    aid = (await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)).json()["id"]
    for body, status in (({"line1": "99 Jalan Baru"}, 422), ({"line1": "Nowhere Road"}, 422)):
        assert (await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json=body, headers=me)).status_code == status
    maps.failure = httpx.ReadTimeout("slow")
    assert (await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"line1": "Elsewhere"}, headers=me)).status_code == 503
    assert await addresses(seeded) == [("12 Jalan Bukit", NEAR[0], NEAR[1])]


async def test_without_a_provider_a_changed_address_keeps_its_pin(seeded: Seed, otp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(geocoding, "_provider", None)
    me = await login(seeded)
    aid = (await seeded.client.post("/v1/public/me/addresses", json={**typed(), "latitude": 3.1, "longitude": 101.7}, headers=me)).json()["id"]
    r = await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"line1": "14 Jalan Bukit"}, headers=me)
    assert r.status_code == 200 and (r.json()["line1"], r.json()["latitude"], r.json()["longitude"]) == ("14 Jalan Bukit", 3.1, 101.7)  # as before geocoding existed


# --------------------------------------------------------------------- the business's own location
async def test_the_business_can_give_its_address_instead_of_coordinates(client: httpx.AsyncClient, maps: FakeMaps) -> None:
    bare = await make_practice(client, radius=None)
    maps.places["1 Jalan Raja, Kuala Lumpur"] = found(*KL, formatted="1, Jalan Raja, Kuala Lumpur, Malaysia")
    r = await bare.client.put("/v1/admin/business-location", json={"address": "1 Jalan Raja, Kuala Lumpur", "service_radius_km": 12.5}, headers=bare.admin)
    assert r.status_code == 200, r.text
    assert r.json() == {"latitude": KL[0], "longitude": KL[1], "service_radius_km": 12.5, "formatted_address": "1, Jalan Raja, Kuala Lumpur, Malaysia", "maps_url": None}
    stored = (await bare.client.get("/v1/admin/business-location", headers=bare.admin)).json()
    assert stored == {"latitude": KL[0], "longitude": KL[1], "service_radius_km": 12.5}  # reading it back is unchanged
    assert "x-bookings-flagged" in r.headers  # bookings are re-checked against the new place, as with coordinates
    pinned = await bare.client.put("/v1/admin/business-location", json={"latitude": 1.5, "longitude": 103.7, "service_radius_km": 5}, headers=bare.admin)
    assert pinned.status_code == 200 and pinned.json()["formatted_address"] is None and len(maps.requests) == 1


async def test_the_business_location_is_one_way_or_the_other(client: httpx.AsyncClient, maps: FakeMaps, monkeypatch: pytest.MonkeyPatch) -> None:
    bare = await make_practice(client, radius=None)
    maps.places["1 Jalan Raja"] = found(*KL)
    put = lambda body: bare.client.put("/v1/admin/business-location", json={"service_radius_km": 10, **body}, headers=bare.admin)  # noqa: E731
    for body in ({}, {"address": "1 Jalan Raja", "latitude": 3.1, "longitude": 101.7}, {"address": "1 Jalan Raja", "latitude": 3.1}, {"latitude": 3.1}, {"longitude": 101.7}, {"address": "ab"}):
        assert (await put(body)).status_code == 422, body
    assert maps.requests == []
    assert (await put({"address": "Nowhere"})).json()["error"] == "address_not_found"
    maps.places["Taman Desa"] = found(*KL, "APPROXIMATE")
    assert (await put({"address": "Taman Desa"})).json()["error"] == "address_not_precise"  # a business position must be a real point too
    maps.failure = httpx.ConnectError("down")
    assert (await put({"address": "1 Jalan Raja"})).status_code == 503
    monkeypatch.setattr(geocoding, "_provider", None)
    assert (await put({"address": "1 Jalan Raja"})).json()["error"] == "geocoding_unavailable"
    assert (await bare.client.get("/v1/admin/business-location", headers=bare.admin)).json() == {"latitude": None, "longitude": None, "service_radius_km": None}
    assert (await bare.client.put("/v1/admin/business-location", json={"address": "1 Jalan Raja", "service_radius_km": 10}, headers=await bare.token("staff"))).status_code == 403
    assert (await put({"address": "1 Jalan Raja", "service_radius_km": 0})).status_code == 422


async def test_the_location_is_saved_to_the_right_business(client: httpx.AsyncClient, maps: FakeMaps) -> None:
    a, b = await make_practice(client, radius=None), await make_practice(client, radius=None)
    maps.places["1 Jalan Raja"] = found(*KL)
    assert (await a.client.put("/v1/admin/business-location", json={"address": "1 Jalan Raja", "service_radius_km": 9}, headers=a.admin)).status_code == 200
    assert (await b.client.get("/v1/admin/business-location", headers=b.admin)).json()["latitude"] is None
    async with tenant_session(a.tenant_id) as db:
        tenant = await db.get(Tenant, a.tenant_id)
        assert tenant is not None and (tenant.latitude, tenant.longitude, tenant.service_radius_km) == (KL[0], KL[1], 9)


async def test_the_api_sets_the_provider_up_when_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import main
    from app.core import hardening
    from app.services import delivery

    seen: list[str] = []

    async def no_checks(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(hardening, "enforce", no_checks)
    monkeypatch.setattr(delivery, "configure_providers", lambda s: None)
    monkeypatch.setattr(delivery, "report_configuration", lambda s: [])
    monkeypatch.setattr(geocoding, "configure", lambda s: seen.append("geocoding"))
    async with main.lifespan(main.app):
        assert seen == ["geocoding"]


async def test_a_business_with_a_point_but_no_radius_has_nothing_to_compare_either(practice: Practice, maps: FakeMaps) -> None:
    from sqlalchemy import text

    maps.places[HOME] = found(*NEAR)
    async with tenant_session(practice.tenant_id) as db:
        await db.execute(text("UPDATE tenants SET service_radius_km = NULL WHERE id = :t"), {"t": practice.tenant_id})
    body = (await geocode(practice, HOME)).json()
    assert body["within_service_area"] is None and body["distance_km"] is None and body["latitude"] == NEAR[0]


async def test_changing_only_the_city_moves_the_pin_too(seeded: Seed, otp: Any, maps: FakeMaps) -> None:
    maps.places[HOME] = found(*NEAR)
    maps.places["12 Jalan Bukit, 50450 Shah Alam"] = found(*SEREMBAN)
    me = await login(seeded)
    aid = (await seeded.client.post("/v1/public/me/addresses", json=typed(), headers=me)).json()["id"]
    r = await seeded.client.patch(f"/v1/public/me/addresses/{aid}", json={"city": "Shah Alam"}, headers=me)
    assert r.status_code == 200 and (r.json()["latitude"], r.json()["longitude"]) == SEREMBAN
    assert maps.asked()[-1] == "12 Jalan Bukit, 50450 Shah Alam" and len(maps.requests) == 2
