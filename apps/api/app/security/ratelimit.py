"""Simple Redis fixed-window rate limiter (per IP / per key).

Milestone 6 adds token buckets for message sending; this covers the
unauthenticated invite endpoints (ARCHITECTURE.md §11.5).
"""

from __future__ import annotations

import time

from fastapi import Request
from redis.asyncio import Redis

from app.errors import ApiError


def client_ip(request: Request) -> str:
    # Behind a trusted proxy, configure uvicorn --proxy-headers so this is the real IP.
    return request.client.host if request.client else "unknown"


async def hit(redis: Redis, key: str, limit: int, window_s: int) -> bool:
    """Count one hit; True if still within the limit."""
    bucket = f"rl:{key}:{int(time.time()) // window_s}"
    async with redis.pipeline(transaction=True) as p:
        p.incr(bucket)
        p.expire(bucket, window_s + 1)
        count, _ = await p.execute()
    return int(count) <= limit


async def limit_ip(request: Request, redis: Redis, scope: str, limit: int, window_s: int) -> None:
    if not await hit(redis, f"{scope}:{client_ip(request)}", limit, window_s):
        raise ApiError(429, "rate_limited", "Too many attempts. Wait a minute and try again.")
