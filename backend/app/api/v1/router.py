"""Aggregates all v1 sub-routers."""
from fastapi import APIRouter

from app.api.v1 import admin, auth, calendar, captcha, countries, owner, platform, public, signup, sync, webhooks

api_router = APIRouter(prefix="/v1")
api_router.include_router(public.router)
api_router.include_router(admin.router)
api_router.include_router(webhooks.router)
api_router.include_router(calendar.router)
api_router.include_router(calendar.callback_router)
api_router.include_router(sync.router)
api_router.include_router(auth.router)
api_router.include_router(auth.otp_router)
api_router.include_router(countries.router)
api_router.include_router(captcha.router)
api_router.include_router(platform.router)
api_router.include_router(signup.router)
api_router.include_router(owner.router)
