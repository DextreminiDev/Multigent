"""Per-chat Redis lock with heartbeat (ARCHITECTURE.md §6.3).

Value = the turn's fencing token. Extend/release are compare-and-set Lua scripts,
so a worker can never extend or delete a lock someone else now holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

from redis.asyncio import Redis

LOCK_TTL_MS = 30_000
HEARTBEAT_S = 10.0

_EXTEND = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def lock_key(chat_id: uuid.UUID | str) -> str:
    return f"chat:{chat_id}:lock"


class ChatLock:
    def __init__(
        self,
        redis: Redis,
        chat_id: uuid.UUID,
        token: uuid.UUID,
        ttl_ms: int = LOCK_TTL_MS,
        heartbeat_s: float = HEARTBEAT_S,
    ) -> None:
        self.redis = redis
        self.key = lock_key(chat_id)
        self.token = str(token)
        self.ttl_ms = ttl_ms
        self.heartbeat_s = heartbeat_s
        self.lost = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def acquire(self) -> bool:
        return bool(await self.redis.set(self.key, self.token, nx=True, px=self.ttl_ms))

    async def extend(self) -> bool:
        return bool(await self.redis.eval(_EXTEND, 1, self.key, self.token, self.ttl_ms))

    async def release(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        with contextlib.suppress(Exception):
            await self.redis.eval(_RELEASE, 1, self.key, self.token)

    def start_heartbeat(self) -> None:
        async def beat() -> None:
            while True:
                await asyncio.sleep(self.heartbeat_s)
                try:
                    ok = await self.extend()
                except Exception:
                    ok = False
                if not ok:
                    self.lost.set()
                    return

        self._task = asyncio.create_task(beat())
