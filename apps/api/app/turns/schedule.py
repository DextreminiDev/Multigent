"""Due-queue for turns (ARCHITECTURE.md §6.2, §6.5).

`chat:due` is a Redis sorted set: member = chat id, score = when a turn may
start (epoch ms). ZADD NX keeps the EARLIEST due time, so later messages inside
the debounce window don't push the start back, and a burst lands in one batch.
"""

from __future__ import annotations

import time
import uuid

from redis.asyncio import Redis

DUE_KEY = "chat:due"


def now_ms() -> int:
    return int(time.time() * 1000)


async def wake(redis: Redis, chat_id: uuid.UUID, delay_ms: int = 0) -> None:
    await redis.zadd(DUE_KEY, {str(chat_id): now_ms() + delay_ms}, nx=True)


async def wake_at_least(redis: Redis, chat_id: uuid.UUID, delay_ms: int) -> None:
    """Re-queue for later even if an earlier entry exists (used after lock contention)."""
    await redis.zadd(DUE_KEY, {str(chat_id): now_ms() + delay_ms}, gt=True)
    await redis.zadd(DUE_KEY, {str(chat_id): now_ms() + delay_ms}, nx=True)


async def due_chats(redis: Redis, limit: int) -> list[str]:
    rows = await redis.zrangebyscore(DUE_KEY, "-inf", now_ms(), start=0, num=limit)
    return [str(r) for r in rows]


async def claim_wakeup(redis: Redis, chat_id: str) -> bool:
    """Exactly one worker wins the ZREM for a given wake-up."""
    return bool(await redis.zrem(DUE_KEY, chat_id))
