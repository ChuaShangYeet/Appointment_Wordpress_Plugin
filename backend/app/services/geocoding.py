"""Turning a typed address into map coordinates (blueprint §11.2 "select or enter an address", §15 "Google Maps: geocoding").

On-site services are checked against the business's service radius with PostGIS, which needs coordinates. A client can send the customer's map pin
(then nothing here runs), or the address as typed: this module looks it up through a **provider** (Google's Geocoding API is the one built; the
seam is the same as for messages, see :mod:`app.services.delivery`) and the booking carries on with the coordinates it found.

* **Precision matters more than coverage.** The radius check is only as good as the point. A result Google says is only *approximate* (a town, a
  district) or a *partial match* (it could not match everything that was typed) is not good enough to accept a booking on: bookings and saved
  addresses refuse it (``address_not_precise``: give a fuller address or drop a pin). ``POST /public/geocode`` returns such a result with
  ``exact: false`` so a page can show it and ask the customer to confirm.
* **Failure is visible.** Nothing found is 422 (``address_not_found``); the provider down, over quota, denied or answering rubbish is 503
  (``geocoding_failed``); no provider configured is 422 (``geocoding_unavailable``: send the coordinates). The reason goes to the log; the
  request URL (which holds the API key) and the address never do, and never into an error text either.
* **Privacy.** The address a customer types is personal data and is sent to the provider (Google) to be looked up: list it as a sub-processor. Nothing is cached here.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from app.core.config import Settings
from app.core.exceptions import AddressNotFound, AddressNotPrecise, GeocodingFailed, GeocodingUnavailable

log = logging.getLogger(__name__)

#: Google's ``geometry.location_type`` values that name a point on the ground (the street, the building) rather than an area.
PRECISE_TYPES = frozenset({"ROOFTOP", "RANGE_INTERPOLATED", "GEOMETRIC_CENTER"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float
    formatted_address: str
    #: A point (building or street), not an area, and the whole address matched.
    exact: bool


class GeocodingProvider(Protocol):
    name: str

    async def geocode(self, address: str, region: str | None = None) -> Location | None:
        """The best match for ``address``, ``None`` when nothing matches; :class:`GeocodingFailed` when the lookup itself failed.

        ``region`` (``my``, ``sg``, ``au``) leans the answer towards the business's country, without ruling anything else out.
        """


class GoogleGeocoder:
    """Google Maps Geocoding API (``maps.googleapis.com/maps/api/geocode/json``)."""

    name = "google"

    def __init__(self, api_key: str, *, base_url: str = "https://maps.googleapis.com", timeout: float = 5.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._key = api_key
        self.base_url, self.timeout, self.transport = base_url.rstrip("/"), timeout, transport

    @property
    def url(self) -> str:
        return f"{self.base_url}/maps/api/geocode/json"

    async def geocode(self, address: str, region: str | None = None) -> Location | None:
        params = {"address": address, "key": self._key}
        if region:
            params["region"] = region  # a bias towards the business's country, not a restriction
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                response = await client.get(self.url, params=params)
        except httpx.HTTPError as exc:  # the exception text carries the URL, and the URL carries the key: keep only its type
            log.warning("geocoding request failed: %s", type(exc).__name__)
            raise GeocodingFailed("We could not look up that address just now; please try again or drop a pin on the map") from exc
        return self._read(response)

    @staticmethod
    def _fail(reason: str) -> GeocodingFailed:
        log.warning("geocoding failed: %s", reason)
        return GeocodingFailed("We could not look up that address just now; please try again or drop a pin on the map")

    def _read(self, response: httpx.Response) -> Location | None:
        if response.status_code != 200:
            raise self._fail(f"HTTP {response.status_code}")
        try:
            body = response.json()
            status = str(body["status"])
        except (ValueError, KeyError, TypeError) as exc:
            raise self._fail("unreadable answer") from exc
        if status == "ZERO_RESULTS":
            return None
        if status != "OK":  # OVER_QUERY_LIMIT, REQUEST_DENIED, INVALID_REQUEST, UNKNOWN_ERROR: the operator needs to know which
            detail = str(body.get("error_message", ""))[:200] if isinstance(body, dict) else ""
            raise self._fail(f"{status} {detail}".strip())
        try:
            best: dict[str, Any] = body["results"][0]
            point = best["geometry"]["location"]
            latitude, longitude = float(point["lat"]), float(point["lng"])
            kind = str(best["geometry"].get("location_type", ""))
            formatted = str(best.get("formatted_address", ""))
            partial = bool(best.get("partial_match", False))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise self._fail("unreadable result") from exc
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):  # (NaN and infinity are in no range either)
            raise self._fail("coordinates out of range")
        return Location(latitude, longitude, formatted, exact=kind in PRECISE_TYPES and not partial)


_provider: GeocodingProvider | None = None


def set_provider(provider: GeocodingProvider | None) -> None:
    global _provider
    _provider = provider


def get_provider() -> GeocodingProvider | None:
    return _provider


def configure(settings: Settings) -> None:
    """Register the provider the settings describe (called when the API starts); outside development, say so when there is none."""
    set_provider(None)
    if settings.geocoding_provider == "google":
        key = settings.google_maps_api_key.get_secret_value()
        if key:
            set_provider(GoogleGeocoder(key, base_url=settings.geocoding_base_url, timeout=settings.geocoding_timeout_seconds))
        else:
            log.error("GEOCODING_PROVIDER=google needs GOOGLE_MAPS_API_KEY; typed addresses cannot be located")
    elif settings.environment not in ("development", "test"):
        log.warning("no geocoding provider is configured (GEOCODING_PROVIDER): on-site bookings need the customer's map-pin coordinates")


def address_text(line1: str, city: str | None = None, postcode: str | None = None) -> str:
    """One line to look up: ``12 Jalan Bukit, 50450 Kuala Lumpur`` (control characters removed, empty parts left out)."""
    area = " ".join(part for part in ((postcode or "").strip(), (city or "").strip()) if part)
    return _CONTROL.sub(" ", ", ".join(part for part in (line1.strip(), area) if part)).strip()


async def locate(address: str, region: str | None = None) -> Location:
    """Look an address up. Raises :class:`GeocodingUnavailable`, :class:`AddressNotFound` or :class:`GeocodingFailed`; the result may not be exact."""
    provider = _provider
    if provider is None:
        raise GeocodingUnavailable("This business cannot look addresses up; send the map coordinates (latitude and longitude) as well")
    found = await provider.geocode(_CONTROL.sub(" ", address).strip(), region)
    if found is None:
        raise AddressNotFound("We could not find that address; check it, or drop a pin on the map")
    return found


async def locate_exact(address: str, region: str | None = None) -> Location:
    """As :func:`locate`, but only a result good enough to measure a distance with (see the module notes)."""
    found = await locate(address, region)
    if not found.exact:
        raise AddressNotPrecise(
            "That address is not specific enough to place on the map; add the street and number, or drop a pin",
            extra={"formatted_address": found.formatted_address},
        )
    return found
