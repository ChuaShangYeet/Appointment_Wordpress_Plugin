"""Shared Redis client: sign-in codes and links, sign-up and calendar tokens, rate-limit counters and the availability cache."""
from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import get_settings

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Shared Redis client (lazy). Short timeouts: a slow Redis must not make requests slow."""
    global _redis
    if _redis is None:
        s = get_settings()
        _redis = aioredis.from_url(
            s.redis_url, decode_responses=True, socket_connect_timeout=s.redis_timeout_seconds, socket_timeout=s.redis_timeout_seconds
        )
    return _redis
