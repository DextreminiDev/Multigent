"""Worker entrypoint: `python -m app.worker`.

Milestone 1: process skeleton only. It runs a scheduler tick loop that records a
heartbeat in Redis (used by `python -m app.worker --check` as the container
healthcheck) and shuts down gracefully on SIGINT/SIGTERM. Claiming, turns and the
sweeper arrive in milestones 3 and 5 (ARCHITECTURE.md §6).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys
import time

from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.ids import uuid7
from app.logging import configure_logging, get_logger

HEARTBEAT_KEY = "worker:{worker_id}:tick"
HEARTBEAT_INDEX = "workers:alive"
HEARTBEAT_TTL_S = 10
TICK_S = 0.2

log = get_logger("worker")


class Worker:
    def __init__(self, settings: Settings, redis: Redis) -> None:
        self.settings = settings
        self.redis = redis
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid7().hex[-6:]}"
        self._stop = asyncio.Event()
        self._last_beat = 0.0

    def stop(self) -> None:
        self._stop.set()

    async def heartbeat(self) -> None:
        now = time.time()
        if now - self._last_beat < 2:
            return
        self._last_beat = now
        async with self.redis.pipeline(transaction=False) as p:
            p.set(HEARTBEAT_KEY.format(worker_id=self.worker_id), int(now), ex=HEARTBEAT_TTL_S)
            p.zadd(HEARTBEAT_INDEX, {self.worker_id: now})
            p.zremrangebyscore(HEARTBEAT_INDEX, "-inf", now - HEARTBEAT_TTL_S)
            await p.execute()

    async def tick(self) -> None:
        """One scheduler iteration. Placeholder until milestone 3."""
        await self.heartbeat()

    async def run(self) -> None:
        log.info("worker.started", worker_id=self.worker_id)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("worker.tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), TICK_S)
        await self.redis.zrem(HEARTBEAT_INDEX, self.worker_id)
        await self.redis.delete(HEARTBEAT_KEY.format(worker_id=self.worker_id))
        log.info("worker.stopped", worker_id=self.worker_id)


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.env != "development")
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    worker = Worker(settings, redis)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await redis.aclose()


async def _check() -> int:
    """Healthcheck: exit 0 if any worker heartbeat is fresh."""
    redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    try:
        fresh = await redis.zcount(HEARTBEAT_INDEX, time.time() - HEARTBEAT_TTL_S, "+inf")
        return 0 if fresh > 0 else 1
    finally:
        await redis.aclose()


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(asyncio.run(_check()))
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())
