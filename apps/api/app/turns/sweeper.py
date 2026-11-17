"""Sweeper (ARCHITECTURE.md §6.5): the I6 "Postgres is the truth" safety net.

Every few seconds the elected leader (one worker at a time, via a Redis lease)
re-schedules any chat that has work but no live lock:

1. runnable chats with pending human messages: covers lost wake-ups, a flushed or
   restarted Redis, and debounce entries that vanished;
2. chats with a `running` turn whose lock expired: the worker died (kill -9,
   OOM, node loss). The next claim fails that turn as `crash`, returns its batch to
   pending, and answers it again, exactly once.
"""

from __future__ import annotations

import time
import uuid

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Chat, ChatKey, Message, Turn
from app.logging import get_logger
from app.turns.lock import lock_key
from app.turns.schedule import wake

log = get_logger("sweeper")

LEADER_KEY = "sweeper:leader"
_RENEW = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""


class Sweeper:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        redis: Redis,
        node_id: str,
        interval_s: float = 5.0,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.redis = redis
        self.node_id = node_id
        self.interval_s = interval_s
        self.lease_ms = int(interval_s * 3 * 1000)
        self._last = 0.0

    async def is_leader(self) -> bool:
        if await self.redis.set(LEADER_KEY, self.node_id, nx=True, px=self.lease_ms):
            return True
        return bool(await self.redis.eval(_RENEW, 1, LEADER_KEY, self.node_id, self.lease_ms))

    async def resign(self) -> None:
        if await self.redis.get(LEADER_KEY) == self.node_id:
            await self.redis.delete(LEADER_KEY)

    async def maybe_sweep(self) -> int:
        now = time.monotonic()
        if now - self._last < self.interval_s:
            return 0
        self._last = now
        if not await self.is_leader():
            return 0
        return await self.sweep()

    async def sweep(self) -> int:
        async with self.sessionmaker() as db:
            pending = set(
                (
                    await db.scalars(
                        select(Message.chat_id)
                        .join(Chat, Chat.id == Message.chat_id)
                        .join(ChatKey, ChatKey.chat_id == Chat.id)
                        .where(
                            Message.status == "pending",
                            Message.kind == "human",
                            Chat.status == "active",
                            Chat.model.is_not(None),
                            ChatKey.status != "invalid",
                        )
                        .distinct()
                    )
                ).all()
            )
            running = set(
                (await db.scalars(select(Turn.chat_id).where(Turn.status == "running"))).all()
            )
        woke = 0
        for chat_id in pending | running:
            if await self.redis.exists(lock_key(chat_id)):
                continue  # someone is on it (their finish re-checks for pending work)
            await wake(self.redis, uuid.UUID(str(chat_id)), 0)
            woke += 1
            if chat_id in running:
                log.warning("sweeper.orphaned_turn", chat_id=str(chat_id))
        if woke:
            log.info("sweeper.rescheduled", chats=woke)
        return woke
