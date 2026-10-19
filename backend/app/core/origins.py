"""Website addresses: the one host a business's site is on, and the browser origins derived from it.

A browser says which page it is calling from in the ``Origin`` header (``https://www.shop.example``). A site's ``allowed_origins`` is the list the publishable key is
accepted from (``app.api.deps.resolve_tenant``). On a plan that limits domains the list is *derived* from the website address the owner gave, so the www and non-www
spellings are one site and nothing else can be added:

* :func:`host_of` turns whatever was typed (``https://WWW.Shop.example/book?x=1``, ``shop.example``, ``bücher.example``) into the canonical host ``shop.example``;
* :func:`derive_origins` gives ``https://shop.example`` and ``https://www.shop.example`` for it;
* :func:`origin_of` checks and canonicalises one origin of a free-form list (plans without a domain limit), keeping its scheme and port;
* ``localhost`` and ``127.0.0.1`` (:func:`is_dev_origin`) are always accepted as a browser's origin on a site that has a list: a page there can only run on the
  developer's own machine, and anyone can call the API without an ``Origin`` header anyway.

Everything here only reads text: nothing is looked up, so a host is *well-formed*, not *owned* (that would need a DNS or meta-tag check, which is not built).
"""
from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

DEV_HOSTS = frozenset({"localhost", "127.0.0.1"})
_DEV_ORIGIN = re.compile(r"^http://(?:localhost|127\.0\.0\.1)(?::\d{1,5})?$")
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


class WebsiteProblem(ValueError):
    """An address that cannot be used (the message says why, for the person who typed it)."""


def _parts(value: str) -> tuple[str, str, int | None]:
    """``(scheme, host, port)`` of what was typed; the scheme defaults to https. Raises :class:`WebsiteProblem`."""
    text = (value or "").strip()
    if not text:
        raise WebsiteProblem("The website address is empty")
    if len(text) > 300:
        raise WebsiteProblem("The website address is too long")
    if "://" not in text:
        text = "https://" + text
    try:
        parts = urlsplit(text)
        port = parts.port
        host = parts.hostname
    except ValueError as exc:
        raise WebsiteProblem(f"{value!r} is not a web address") from exc
    if parts.scheme not in ("http", "https"):
        raise WebsiteProblem("The website address must be an http or https address")
    if parts.username is not None or parts.password is not None:
        raise WebsiteProblem("The website address must not contain a user name or password")
    if not host:
        raise WebsiteProblem(f"{value!r} has no host name")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise WebsiteProblem(f"{value!r} is not a valid host name") from exc
    host = host.lower().rstrip(".")
    if not host:
        raise WebsiteProblem(f"{value!r} has no host name")
    return parts.scheme, host, port


def _public_host(host: str) -> str:
    """Check a host name is a public-looking one (not an IP address, not ``localhost``, with a dot, well-formed labels)."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise WebsiteProblem("Give the website's name (shop.example), not an IP address")
    if "." not in host or len(host) > 253:  # (``localhost`` has no dot; ``127.0.0.1`` was an IP address above)
        raise WebsiteProblem("Give the website's public name, such as shop.example")
    labels = host.split(".")
    if not all(_LABEL.match(label) for label in labels) or labels[-1].isdigit():
        raise WebsiteProblem(f"{host!r} is not a valid host name")
    return host


def host_key(host: str) -> str:
    """A host as sites are compared: lower case, without a leading ``www.`` (``www.shop.example`` and ``shop.example`` are one site)."""
    host = host.strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") and host.count(".") >= 2 else host


def host_of(value: str, *, public: bool = True) -> str:
    """The canonical host of a website address typed by the owner: no scheme, port, path or ``www.``.

    ``public`` (the default, for a plan that limits domains) insists on a public website name: a port, an IP address, ``localhost`` or a name with no dot raise
    :class:`WebsiteProblem`. Without it (the address only recorded for a business on a plan with no domain limit) a development host such as ``localhost:8080`` is
    accepted too, with its port dropped; a host name that is not well-formed is refused either way.
    """
    _, host, port = _parts(value)
    if public:
        if port is not None:
            raise WebsiteProblem("Give the website address without a port")
        return host_key(_public_host(host))
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not all(_LABEL.match(label) for label in host.split(".")):
            raise WebsiteProblem(f"{host!r} is not a valid host name") from None
    return host_key(host)


def derive_origins(hosts: Iterable[str]) -> list[str]:
    """The browser origins of these hosts: ``https://host`` and ``https://www.host`` for each."""
    out: set[str] = set()
    for host in hosts:
        key = host_key(host)
        out.update((f"https://{key}", f"https://www.{key}"))
    return sorted(out)


def origin_of(value: str) -> str:
    """One origin of a free-form list, canonical (``https://Shop.example:443/`` -> ``https://shop.example``). The scheme must be written; a path is refused."""
    text = (value or "").strip()
    if "://" not in text:
        raise WebsiteProblem(f"{value!r} is not an origin: write it as https://shop.example")
    scheme, host, port = _parts(text)
    parts = urlsplit(text)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise WebsiteProblem(f"{value!r} is not an origin: leave out the path")
    if (scheme, port) in (("https", 443), ("http", 80)):
        port = None
    shown = f"[{host}]" if ":" in host else host
    return f"{scheme}://{shown}" + (f":{port}" if port is not None else "")


def host_of_origin(origin: str) -> str | None:
    """The host-key of a stored origin, or ``None`` if it has none (older rows were stored as typed)."""
    try:
        host = urlsplit(origin).hostname
    except ValueError:
        return None
    return host_key(host) if host else None


def is_dev_origin(origin: str) -> bool:
    """``http://localhost`` / ``http://127.0.0.1`` with any port: a page on the developer's own machine."""
    return bool(_DEV_ORIGIN.match(origin))


def hosts_of(domain: str | None, origins: Iterable[str]) -> frozenset[str]:
    """The sites' hosts as compared: the domain and the host of every origin, without ``www.``, leaving out the developer's own machine."""
    found = {host_key(domain)} if domain else set()
    for origin in origins:
        host = host_of_origin(origin)
        if host:
            found.add(host)
    return frozenset(found - DEV_HOSTS)


def site_url_host(value: str | None) -> str | None:
    """The host-key of an address a plugin reported (lenient: ``None`` for anything unreadable)."""
    if not value:
        return None
    try:
        text = value.strip()
        host = urlsplit(text if "://" in text else "https://" + text).hostname
    except ValueError:
        return None
    return host_key(host) if host else None
