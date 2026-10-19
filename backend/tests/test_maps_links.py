"""A Google Maps link as the business's location (``app.services.maps_links``, ``PUT /admin/business-location`` with ``maps_url``)."""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from pydantic import ValidationError

from app.core.config import get_settings
from app.core.exceptions import MapsLinkFailed, MapsLinkInvalid
from app.schemas.scheduling import BusinessLocationIn
from app.services import maps_links
from app.services.maps_links import MapsPoint, MapsReading, is_maps_link, read, resolve
from tests.test_field_service import KL, make_practice
from tests.test_geocoding import FakeMaps, found, maps  # noqa: F401  (``maps`` is a fixture)

SHORT = "https://maps.app.goo.gl/AbCdEf123"
PLACE = "https://www.google.com/maps/place/Serene+Lotus+Spa/@3.1300000,101.6700000,17z/data=!3m1!4b1!4m6!3m5!1s0x0:0x1!8m2!3d3.1317842!4d101.6712391"


class FakeGoogle:
    """Answers each URL from ``hops`` (a redirect to the given place, or a page); remembers every request."""

    def __init__(self) -> None:
        self.hops: dict[str, Any] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.hops.get(str(request.url))
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, str):
            return httpx.Response(302, headers={"Location": answer})
        return httpx.Response(200, text="<html>a page</html>")


@pytest.fixture
def google(monkeypatch: pytest.MonkeyPatch) -> FakeGoogle:
    fake = FakeGoogle()
    monkeypatch.setattr(maps_links, "_transport", httpx.MockTransport(fake.handler))
    return fake


# --------------------------------------------------------------------- which links are Google Maps links
@pytest.mark.parametrize("url", [
    SHORT, "https://goo.gl/maps/xyz123", PLACE, "https://maps.google.com/?q=3.1,101.6", "https://google.com.my/maps/@3.1,101.6,15z",
    "https://www.google.co.uk/maps/search/spa", "https://maps.google.com.au/maps?ll=-33.8,151.2", "https://www.google.com/maps?q=Lotus",
])
def test_google_maps_links_are_accepted(url: str) -> None:
    assert is_maps_link(url)


@pytest.mark.parametrize("url", [
    "http://maps.app.goo.gl/AbCdEf123",            # not HTTPS
    "https://maps.app.goo.gl/",                     # no link in it
    "https://goo.gl/xyz123",                        # a goo.gl link that is not a Maps link
    "https://www.google.com/search?q=spa",          # Google, but not Maps
    "https://www.google.com/mapsfoo",               # a path that only starts like /maps
    "https://google.evil.com/maps/@3.1,101.6,15z",  # someone else's host
    "https://maps.google.com.evil.com/?q=1,2",
    "https://evil.com/?u=https://maps.app.goo.gl/x",
    "https://user:pw@maps.google.com/?q=3.1,101.6", "https://maps.google.com:8443/?q=3.1,101.6",
    "javascript:alert(1)", "not a link", "https://[::1/maps",
])
def test_anything_else_is_refused(url: str) -> None:
    assert not is_maps_link(url)


# --------------------------------------------------------------------- reading a link without calling anyone
def test_the_place_pin_wins_over_the_map_centre() -> None:
    assert read(PLACE) == MapsReading(point=MapsPoint(3.1317842, 101.6712391))


@pytest.mark.parametrize(("url", "point"), [
    ("https://www.google.com/maps/@3.1186,101.6216,17z", (3.1186, 101.6216)),
    ("https://www.google.com/maps/@-33.8688,151.2093,14z?entry=ttu", (-33.8688, 151.2093)),
    ("https://maps.google.com/?q=3.1186,101.6216", (3.1186, 101.6216)),
    ("https://maps.google.com/?q=3.1186%2C+101.6216", (3.1186, 101.6216)),
    ("https://www.google.com/maps/search/?api=1&query=1.2903,103.8520", (1.2903, 103.852)),
    ("https://maps.google.com/maps?ll=-37.81,144.96&z=12", (-37.81, 144.96)),
    ("https://www.google.com/maps/dir/?api=1&destination=3.15,101.71", (3.15, 101.71)),
])
def test_points_are_read_from_full_links(url: str, point: tuple[float, float]) -> None:
    assert read(url) == MapsReading(point=MapsPoint(*point))


@pytest.mark.parametrize(("url", "words"), [
    ("https://www.google.com/maps?q=Serene+Lotus+Spa,+Bangsar", "Serene Lotus Spa, Bangsar"),
    ("https://www.google.com/maps/place/Serene+Lotus+Spa/", "Serene Lotus Spa"),
    ("https://www.google.com/maps/search/?api=1&query=Studio%20Gunting%20Subang", "Studio Gunting Subang"),
])
def test_a_place_search_gives_words_to_look_up(url: str, words: str) -> None:
    assert read(url) == MapsReading(words=words)


@pytest.mark.parametrize("url", [
    "https://www.google.com/maps/@91.0,101.6,17z",    # no such latitude
    "https://www.google.com/maps/@3.1,181.0,17z",     # no such longitude
    "https://maps.google.com/?q=0,0",                  # the "null island" of a broken link
    "https://www.google.com/maps",                     # nothing at all
    "https://maps.google.com/?q=ab",                   # too short to look up
])
def test_nothing_usable_reads_as_nothing(url: str) -> None:
    assert read(url) == MapsReading()


# --------------------------------------------------------------------- following a share link
async def test_a_share_link_is_followed_to_the_point(google: FakeGoogle) -> None:
    google.hops[SHORT] = PLACE
    assert await resolve(SHORT) == MapsReading(point=MapsPoint(3.1317842, 101.6712391))
    (request,) = google.requests
    assert request.method == "GET" and str(request.url) == SHORT


async def test_a_full_link_needs_no_call(google: FakeGoogle) -> None:
    assert (await resolve(PLACE)).point == MapsPoint(3.1317842, 101.6712391)
    assert (await resolve("https://www.google.com/maps?q=Serene+Lotus+Spa")).words == "Serene Lotus Spa"
    assert google.requests == []


async def test_googles_consent_page_is_seen_through(google: FakeGoogle) -> None:
    from urllib.parse import quote
    google.hops[SHORT] = "https://consent.google.com/ml?continue=" + quote(PLACE, safe="") + "&gl=MY"
    assert (await resolve(SHORT)).point == MapsPoint(3.1317842, 101.6712391)
    assert len(google.requests) == 1  # the consent page itself is never opened


async def test_several_hops_and_relative_redirects_are_followed(google: FakeGoogle) -> None:
    google.hops[SHORT] = "https://goo.gl/maps/second"
    google.hops["https://goo.gl/maps/second"] = "/maps/third"
    google.hops["https://goo.gl/maps/third"] = "https://www.google.com/maps?q=3.2,101.5"
    assert (await resolve(SHORT)).point == MapsPoint(3.2, 101.5)
    assert len(google.requests) == 3


async def test_a_share_link_to_a_place_search_gives_the_words(google: FakeGoogle) -> None:
    google.hops[SHORT] = "https://www.google.com/maps/search/Serene+Lotus+Spa"
    google.hops["https://www.google.com/maps/search/Serene+Lotus+Spa"] = "https://www.google.com/maps?q=Serene+Lotus+Spa,+Bangsar"
    assert await resolve(SHORT) == MapsReading(words="Serene Lotus Spa, Bangsar")


async def test_a_redirect_away_from_google_maps_is_not_followed(google: FakeGoogle) -> None:
    google.hops[SHORT] = "https://evil.example/maps/@3.1,101.6,17z"
    assert await resolve(SHORT) == MapsReading()
    assert [str(r.url) for r in google.requests] == [SHORT]  # the other host is never called
    google.hops[SHORT] = "http://www.google.com/maps/@3.1,101.6,17z"  # not HTTPS either
    assert await resolve(SHORT) == MapsReading()
    google.hops[SHORT] = "https://consent.google.com/ml?continue=https://evil.example/x"
    assert await resolve(SHORT) == MapsReading()


async def test_the_hops_are_limited(google: FakeGoogle) -> None:
    for n in range(10):
        google.hops[f"https://goo.gl/maps/{n}"] = f"https://goo.gl/maps/{n + 1}"
    assert await resolve("https://goo.gl/maps/0") == MapsReading()
    assert len(google.requests) == maps_links.MAX_HOPS


async def test_a_page_instead_of_a_redirect_ends_it(google: FakeGoogle) -> None:
    assert await resolve(SHORT) == MapsReading()  # (the fake answers 200 with a page)
    assert len(google.requests) == 1


async def test_google_not_answering_is_a_failure_not_a_guess(google: FakeGoogle) -> None:
    google.hops[SHORT] = httpx.ConnectTimeout("slow")
    with pytest.raises(MapsLinkFailed):
        await resolve(SHORT)


async def test_not_a_maps_link_is_refused_before_any_call(google: FakeGoogle) -> None:
    with pytest.raises(MapsLinkInvalid):
        await resolve("https://evil.example/?q=3.1,101.6")
    assert google.requests == []


async def test_the_time_limit_is_the_settings(google: FakeGoogle, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = httpx.AsyncClient.__init__

    def spy(self: httpx.AsyncClient, *args: Any, **kw: Any) -> None:
        seen.append(kw.get("timeout"))
        real(self, *args, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    google.hops[SHORT] = PLACE
    await resolve(SHORT, timeout=2.5)
    assert seen == [2.5]


# --------------------------------------------------------------------- the business's location
async def test_the_business_sets_its_location_with_a_share_link(client: httpx.AsyncClient, google: FakeGoogle) -> None:
    bare = await make_practice(client, radius=None)
    google.hops[SHORT] = PLACE
    r = await bare.client.put("/v1/admin/business-location", json={"maps_url": f"  {SHORT} ", "service_radius_km": 20}, headers=bare.admin)
    assert r.status_code == 200, r.text
    assert r.json() == {"latitude": 3.1317842, "longitude": 101.6712391, "service_radius_km": 20, "formatted_address": None, "maps_url": SHORT}
    assert "x-bookings-flagged" in r.headers  # bookings are re-checked against the new place
    assert (await bare.client.get("/v1/admin/business-location", headers=bare.admin)).json() == {"latitude": 3.1317842, "longitude": 101.6712391, "service_radius_km": 20}
    profile = (await bare.client.get("/v1/admin/business-profile", headers=bare.admin)).json()
    assert profile["google_maps_url"] == SHORT  # the same link becomes the directions link
    public = (await bare.client.get("/v1/public/business", headers=bare.pk)).json()
    assert public["google_maps_url"] == SHORT


async def test_a_profile_that_exists_keeps_everything_else(client: httpx.AsyncClient, google: FakeGoogle) -> None:
    bare = await make_practice(client, radius=None)
    put = await bare.client.put("/v1/admin/business-profile", json={"business_name": "Kemas", "description": "Aircond", "contact_no": "+603 1234"}, headers=bare.admin)
    assert put.status_code == 200, put.text
    r = await bare.client.put("/v1/admin/business-location", json={"maps_url": PLACE, "service_radius_km": 5}, headers=bare.admin)
    assert r.status_code == 200, r.text
    profile = (await bare.client.get("/v1/admin/business-profile", headers=bare.admin)).json()
    assert (profile["business_name"], profile["description"], profile["contact_no"], profile["google_maps_url"]) == ("Kemas", "Aircond", "+603 1234", PLACE)
    assert google.requests == []


async def test_a_place_search_link_is_looked_up_like_an_address(client: httpx.AsyncClient, google: FakeGoogle, maps: FakeMaps) -> None:
    bare = await make_practice(client, radius=None)
    maps.places["Serene Lotus Spa, Bangsar"] = found(*KL, formatted="23 Jalan Telawi 3, Bangsar, Kuala Lumpur")
    r = await bare.client.put("/v1/admin/business-location", json={"maps_url": "https://www.google.com/maps?q=Serene+Lotus+Spa,+Bangsar", "service_radius_km": 5}, headers=bare.admin)
    assert r.status_code == 200, r.text
    assert (r.json()["latitude"], r.json()["longitude"], r.json()["formatted_address"]) == (KL[0], KL[1], "23 Jalan Telawi 3, Bangsar, Kuala Lumpur")
    assert maps.asked() == ["Serene Lotus Spa, Bangsar"]
    maps.places["Taman Desa"] = found(*KL, "APPROXIMATE")
    vague = await bare.client.put("/v1/admin/business-location", json={"maps_url": "https://www.google.com/maps?q=Taman+Desa", "service_radius_km": 5}, headers=bare.admin)
    assert vague.json()["error"] == "address_not_precise"  # the same precision rule as a typed address


async def test_links_that_cannot_be_used_change_nothing(client: httpx.AsyncClient, google: FakeGoogle, maps: FakeMaps) -> None:
    bare = await make_practice(client, radius=None)
    put = lambda body: bare.client.put("/v1/admin/business-location", json={"service_radius_km": 10, **body}, headers=bare.admin)  # noqa: E731
    assert (await put({"maps_url": "https://evil.example/@3.1,101.6,17z"})).json()["error"] == "maps_link_invalid"
    assert (await put({"maps_url": SHORT})).json()["error"] == "maps_link_unreadable"  # the share link leads to a page with no place
    google.hops[SHORT] = httpx.ConnectError("down")
    failed = await put({"maps_url": SHORT})
    assert failed.status_code == 503 and failed.json()["error"] == "maps_link_failed"
    for body in ({"maps_url": PLACE, "latitude": 3.1, "longitude": 101.6}, {"maps_url": PLACE, "address": "1 Jalan Raja"}, {"maps_url": "short"}):
        assert (await put(body)).status_code == 422, body
    assert (await bare.client.get("/v1/admin/business-location", headers=bare.admin)).json() == {"latitude": None, "longitude": None, "service_radius_km": None}
    profile = await bare.client.get("/v1/admin/business-profile", headers=bare.admin)
    assert profile.status_code == 404 or profile.json()["google_maps_url"] is None
    assert (await bare.client.put("/v1/admin/business-location", json={"maps_url": PLACE, "service_radius_km": 10}, headers=await bare.token("staff"))).status_code == 403


def test_the_location_body_needs_exactly_one_way_to_say_where() -> None:
    for body in ({}, {"maps_url": PLACE, "address": "1 Jalan Raja"}, {"maps_url": PLACE, "latitude": 3.1, "longitude": 101.6}):
        with pytest.raises(ValidationError):
            BusinessLocationIn(service_radius_km=5, **body)
    assert BusinessLocationIn(service_radius_km=5, maps_url=PLACE).maps_url == PLACE


async def test_the_business_location_uses_the_configured_time_limit(client: httpx.AsyncClient, google: FakeGoogle, monkeypatch: pytest.MonkeyPatch) -> None:
    bare = await make_practice(client, radius=None)
    seen: list[Any] = []
    real = maps_links.resolve

    async def spy(url: str, *, timeout: float = 5.0) -> MapsReading:
        seen.append(timeout)
        return await real(url, timeout=timeout)

    monkeypatch.setattr(maps_links, "resolve", spy)
    monkeypatch.setattr(get_settings(), "maps_link_timeout_seconds", 1.25)
    google.hops[SHORT] = PLACE
    assert (await bare.client.put("/v1/admin/business-location", json={"maps_url": SHORT, "service_radius_km": 5}, headers=bare.admin)).status_code == 200
    assert seen == [1.25]
