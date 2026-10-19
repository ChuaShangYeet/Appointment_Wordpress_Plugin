"""The owner signs in by email (no key: see :mod:`app.services.owner_login`)."""
from __future__ import annotations

from fastapi import APIRouter

from app.schemas.tenant import OwnerLoginRequest, OwnerLoginVerify, OwnerSession
from app.services import owner_login

router = APIRouter(prefix="/owner", tags=["owner"])


@router.post("/login", status_code=202)
async def request_login(body: OwnerLoginRequest) -> dict[str, str]:
    """Ask for a sign-in link. If the address is a business's owner address a link is mailed to it; the answer is ``202`` either way, so the form cannot be used to find
    out who is registered. 429 when asked again too soon, or too often in a day."""
    await owner_login.start(str(body.email))
    return {"status": "sent"}


@router.post("/login/verify", response_model=OwnerSession)
async def verify_login(body: OwnerLoginVerify) -> OwnerSession:
    """Exchange the token from the emailed link (once) for an owner admin token: the same token, powers and lifetime as the one the WordPress server gets from the secret key.
    A POST on purpose: a mail scanner that merely opens links would otherwise use the link up first."""
    return await owner_login.verify(body.token)
