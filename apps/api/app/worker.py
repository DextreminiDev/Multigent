"""Worker entrypoint: `python -m app.worker`.

Runs the scheduler loop (ARCHITECTURE.md §6.5): poll `chat:due`, win the ZREM
for a chat, and run its turn under the per-chat lock, bounded by
WORKER_MAX_CONCURRENT_TURNS. Also records a heartbeat in Redis
(`python -m app.worker --check` is the container healthcheck) and shuts down
gracefully: stop claiming, give in-flight turns SHUTDOWN_GRACE_S, then cancel
(their locks expire and the batch is recovered on the next claim/sweep).

The elected sweeper re-queues orphaned turns and lost wake-ups (app/turns/sweeper.py).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import sys
import time
import uuid

from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.db.session import make_engine, make_sessionmaker
from app.ids import uuid7
from app.logging import configure_logging, get_logger
from app.main import build_vault
from app.providers.fake import FakeProvider, FakeScript
from app.providers.registry import ProviderRegistry
from app.redis_client import make_redis
from app.turns.runner import TurnRunner
from app.turns.schedule import claim_wakeup, due_chats
from app.turns.sweeper import Sweeper

HEARTBEAT_KEY = "worker:{worker_id}:tick"
HEARTBEAT_INDEX = "workers:alive"
HEARTBEAT_TTL_S = 10
TICK_S = 0.1

log = get_logger("worker")


class Worker:
    def __init__(
        self,
        settings: Settings,
        redis: Redis,
        runner: TurnRunner | None = None,
        sweep_interval_s: float = 5.0,
    ) -> None:
        self.settings = settings
        self.redis = redis
        self.runner = runner
        self.worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid7().hex[-6:]}"
        self.sweeper = (
            Sweeper(runner.sessionmaker, redis, self.worker_id, sweep_interval_s)
            if runner is not None
            else None
        )
        self._stop = asyncio.Event()
        self._last_beat = 0.0
        self._tasks: set[asyncio.Task[None]] = set()

    def stop(self) -> None:
        self._stop.set()

    @property
    def in_flight(self) -> int:
        return len(self._tasks)

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

    async def _run_turn(self, chat_id: str) -> None:
        assert self.runner is not None
        try:
            await self.runner.run_chat(uuid.UUID(chat_id))
        except Exception:
            log.exception("worker.turn_crashed", chat_id=chat_id)

    async def tick(self) -> None:
        await self.heartbeat()
        if self.runner is None or self._stop.is_set():
            return
        if self.sweeper is not None:
            try:
                await self.sweeper.maybe_sweep()
            except Exception:
                log.exception("worker.sweep_failed")
        free = self.settings.worker_max_concurrent_turns - len(self._tasks)
        if free <= 0:
            return
        for chat_id in await due_chats(self.redis, free):
            if not await claim_wakeup(self.redis, chat_id):
                continue  # another worker won this wake-up
            task = asyncio.create_task(self._run_turn(chat_id))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def drain(self, grace_s: float) -> None:
        if not self._tasks:
            return
        log.info("worker.draining", in_flight=len(self._tasks), grace_s=grace_s)
        _done, pending = await asyncio.wait(set(self._tasks), timeout=grace_s)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def run(self) -> None:
        log.info("worker.started", worker_id=self.worker_id)
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("worker.tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), TICK_S)
        await self.drain(self.settings.shutdown_grace_s)
        if self.sweeper is not None:
            await self.sweeper.resign()
        await self.redis.zrem(HEARTBEAT_INDEX, self.worker_id)
        await self.redis.delete(HEARTBEAT_KEY.format(worker_id=self.worker_id))
        log.info("worker.stopped", worker_id=self.worker_id)


async def _main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.env != "development")
    redis = make_redis(settings.redis_url)
    engine = make_engine(settings.database_url)
    # demo provider streams at a visible pace (tests use their own instant FakeProvider)
    providers = ProviderRegistry(fake=FakeProvider(FakeScript(delay_s=0.04)))
    runner = TurnRunner(make_sessionmaker(engine), redis, build_vault(settings), providers)
    worker = Worker(settings, redis, runner)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # Windows
            loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await redis.aclose()
        await engine.dispose()


async def _check() -> int:
    """Healthcheck: exit 0 if any worker heartbeat is fresh."""
    redis = make_redis(get_settings().redis_url)
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
