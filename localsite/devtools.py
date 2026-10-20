"""Local test site only: stand-ins for the outside services, and a few /dev routes the test pages use.

Nothing here is part of the product. The backend runs unchanged; this module only swaps what it would send to the outside world:

* messages (WhatsApp, SMS, email) go into an in-memory inbox that the page shows, instead of a log line;
* Redis is the in-memory fake the tests use (fakeredis), so nothing has to be installed;
* typed addresses are looked up in a small table of Klang Valley places instead of Google.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
from collections import deque
from pathlib import Path
from typing import Any, Protocol

import fakeredis
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel

import app.core.redis as redis_module
from app.services import delivery, geocoding

# ------------------------------------------------------------------ messages
INBOX: deque[dict[str, Any]] = deque(maxlen=300)
_seq = [0]


class InboxProvider:
    """Every channel: the message is kept for the page to show (newest last). Nothing is sent."""

    def __init__(self, channel: str) -> None:
        self.name = f"local-{channel}"
        self.channel = channel

    async def send(self, message: delivery.Message) -> str | None:
        _seq[0] += 1
        body = message.body
        if message.external_template and message.params:  # a WhatsApp template: show its variables, the code first
            body = body or f"[{message.external_template}] " + " ".join(message.params)
        INBOX.append({
            "id": _seq[0], "channel": message.channel, "to": message.recipient, "subject": message.subject, "body": body,
            "sent_at": dt.datetime.now(dt.UTC).isoformat(),
        })
        return f"local-{_seq[0]}"


def install_inbox() -> None:
    for channel in ("email", "sms", "whatsapp"):
        delivery.set_provider(channel, InboxProvider(channel))


# ------------------------------------------------------------------ Redis
def install_memory_redis() -> None:
    redis_module._redis = fakeredis.aioredis.FakeRedis(decode_responses=True)


# ------------------------------------------------------------------ typed addresses
# (place pattern, latitude, longitude): rough centres, good enough to measure distance from the sample home-services business in SS 2, Petaling Jaya
PLACES: list[tuple[str, float, float]] = [
    (r"seremban", 2.7258, 101.9424), (r"melaka|malacca", 2.1896, 102.2501), (r"ipoh", 4.5975, 101.0901), (r"johor", 1.4927, 103.7414),
    (r"penang|george ?town", 5.4141, 100.3288), (r"genting", 3.4236, 101.7932), (r"klang", 3.0449, 101.4456), (r"putrajaya", 2.9264, 101.6964),
    (r"cyberjaya", 2.9213, 101.6559), (r"shah alam", 3.0733, 101.5185), (r"cheras", 3.0851, 101.7465), (r"puchong", 3.0280, 101.6170),
    (r"subang|usj|ss ?15", 3.0438, 101.5806), (r"mont kiara", 3.1700, 101.6519), (r"bangsar", 3.1300, 101.6710),
    (r"kl sentral|kuala lumpur|\bkl\b|bukit bintang|kia peng", 3.1478, 101.7101), (r"ttdi|taman tun", 3.1361, 101.6293),
    (r"damansara", 3.1365, 101.6230), (r"ss ?2\b", 3.1186, 101.6216), (r"petaling jaya|\bpj\b|ss ?\d+|seksyen|section", 3.1073, 101.6067),
]


class LocalGeocoder:
    """Looks a typed address up in :data:`PLACES`. Exact when it has a house number and at least three words, like a street address."""

    name = "local"

    async def geocode(self, address: str, region: str | None = None) -> geocoding.Location | None:
        text = address.strip()
        if len(text) < 3 or re.search(r"asdf|qwerty|nowhere", text, re.I):
            return None
        for pattern, lat, lon in PLACES:
            if re.search(pattern, text, re.I):
                break
        else:
            lat, lon = 3.1390, 101.6869  # somewhere in Kuala Lumpur
        exact = bool(re.search(r"\d", text)) and len(re.split(r"[\s,]+", text)) >= 3
        formatted = re.sub(r"\s+", " ", text).rstrip(", ")
        if not re.search(r"malaysia$", formatted, re.I):
            formatted += ", Malaysia"
        return geocoding.Location(latitude=lat, longitude=lon, formatted_address=formatted, exact=exact)


def install_geocoder() -> None:
    """The API's start-up calls ``geocoding.configure``, which would clear the provider: make it install this one instead."""
    geocoding.set_provider(LocalGeocoder())
    geocoding.configure = lambda settings: geocoding.set_provider(LocalGeocoder())  # type: ignore[assignment]


# ------------------------------------------------------------------ /dev routes for the test pages
class TokenAsk(BaseModel):
    business: str
    role: str = "owner"
    resource_id: str | None = None
    name: str | None = None


class NewBusiness(BaseModel):
    tenant_id: str
    name: str
    business_type: str
    publishable_key: str
    secret_key: str


class FileStore:
    """The test pages' list of businesses and their keys, kept in a JSON file (the local test site: ``data/local.json``)."""

    hosted = False

    def __init__(self, path: Path) -> None:
        self.path = path

    async def load(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf8"))

    async def save(self, data: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(data, indent=2), encoding="utf8")


class Store(Protocol):
    hosted: bool

    async def load(self) -> dict[str, Any]: ...

    async def save(self, data: dict[str, Any]) -> None: ...


def dev_router(app: FastAPI, store: Store, guard: Any = None) -> APIRouter:
    """``guard``: a FastAPI dependency every route needs (the hosted demo's sign-in), or none (the local test site)."""
    import httpx
    from fastapi import Depends

    router = APIRouter(prefix="/dev", tags=["test pages"], dependencies=[Depends(guard)] if guard else [])
    writing = asyncio.Lock()  # (several visitors may change the list at once on a hosted demo)

    @router.get("/config")
    async def config() -> dict[str, Any]:
        """The sample businesses with their public keys (never the secret keys), and the sample customer."""
        data = await store.load()
        return {
            "businesses": [{k: v for k, v in b.items() if k != "secret_key"} for b in data["businesses"]],
            "sample_customer": data["sample_customer"], "hosted": store.hosted,
        }

    @router.post("/businesses")
    async def add_business(body: NewBusiness) -> dict[str, str]:
        """A business made on the sign-up page: remember it so the admin pages can open it (keys are kept here, as a WordPress site would)."""
        async with writing:
            data = await store.load()
            if not any(b["tenant_id"] == body.tenant_id for b in data["businesses"]):
                data["businesses"].append({
                    "id": "new-" + body.tenant_id[:8], "tenant_id": body.tenant_id, "name": body.name, "type": body.business_type,
                    "publishable_key": body.publishable_key, "secret_key": body.secret_key, "plan": "free", "is_new": True,
                })
                await store.save(data)
        return {"status": "ok"}

    @router.put("/businesses/{business_id}")
    async def update_business(business_id: str, body: dict[str, Any]) -> dict[str, str]:
        """What the backend cannot be asked for (opening hours, which rule set is the default) and replaced keys: kept here for the test pages."""
        async with writing:
            data = await store.load()
            business = next((b for b in data["businesses"] if b["id"] == business_id), None)
            if business is None:
                raise HTTPException(404, "No such business")
            for key in ("hours", "default_policy_id", "secret_key", "publishable_key"):
                if key in body:
                    business[key] = body[key]
            await store.save(data)
        return {"status": "ok"}

    @router.post("/admin-token")
    async def admin_token(body: TokenAsk) -> dict[str, Any]:
        """Stands in for the WordPress plugin: exchanges the business's secret key for an admin token for a user of the given role."""
        business = next((b for b in (await store.load())["businesses"] if b["id"] == body.business), None)
        if business is None:
            raise HTTPException(404, "No such business")
        user_id = {"owner": "owner-1", "manager": "manager-1"}.get(body.role, f"staff-{body.resource_id or 'all'}")
        payload: dict[str, Any] = {"user_id": user_id, "role": body.role, "display_name": body.name or body.role.title()}
        if body.resource_id:
            payload["resource_id"] = body.resource_id
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            r = await client.post("/v1/auth/token", json=payload, headers={"X-API-Key": business["secret_key"]})
        if r.status_code != 200:
            raise HTTPException(r.status_code, r.text)
        return r.json()

    @router.get("/messages")
    async def messages(after: int = 0) -> list[dict[str, Any]]:
        """What the backend 'sent' (WhatsApp, SMS, email), newest last."""
        return [m for m in INBOX if m["id"] > after][-200:]

    return router


# ------------------------------------------------------------------ serving the test pages
SKELETON = (
    '<!doctype html><html lang="en"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">'
    "<style>:root{color-scheme:light;padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}"
    "body{margin:0;font:14px system-ui,sans-serif}img{max-width:100%}[hidden]{display:none!important}</style></head><body>{page}</body></html>"
)
#: The API answers carry "default-src 'none'" (they are never pages); the test pages need their own scripts, styles and fonts.
PAGE_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'")


def web_pages(directory: Path) -> Any:
    """The test pages (``web/``): index.html wrapped in a document, never cached, with a content policy that lets them run."""
    from starlette.responses import HTMLResponse, Response
    from starlette.staticfiles import StaticFiles
    from starlette.types import Scope

    class WebPages(StaticFiles):
        async def get_response(self, path: str, scope: Scope) -> Response:
            if path in ("", ".", "index.html"):
                response: Response = HTMLResponse(SKELETON.replace("{page}", (directory / "index.html").read_text(encoding="utf8")))
            else:
                response = await super().get_response(path, scope)
            response.headers["Content-Security-Policy"] = PAGE_CSP
            response.headers["Cache-Control"] = "no-store"
            return response

    return WebPages(directory=directory, html=True)
