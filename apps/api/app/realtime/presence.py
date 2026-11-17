"""Presence + typing (ARCHITECTURE.md §8.5). Redis only, never persisted.

presence:{chat} is a sorted set of "user_id:conn_id" scored by last heartbeat.
A user is online if any of their connections beat within PRESENCE_TTL_S, so a
crashed API instance's entries simply age out. typing:{chat}:{user} has a 4 s TTL.
"""

from __future__ import annotations

import json
import time
import uuid

from redis.asyncio import Redis

from app.events import chat_channel

PRESENCE_TTL_S = 45
HEARTBEAT_S = 15
TYPING_TTL_S = 4


def presence_key(chat_id: uuid.UUID | str) -> str:
    return f"presence:{chat_id}"


async def touch(redis: Redis, chat_id: uuid.UUID, user_id: uuid.UUID, conn_id: str) -> None:
    async with redis.pipeline(transaction=False) as p:
        p.zadd(presence_key(chat_id), {f"{user_id}:{conn_id}": time.time()})
        p.zremrangebyscore(presence_key(chat_id), "-inf", time.time() - PRESENCE_TTL_S)
        p.expire(presence_key(chat_id), PRESENCE_TTL_S * 2)
        await p.execute()


async def drop(redis: Redis, chat_id: uuid.UUID, user_id: uuid.UUID, conn_id: str) -> None:
    await redis.zrem(presence_key(chat_id), f"{user_id}:{conn_id}")


async def online_user_ids(redis: Redis, chat_id: uuid.UUID | str) -> set[str]:
    entries = await redis.zrangebyscore(presence_key(chat_id), time.time() - PRESENCE_TTL_S, "+inf")
    return {str(e).split(":", 1)[0] for e in entries}


async def broadcast_presence(redis: Redis, chat_id: uuid.UUID | str) -> list[str]:
    online = sorted(await online_user_ids(redis, chat_id))
    await redis.publish(
        chat_channel(uuid.UUID(str(chat_id))),
        json.dumps(
            {"type": "presence.update", "chat_id": str(chat_id), "payload": {"online": online}}
        ),
    )
    return online


async def set_typing(redis: Redis, chat_id: uuid.UUID, user_id: uuid.UUID, name: str) -> None:
    async with redis.pipeline(transaction=False) as p:
        p.set(f"typing:{chat_id}:{user_id}", "1", ex=TYPING_TTL_S)
        p.publish(
            chat_channel(chat_id),
            json.dumps(
                {
                    "type": "typing",
                    "chat_id": str(chat_id),
                    "payload": {"user_id": str(user_id), "name": name, "ttl_s": TYPING_TTL_S},
                }
            ),
        )
        await p.execute()
