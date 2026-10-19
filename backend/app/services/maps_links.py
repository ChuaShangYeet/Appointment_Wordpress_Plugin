"""A Google Maps link as the business's location: the owner pastes the link Google Maps gives them and the map point is read from it.

Three kinds of link reach us, and each is handled differently:

* **A full link** (copied from the browser's address bar) carries the point itself: the place's pin as ``!3d<lat>!4d<lng>``, the map's centre as
  ``/@<lat>,<lng>,<zoom>z``, or ``q=``/``ll=``/``query=`` holding ``<lat>,<lng>``. It is read from the text, with no call to anyone.
* **A share link** (``https://maps.app.goo.gl/...``, what the phone app's Share button gives) carries nothing; it is a redirect. It is followed
  hop by hop, **only ever to Google's own Maps hosts over HTTPS** (a hop anywhere else stops it), at most :data:`MAX_HOPS` hops, each with a time limit,
  and without reading any page: the point is read from the address each hop leads to. Google may send a consent page first
  (``consent.google.com/...?continue=<the real link>``); the real link is read from ``continue``.
* **A place search** (``?q=Serene+Lotus+Spa``, or a share link that leads to one) holds words, not a point. The words are looked up like a typed
  address (:mod:`app.services.geocoding`), so the same precision rule applies.

Which point wins: the pin (``!3d!4d``) is where the place is; ``@`` is only where the map was centred when the link was made, so it is used only
when there is no pin. Anything that is not a Google Maps link is refused before any call is made (``maps_link_invalid``); a Maps link the point
cannot be read from is ``maps_link_unreadable``; Google not answering is ``maps_link_failed`` (503).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

import httpx

from app.core.exceptions import MapsLinkFailed, MapsLinkInvalid, MapsLinkUnreadable

log = logging.getLogger(__name__)

MAX_HOPS = 5
#: Hosts a share link may lead through. Short links first; the rest are Google Maps itself in any country (google.com, google.com.my, google.co.uk ...).
SHORT_HOSTS = frozenset({"maps.app.goo.gl", "goo.gl"})
_GOOGLE = re.compile(r"^(?:www\.|maps\.)?google\.(?:com|co)(?:\.[a-z]{2})?$|^(?:www\.|maps\.)?google\.[a-z]{2}$")
_CONSENT = "consent.google.com"
_NUMBER = r"(-?\d{1,3}(?:\.\d+)?)"
_PIN = re.compile(r"!3d" + _NUMBER + r"!4d" + _NUMBER)
_CENTRE = re.compile(r"/@" + _NUMBER + r"," + _NUMBER + r"(?:,|$|/|\?)")
_PAIR = re.compile(r"^\s*" + _NUMBER + r"\s*,\s*" + _NUMBER + r"\s*$")
_POINT_PARAMS = ("q", "query", "ll", "center", "destination", "daddr")
_WORD_PARAMS = ("q", "query", "destination", "daddr")

#: Only for tests: the transport the redirects are followed through.
_transport: httpx.AsyncBaseTransport | None = None


def set_transport(transport: httpx.AsyncBaseTransport | None) -> None:
    global _transport
    _transport = transport


@dataclass(frozen=True)
class MapsPoint:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class MapsReading:
    """What a link says: a point, or failing that the words of a place search (to be looked up), or neither."""

    point: MapsPoint | None = None
    words: str | None = None


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def is_maps_host(host: str) -> bool:
    return host in SHORT_HOSTS or bool(_GOOGLE.match(host))


def is_maps_link(url: str) -> bool:
    """A Google Maps link over HTTPS: a share link, ``maps.google.*``, or ``google.*`` with a path under ``/maps``."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme != "https" or not host or parts.username or parts.password or parts.port not in (None, 443):
        return False
    if host == "goo.gl":
        return parts.path.startswith("/maps")
    if host == "maps.app.goo.gl":
        return len(parts.path) > 1
    if not _GOOGLE.match(host):
        return False
    return host.startswith("maps.") or parts.path == "/maps" or parts.path.startswith("/maps/") or parts.path.startswith("/maps?")


def _point(latitude: str, longitude: str) -> MapsPoint | None:
    lat, lng = float(latitude), float(longitude)
    if not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0):
        return None
    return MapsPoint(lat, lng)


def read(url: str) -> MapsReading:
    """What the link's own text says, without calling anyone."""
    text = unquote(url)
    if (found := _PIN.search(text)) and (p := _point(*found.groups())):
        return MapsReading(point=p)
    try:
        query = parse_qs(urlsplit(url).query)
    except ValueError:
        query = {}
    for name in _POINT_PARAMS:
        for value in query.get(name, []):
            if (pair := _PAIR.match(value)) and (p := _point(*pair.groups())):
                return MapsReading(point=p)
    if (found := _CENTRE.search(text)) and (p := _point(*found.groups())):
        return MapsReading(point=p)
    for name in _WORD_PARAMS:
        for value in query.get(name, []):
            words = " ".join(value.split())
            if len(words) >= 3 and not _PAIR.match(words):
                return MapsReading(words=words[:300])
    place = re.search(r"/maps/place/([^/@?]+)", text)
    if place:
        words = " ".join(place.group(1).replace("+", " ").split())
        if len(words) >= 3:
            return MapsReading(words=words[:300])
    return MapsReading()


def _unwrap_consent(url: str) -> str | None:
    """Google's cookie-consent page carries the link it interrupted in ``continue``."""
    if _host(url) != _CONSENT:
        return None
    found = parse_qs(urlsplit(url).query).get("continue", [])
    return found[0] if found else None  # (where it leads is checked like any other hop)


async def resolve(url: str, *, timeout: float = 5.0) -> MapsReading:
    """Read the link, following a share link's redirects (only through Google's Maps hosts) until a point turns up.

    Raises :class:`MapsLinkInvalid` for anything that is not a Google Maps link, :class:`MapsLinkFailed` when Google cannot be reached.
    The reading may be empty (the caller decides what that means)."""
    url = url.strip()
    if not is_maps_link(url):
        raise MapsLinkInvalid("That is not a Google Maps link. In Google Maps, open the business, tap Share and copy the link.")
    reading = read(url)
    if reading.point or _host(url) not in SHORT_HOSTS:
        return reading
    current = url
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=_transport, follow_redirects=False) as client:
            for _ in range(MAX_HOPS):
                async with client.stream("GET", current, headers={"User-Agent": "Mozilla/5.0 (compatible; JustSimple location check)"}) as response:
                    target = response.headers.get("location") if response.is_redirect else None
                if not target:
                    break
                target = urljoin(current, target)
                current = _unwrap_consent(target) or target
                if not is_maps_link(current):  # (never anywhere but Google Maps, whatever the redirect says)
                    log.info("maps link: a redirect left Google Maps (%s)", _host(current) or "no host")
                    break
                found = read(current)
                if found.point:
                    return found
                if found.words and _host(current) not in SHORT_HOSTS:
                    reading = found
    except httpx.HTTPError as exc:
        log.warning("maps link: Google could not be reached: %s", type(exc).__name__)
        raise MapsLinkFailed("We could not open that link just now; please try again, or type the address instead") from exc
    return reading


def unreadable() -> MapsLinkUnreadable:
    return MapsLinkUnreadable("We could not find the place in that link. Open it in Google Maps, check it shows your business, and copy the link again; or type the address instead.")
