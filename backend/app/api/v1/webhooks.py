"""Admin endpoints for outgoing webhooks (blueprint §14: ``/v1/admin/webhooks``). Owners change them, managers can look."""
from __future__ import annotations

import secrets
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select

from app.api.deps import AuditAdmin, DbDep, Principal, get_tenant, require_role
from app.core.exceptions import Conflict, NotFound
from app.core.security import encrypt_field
from app.models import Tenant
from app.models.webhooks import WEBHOOK_EVENTS, WebhookDelivery, WebhookEndpoint
from app.schemas.webhooks import (
    DeliveryOut,
    PingResult,
    SecretOut,
    WebhookCreate,
    WebhookCreated,
    WebhookOut,
    WebhookUpdate,
)
from app.services import webhooks as webhook_service

_staff = require_role("staff")
_manager = require_role("manager")
_owner = require_role("owner")
router = APIRouter(prefix="/admin", tags=["webhooks"], dependencies=[Depends(_staff), AuditAdmin])
Manager = Annotated[Principal, Depends(_manager)]
Owner = Annotated[Principal, Depends(_owner)]
TenantDep = Annotated[Tenant, Depends(get_tenant)]


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


async def _endpoint(db: DbDep, endpoint_id: uuid.UUID) -> WebhookEndpoint:
    endpoint = await db.get(WebhookEndpoint, endpoint_id)
    if endpoint is None:
        raise NotFound("Webhook not found")
    return endpoint


@router.get("/webhook-events")
async def webhook_events(_: Manager) -> list[str]:
    """The event types a webhook can subscribe to (``"*"`` subscribes to all of them)."""
    return list(WEBHOOK_EVENTS)


@router.get("/webhooks", response_model=list[WebhookOut])
async def list_webhooks(_: Manager, db: DbDep) -> list[WebhookEndpoint]:
    return list((await db.scalars(select(WebhookEndpoint).order_by(WebhookEndpoint.created_at, WebhookEndpoint.id))).all())


@router.post("/webhooks", response_model=WebhookCreated, status_code=201)
async def create_webhook(body: WebhookCreate, _: Owner, db: DbDep, tenant: TenantDep) -> WebhookCreated:
    """Subscribe a URL. The signing secret is in this response only; keep it to verify signatures."""
    await webhook_service.check_url(body.url)
    if (await db.scalar(select(func.count()).select_from(WebhookEndpoint)) or 0) >= webhook_service.MAX_ENDPOINTS:
        raise Conflict(f"At most {webhook_service.MAX_ENDPOINTS} webhooks can be registered; remove one first")
    secret = new_secret()
    endpoint = WebhookEndpoint(
        tenant_id=tenant.id, url=body.url, description=body.description, events=body.events, secret_enc=encrypt_field(secret, tenant.id),
    )
    db.add(endpoint)
    await db.flush()
    await db.refresh(endpoint)
    return WebhookCreated.model_validate({**WebhookOut.model_validate(endpoint).model_dump(), "secret": secret})


@router.patch("/webhooks/{webhook_id}", response_model=WebhookOut)
async def update_webhook(webhook_id: uuid.UUID, body: WebhookUpdate, _: Owner, db: DbDep) -> WebhookEndpoint:
    """Change the URL, the events or the description, or switch the endpoint off (``active: false``) or back on."""
    endpoint = await _endpoint(db, webhook_id)
    changes = body.model_dump(exclude_unset=True)
    if "url" in changes:
        await webhook_service.check_url(changes["url"])
        endpoint.url = changes["url"]
    if "events" in changes:
        endpoint.events = changes["events"]
    if "description" in changes:
        endpoint.description = changes["description"]
    if changes.get("active") is False and endpoint.active:
        await webhook_service.disable_endpoint(db, endpoint, "switched off by an administrator")
    elif changes.get("active") is True and not endpoint.active:
        await webhook_service.check_url(endpoint.url)  # its address may have changed while it was off
        endpoint.active, endpoint.disabled_reason, endpoint.consecutive_failures = True, None, 0
    await db.flush()
    return endpoint


@router.delete("/webhooks/{webhook_id}", status_code=204)
async def delete_webhook(webhook_id: uuid.UUID, _: Owner, db: DbDep) -> Response:
    await db.delete(await _endpoint(db, webhook_id))
    await db.flush()
    return Response(status_code=204)


@router.post("/webhooks/{webhook_id}/rotate-secret", response_model=SecretOut)
async def rotate_secret(webhook_id: uuid.UUID, _: Owner, db: DbDep, tenant: TenantDep) -> SecretOut:
    """Issue a new signing secret (shown once). The old one stops working at once, also for deliveries that are being retried."""
    endpoint = await _endpoint(db, webhook_id)
    secret = new_secret()
    endpoint.secret_enc = encrypt_field(secret, tenant.id)
    await db.flush()
    return SecretOut(secret=secret)


@router.post("/webhooks/{webhook_id}/test", response_model=PingResult)
async def test_webhook(webhook_id: uuid.UUID, _: Owner, db: DbDep) -> PingResult:
    """Send one signed ``webhook.test`` request now and report the answer. Not recorded as a delivery."""
    result = await webhook_service.send_test(await _endpoint(db, webhook_id))
    return PingResult(delivered=result.ok, status_code=result.status, error=result.error)


@router.get("/webhooks/{webhook_id}/deliveries", response_model=list[DeliveryOut])
async def list_deliveries(
    webhook_id: uuid.UUID, _: Manager, db: DbDep, status: Literal["pending", "delivered", "failed"] | None = None,
    limit: int = 100, offset: int = 0,
) -> list[WebhookDelivery]:
    """What was sent to this webhook, newest first: outcome, attempts and the receiver's HTTP status (never its body)."""
    await _endpoint(db, webhook_id)
    q = (
        select(WebhookDelivery).where(WebhookDelivery.endpoint_id == webhook_id)
        .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id).limit(max(1, min(limit, 500))).offset(max(0, offset))
    )
    if status:
        q = q.where(WebhookDelivery.status == status)
    return list((await db.scalars(q)).all())


@router.post("/webhooks/{webhook_id}/deliveries/{delivery_id}/redeliver", response_model=DeliveryOut)
async def redeliver(webhook_id: uuid.UUID, delivery_id: uuid.UUID, _: Owner, db: DbDep) -> WebhookDelivery:
    """Try a failed delivery again from the start (the sweep sends it on its next pass). The endpoint must be active."""
    endpoint = await _endpoint(db, webhook_id)
    delivery = await db.get(WebhookDelivery, delivery_id)
    if delivery is None or delivery.endpoint_id != webhook_id:
        raise NotFound("Delivery not found")
    if delivery.status != "failed":
        raise Conflict(f"Only a failed delivery can be redelivered (this one is {delivery.status})")
    if not endpoint.active:
        raise Conflict("The webhook is switched off; switch it on first")
    delivery.status, delivery.attempts, delivery.error = "pending", 0, None
    delivery.next_attempt_at = func.clock_timestamp()
    await db.flush()
    await db.refresh(delivery)
    return delivery
