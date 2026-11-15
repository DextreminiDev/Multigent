"""Redis client factory (one place for connection options)."""

from __future__ import annotations

from redis.asyncio import Redis
from redis.maint_notifications import MaintNotificationsConfig


def make_redis(url: str) -> Redis:
    # Maintenance notifications are a Redis Enterprise/Cloud feature; OSS Redis 7
    # answers "unknown subcommand" and redis-py logs a warning on every connect.
    return Redis.from_url(
        url,
        decode_responses=True,
        maint_notifications_config=MaintNotificationsConfig(enabled=False),
    )
