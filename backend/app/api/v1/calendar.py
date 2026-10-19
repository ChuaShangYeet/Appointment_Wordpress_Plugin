"""Google Calendar sync: connect a calendar (owner), see the connections (manager), and Google's OAuth callback. See :mod:`app.services.calendar`."""
from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select

from app.api.deps import AuditAdmin, DbDep, Principal, get_tenant, require_role
from app.core.config import get_settings
from app.core.exceptions import Conflict, NotFound
from app.models import Resource, Tenant
from app.models.calendar import CalendarConnection, CalendarEvent
from app.schemas.calendar import (
    CalendarConnectIn,
    CalendarConnectionOut,
    CalendarConnectionUpdate,
    CalendarConnectOut,
    ResyncOut,
)
from app.services import calendar

_staff = require_role("staff")
_manager = require_role("manager")
_owner = require_role("owner")
router = APIRouter(prefix="/admin/calendar", tags=["calendar"], dependencies=[Depends(_staff), AuditAdmin])
Manager = Annotated[Principal, Depends(_manager)]
Owner = Annotated[Principal, Depends(_owner)]
TenantDep = Annotated[Tenant, Depends(get_tenant)]
#: Google's own redirect, which carries no key: the one-time ``state`` we gave it is the credential.
callback_router = APIRouter(prefix="/calendar", tags=["calendar"])


async def _connection(db: DbDep, connection_id: uuid.UUID) -> CalendarConnection:
    connection = await db.get(CalendarConnection, connection_id)
    if connection is None:
        raise NotFound("Calendar connection not found")
    return connection


async def _out(db: DbDep, connections: list[CalendarConnection]) -> list[CalendarConnectionOut]:
    if not connections:
        return []
    counts = {(cid, state): n for cid, state, n in (await db.execute(
        select(CalendarEvent.connection_id, CalendarEvent.state, func.count()).group_by(CalendarEvent.connection_id, CalendarEvent.state)
    )).all()}
    names = dict((await db.execute(select(Resource.id, Resource.name))).all())
    return [
        CalendarConnectionOut(
            id=c.id, resource_id=c.resource_id, resource_name=names.get(c.resource_id) if c.resource_id else None, calendar_id=c.calendar_id,
            account_label=c.account_label, status=c.status, share_customer_details=c.share_customer_details, last_sync_at=c.last_sync_at,
            last_error=c.last_error, pending=counts.get((c.id, "pending"), 0), synced=counts.get((c.id, "synced"), 0), failed=counts.get((c.id, "failed"), 0),
        )
        for c in connections
    ]


@router.get("/connections", response_model=list[CalendarConnectionOut])
async def list_connections(_: Manager, db: DbDep) -> list[CalendarConnectionOut]:
    """The connected calendars: the business's own and any staff member's, with how their sync is going. Never a token."""
    return await _out(db, list((await db.scalars(select(CalendarConnection).order_by(CalendarConnection.created_at, CalendarConnection.id))).all()))


@router.post("/connect", response_model=CalendarConnectOut)
async def connect(body: CalendarConnectIn, principal: Owner, db: DbDep) -> CalendarConnectOut:
    """Owner. Start connecting a Google calendar: returns the address to send the owner's browser to (valid for ten minutes). After they allow access Google sends the
    browser to the server, which stores the connection (encrypted), queues the bookings that belong in it and redirects to ``CALENDAR_DONE_URL``. Connecting again
    replaces the saved sign-in of the same calendar. 503 ``calendar_not_configured`` when the server has no Google OAuth client."""
    url = await calendar.begin_authorization(db, principal.tenant_id, resource_id=body.resource_id, calendar_id=body.calendar_id, share_customer_details=body.share_customer_details)
    return CalendarConnectOut(authorization_url=url)


@router.patch("/connections/{connection_id}", response_model=CalendarConnectionOut)
async def update_connection(connection_id: uuid.UUID, body: CalendarConnectionUpdate, _: Owner, db: DbDep) -> CalendarConnectionOut:
    """Owner. Share (or stop sharing) the customer's details with this calendar; every event is written again to match."""
    connection = await _connection(db, connection_id)
    connection.share_customer_details = body.share_customer_details
    await calendar.mark_upcoming(db, connection, force=True)
    await db.flush()
    return (await _out(db, [connection]))[0]


@router.post("/connections/{connection_id}/resync", response_model=ResyncOut)
async def resync(connection_id: uuid.UUID, _: Owner, db: DbDep) -> ResyncOut:
    """Owner. Write every event of this calendar again (and give up-and-failed ones another chance). 409 while the connection is paused: connect it again first."""
    connection = await _connection(db, connection_id)
    if connection.status != "active":
        raise Conflict("This calendar needs to be connected again first (POST /admin/calendar/connect)")
    return ResyncOut(queued=await calendar.mark_upcoming(db, connection, force=True))


@router.delete("/connections/{connection_id}", status_code=204)
async def disconnect(connection_id: uuid.UUID, _: Owner, db: DbDep) -> Response:
    """Owner. Disconnect the calendar: Google is told to revoke the saved sign-in and the connection is forgotten. Events already in the calendar stay there."""
    await calendar.disconnect(db, await _connection(db, connection_id))
    return Response(status_code=204)


@callback_router.get("/oauth/callback")
async def oauth_callback(
    code: Annotated[str | None, Query(max_length=2048)] = None, state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=200)] = None,
) -> Response:
    """Where Google sends the browser after the owner has allowed (or refused) access. Redirects to ``CALENDAR_DONE_URL`` with ``calendar=connected`` or
    ``calendar=error&reason=...`` (``invalid_state``, ``denied``, ``google_error``, ``google_refused``, ``google_unavailable``, ``no_refresh_token``, ``resource_gone``);
    without that setting it answers with JSON."""
    done = await calendar.complete_authorization(code=code, state=state, error=error)
    target = get_settings().calendar_done_url
    if target:
        query = urlencode({"calendar": "connected"} if done.ok else {"calendar": "error", "reason": done.reason or "error"})
        return RedirectResponse(f"{target}{'&' if '?' in target else '?'}{query}", status_code=303)
    if done.ok:
        return JSONResponse({"status": "connected"})
    return JSONResponse({"status": "error", "reason": done.reason}, status_code=400)
