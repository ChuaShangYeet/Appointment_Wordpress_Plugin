"""WordPress webhook sync. Requests are HMAC-SHA256 signed with the site's webhook secret.

Headers: ``X-Site-Key`` (publishable key, identifies the site), ``X-Timestamp`` (unix seconds),
``X-Signature`` = hex HMAC-SHA256 over ``"{timestamp}.{raw body}"``.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field

from app.api.deps import DbDep, resolve_tenant
from app.core.exceptions import Unauthorized, ValidationFailed
from app.core.security import decrypt_field, verify_signature
from app.models import Site
from app.services import sites as sites_service

router = APIRouter(prefix="/sync", tags=["sync"], dependencies=[Depends(resolve_tenant)])


class SyncEvent(BaseModel):
    event: Literal["site.updated"]
    data: dict[str, Any] = Field(default_factory=dict)


@router.post("/webhook")
async def receive(
    request: Request, db: DbDep,
    x_timestamp: Annotated[str, Header()], x_signature: Annotated[str, Header()],
) -> dict[str, str]:
    """Apply a signed event from the WordPress connector (currently ``site.updated``)."""
    site: Site | None = request.state.site
    if request.state.key_kind != "publishable" or site is None or site.webhook_secret_enc is None:
        raise Unauthorized("Site key required")
    body = await request.body()
    verify_signature(decrypt_field(site.webhook_secret_enc, site.tenant_id), x_timestamp, body, x_signature)
    event = SyncEvent.model_validate_json(body)

    live = await db.get(Site, site.id)  # sites table is not tenant-scoped by RLS
    if live is None:
        raise ValidationFailed("Unknown site")
    domain: Any = sites_service.KEEP
    if "domain" in event.data:
        domain = event.data["domain"]
        if domain is not None and not isinstance(domain, str):
            raise ValidationFailed("domain must be a string")
    given: Any = None
    if "allowed_origins" in event.data:
        given = event.data["allowed_origins"]
        if not isinstance(given, list) or not all(isinstance(o, str) for o in given) or len(given) > 50:
            raise ValidationFailed("allowed_origins must be a list of at most 50 strings")
    # the plan's rules apply: on the free plan one domain (www and non-www are one), and the address may change once every 30 days (402 otherwise, nothing changes)
    await sites_service.update_site(db, request.state.tenant, live, domain=domain, allowed_origins=given)
    return {"status": "ok"}
