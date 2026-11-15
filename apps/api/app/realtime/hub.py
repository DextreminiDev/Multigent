"""Per-process fan-out hub (ARCHITECTURE.md §8.4).

One Redis pub/sub connection per API process. A chat channel is subscribed
lazily when the first local socket joins and dropped when the last leaves.
`join()` returns only after Redis confirms the SUBSCRIBE, which is what makes the
subscribe-then-replay resume protocol gap-free (§8.3).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from app.events import chat_channel
from app.logging import get_logger

log = get_logger("hub")


@dataclass(frozen=True, slots=True)
class HubEvent:
    chat_id: str
    raw: str
    type: str
    seq: int | None
    data: dict[str, Any]


class Subscriber(Protocol):
    def deliver(self, event: HubEvent) -> None: ...


class Hub:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis
        self._pubsub: PubSub | None = None
        self._subs: dict[str, set[Subscriber]] = defaultdict(set)
        self._confirm: dict[str, asyncio.Event] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._home = f"hub:{uuid.uuid4().hex}"  # keeps the pubsub connection alive
        self._ready = asyncio.Event()

    async def start(self) -> None:
        """Non-blocking: the API must boot (and report not-ready) while Redis is down."""
        self._pubsub = self.redis.pubsub()
        self._task = asyncio.create_task(self._reader())

    async def _connect(self) -> None:
        assert self._pubsub is not None
        while True:
            try:
                await self._pubsub.subscribe(self._home)
                self._ready.set()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                await asyncio.sleep(1.0)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        if self._pubsub is not None:
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]

    async def join(self, chat_id: uuid.UUID, sub: Subscriber) -> None:
        key = str(chat_id)
        await asyncio.wait_for(self._ready.wait(), 5)
        async with self._lock:
            first = not self._subs[key]
            self._subs[key].add(sub)
            if first:
                assert self._pubsub is not None
                ev = self._confirm[key] = asyncio.Event()
                await self._pubsub.subscribe(chat_channel(chat_id))
            else:
                ev = self._confirm.get(key) or asyncio.Event()
                if key not in self._confirm:
                    ev.set()
        await asyncio.wait_for(ev.wait(), 5)

    async def leave(self, chat_id: uuid.UUID, sub: Subscriber) -> None:
        key = str(chat_id)
        async with self._lock:
            subs = self._subs.get(key)
            if not subs:
                return
            subs.discard(sub)
            if not subs:
                del self._subs[key]
                self._confirm.pop(key, None)
                assert self._pubsub is not None
                await self._pubsub.unsubscribe(chat_channel(chat_id))

    def local_subscribers(self, chat_id: uuid.UUID) -> int:
        return len(self._subs.get(str(chat_id), ()))

    async def _reader(self) -> None:
        assert self._pubsub is not None
        await self._connect()
        while True:
            try:
                msg = await self._pubsub.get_message(timeout=1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("hub.read_failed")
                await asyncio.sleep(0.5)
                continue
            if msg is None:
                continue
            channel = str(msg.get("channel", ""))
            if not channel.startswith("chat:"):
                continue
            chat_id = channel.split(":")[1]
            if msg["type"] == "subscribe":
                if (ev := self._confirm.get(chat_id)) is not None:
                    ev.set()
                continue
            if msg["type"] != "message":
                continue
            raw = str(msg["data"])
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            event = HubEvent(
                chat_id=chat_id,
                raw=raw,
                type=str(data.get("type", "")),
                seq=data.get("seq"),
                data=data,
            )
            for sub in list(self._subs.get(chat_id, ())):
                try:
                    sub.deliver(event)
                except Exception:
                    log.exception("hub.deliver_failed")
