"""Per-process fan-out hub (ARCHITECTURE.md §8.4, §8.5).

One Redis pub/sub connection per API process.
- Chat channels `chat:{id}:events` are subscribed lazily when the first local
  socket joins and dropped when the last leaves. `join()` returns only after
  Redis confirms the SUBSCRIBE, which makes subscribe-then-replay gap-free (§8.3).
- User control channels `user:{id}:control` deliver `kicked` to every socket of a
  user on every API instance when their membership changes (I5).
- A heartbeat refreshes presence for local sockets and re-broadcasts the online
  set whenever it changes (including when another instance's sockets age out).
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
from app.realtime import presence

log = get_logger("hub")


@dataclass(frozen=True, slots=True)
class HubEvent:
    chat_id: str
    raw: str
    type: str
    seq: int | None
    data: dict[str, Any]


class Subscriber(Protocol):
    user_id: uuid.UUID
    conn_id: str

    def deliver(self, event: HubEvent) -> None: ...

    def control(self, data: dict[str, Any]) -> None: ...


def user_channel(user_id: uuid.UUID | str) -> str:
    return f"user:{user_id}:control"


class Hub:
    def __init__(self, redis: Redis, heartbeat_s: float = presence.HEARTBEAT_S) -> None:
        self.redis = redis
        self.heartbeat_s = heartbeat_s
        self._pubsub: PubSub | None = None
        self._subs: dict[str, set[Subscriber]] = defaultdict(set)
        self._users: dict[str, set[Subscriber]] = defaultdict(set)
        self._confirm: dict[str, asyncio.Event] = {}
        self._last_online: dict[str, list[str]] = {}
        self._lock = asyncio.Lock()
        self._tasks: list[asyncio.Task[None]] = []
        self._home = f"hub:{uuid.uuid4().hex}"  # keeps the pubsub connection alive
        self._ready = asyncio.Event()

    async def start(self) -> None:
        """Non-blocking: the API must boot (and report not-ready) while Redis is down."""
        self._pubsub = self.redis.pubsub()
        self._tasks = [asyncio.create_task(self._reader()), asyncio.create_task(self._heartbeat())]

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
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        if self._pubsub is not None:
            await self._pubsub.aclose()  # type: ignore[no-untyped-call]

    # ------------------------------------------------------------------ users

    async def connect_user(self, sub: Subscriber) -> None:
        await asyncio.wait_for(self._ready.wait(), 5)
        key = str(sub.user_id)
        async with self._lock:
            first = not self._users[key]
            self._users[key].add(sub)
            if first:
                assert self._pubsub is not None
                await self._pubsub.subscribe(user_channel(key))

    async def disconnect_user(self, sub: Subscriber) -> None:
        key = str(sub.user_id)
        async with self._lock:
            subs = self._users.get(key)
            if not subs:
                return
            subs.discard(sub)
            if not subs:
                del self._users[key]
                assert self._pubsub is not None
                await self._pubsub.unsubscribe(user_channel(key))

    # ------------------------------------------------------------------ chats

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
        await presence.touch(self.redis, chat_id, sub.user_id, sub.conn_id)
        await self._announce(key)

    async def leave(self, chat_id: uuid.UUID, sub: Subscriber) -> None:
        key = str(chat_id)
        async with self._lock:
            subs = self._subs.get(key)
            if not subs or sub not in subs:
                return
            subs.discard(sub)
            if not subs:
                del self._subs[key]
                self._confirm.pop(key, None)
                assert self._pubsub is not None
                await self._pubsub.unsubscribe(chat_channel(chat_id))
        with contextlib.suppress(Exception):
            await presence.drop(self.redis, chat_id, sub.user_id, sub.conn_id)
            await self._announce(key, force=True)

    def local_subscribers(self, chat_id: uuid.UUID) -> int:
        return len(self._subs.get(str(chat_id), ()))

    async def _announce(self, chat_id: str, *, force: bool = True) -> None:
        online = sorted(await presence.online_user_ids(self.redis, chat_id))
        if force or online != self._last_online.get(chat_id):
            self._last_online[chat_id] = online
            await presence.broadcast_presence(self.redis, chat_id)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_s)
            try:
                for chat_id, subs in list(self._subs.items()):
                    for sub in list(subs):
                        await presence.touch(
                            self.redis, uuid.UUID(chat_id), sub.user_id, sub.conn_id
                        )
                    await self._announce(chat_id, force=False)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("hub.heartbeat_failed")

    # ------------------------------------------------------------------ reader

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
            kind, _, rest = channel.partition(":")
            target = rest.split(":", 1)[0]
            if msg["type"] == "subscribe":
                if kind == "chat" and (ev := self._confirm.get(target)) is not None:
                    ev.set()
                continue
            if msg["type"] != "message":
                continue
            raw = str(msg["data"])
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            if kind == "user":
                for sub in list(self._users.get(target, ())):
                    try:
                        sub.control(data)
                    except Exception:
                        log.exception("hub.control_failed")
                continue
            if kind != "chat":
                continue
            event = HubEvent(
                chat_id=target,
                raw=raw,
                type=str(data.get("type", "")),
                seq=data.get("seq"),
                data=data,
            )
            for sub in list(self._subs.get(target, ())):
                try:
                    sub.deliver(event)
                except Exception:
                    log.exception("hub.deliver_failed")
