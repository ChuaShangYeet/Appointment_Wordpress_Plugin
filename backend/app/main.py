"""FastAPI application factory."""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.database import dispose_engine, get_engine
from app.core import hardening, logs, observability, ratelimit
from app.core.exceptions import DomainError
from app.core.redis import get_redis
from app.services import captcha, delivery, geocoding

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.environment != "test":  # (tests keep pytest's own capture)
        logs.configure_logging(settings.log_level, logs.resolve_format(settings))
    observability.init_sentry(settings)
    await hardening.enforce(get_settings(), get_engine())  # outside development: refuse to run with tenant isolation off or placeholder secrets
    delivery.configure_providers(get_settings())  # sign-in codes and links go out through these
    geocoding.configure(get_settings())  # typed addresses are looked up through this
    captcha.configure(get_settings())  # sign-up and booking are checked through this
    delivery.report_configuration(get_settings())  # outside development: say loudly what is missing
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    settings = get_settings()
    docs = settings.docs_enabled if settings.docs_enabled is not None else settings.environment in hardening.RELAXED_ENVIRONMENTS
    app = FastAPI(
        title="JustSimple Booking Engine", version="0.1.0", lifespan=lifespan,
        docs_url="/docs" if docs else None, redoc_url="/redoc" if docs else None, openapi_url="/openapi.json" if docs else None,
    )

    @app.middleware("http")
    async def limit_by_address(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Per-address rate limit, before any database work (added first = inside the CORS layer below, so refusals carry CORS headers)."""
        bucket = ratelimit.classify(request.method, request.url.path) if get_settings().rate_limit_enabled else None
        decision = await ratelimit.check_ip(request, bucket) if bucket else None
        if decision is not None and not decision.allowed:
            error = ratelimit.RateLimited(decision)
            return JSONResponse(status_code=error.status_code, content={"error": error.code, "detail": error.message, **error.extra}, headers=error.headers)
        response = await call_next(request)
        if decision is not None:
            for name, value in ratelimit.headers_for(decision).items():
                response.headers[name] = value
        return response

    # Coarse global allowlist; the per-site allowlist is enforced in ``resolve_tenant``.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[observability.REQUEST_ID_HEADER, "Idempotent-Replayed", "Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset"],
    )
    app.add_middleware(observability.RequestContextMiddleware)  # outermost: its request id covers everything below, CORS refusals included

    @app.exception_handler(DomainError)
    async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.code, "detail": exc.message, **exc.extra}, headers=exc.headers
        )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        """Can this instance serve? 503 only when the database is down; a Redis outage is reported as ``degraded`` (the API fails open without it)."""
        checks = {"database": "ok", "redis": "ok"}
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - any failure means not ready
            log.warning("readiness: database check failed: %s", type(exc).__name__)
            checks["database"] = "down"
        try:
            await get_redis().ping()
        except Exception as exc:  # noqa: BLE001
            log.warning("readiness: redis check failed: %s", type(exc).__name__)
            checks["redis"] = "down"
        status = "ok" if set(checks.values()) == {"ok"} else ("degraded" if checks["database"] == "ok" else "unavailable")
        return JSONResponse(status_code=200 if checks["database"] == "ok" else 503, content={"status": status, **checks})

    app.include_router(api_router)
    return app


app = create_app()
