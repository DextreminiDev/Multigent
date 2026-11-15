"""WebSocket gateway: `GET /ws` (ARCHITECTURE.md §8).

Frames are JSON. Every client frame carries a `ref`; the server answers with
`{"type": "ack", "ref": ...}` or `{"type": "error", "ref": ..., "code", "message"}`.

Resume (§8.3), per subscription:
  1. verify membership
  2. join the Redis channel and WAIT for the subscribe confirmation (live events
     now buffer locally)
  3. replay `chat_events` with seq > after_seq from Postgres
  4. if a turn is running, send `turn.snapshot` with its streamed chunks so far
  5. flush buffered live events, dropping persisted ones with seq <= last replayed
Clients dedupe deltas by (turn_id, idx), so snapshot/live overlap is harmless.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.websockets import WebSocketState

from app.auth.sessions import SESSION_COOKIE, SessionStore
from app.chats.permissions import Action, authorize
from app.chats.service import load_membership
from app.db.models import ChatEvent, Turn, User
from app.errors import ApiError
from app.logging import get_logger
from app.messages.service import MAX_MESSAGE_CHARS, message_payload, send_message
from app.realtime.hub import Hub, HubEvent
from app.turns.runner import buffer_key

router = APIRouter()
log = get_logger("ws")

MAX_FRAME_BYTES = 64 * 1024
OUTBOX_LIMIT = 2000
REPLAY_LIMIT = 500

# close codes
POLICY = 1008
TOO_BIG = 1009
OVERLOADED = 1013


class Frame(BaseModel):
    type: str
    ref: str | None = Field(default=None, max_length=64)


class SubscribeIn(Frame):
    chat_id: uuid.UUID
    after_seq: int = Field(ge=0)


class UnsubscribeIn(Frame):
    chat_id: uuid.UUID


class SendIn(Frame):
    chat_id: uuid.UUID
    client_msg_id: uuid.UUID
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


@dataclass
class Sub:
    chat_id: uuid.UUID
    last_seq: int
    buffering: list[HubEvent] | None = field(default_factory=list)


class Connection:
    def __init__(self, ws: WebSocket, user: User) -> None:
        self.ws = ws
        self.user = user
        self.subs: dict[uuid.UUID, Sub] = {}
        self.outbox: asyncio.Queue[str | None] = asyncio.Queue(maxsize=OUTBOX_LIMIT)
        self.overloaded = False

    # ---- outbound
    def send_raw(self, raw: str) -> None:
        if self.overloaded:
            return
        try:
            self.outbox.put_nowait(raw)
        except asyncio.QueueFull:
            # slow consumer: drop the socket; the client resumes from its last seq
            # (the writer sees the flag on its next frame and closes)
            self.overloaded = True

    def send(self, obj: dict[str, Any]) -> None:
        self.send_raw(json.dumps(obj, default=str))

    def deliver(self, event: HubEvent) -> None:
        sub = self.subs.get(uuid.UUID(event.chat_id))
        if sub is None:
            return
        if sub.buffering is not None:
            sub.buffering.append(event)
            return
        self._forward(sub, event)

    def _forward(self, sub: Sub, event: HubEvent) -> None:
        if event.seq is not None:
            if event.seq <= sub.last_seq:
                return  # already replayed
            sub.last_seq = event.seq
        self.send_raw(event.raw)
        if event.type == "chat.deleted":
            self.subs.pop(sub.chat_id, None)

    async def writer(self) -> None:
        while True:
            raw = await self.outbox.get()
            if raw is None or self.overloaded:
                with contextlib.suppress(Exception):
                    await self.ws.close(OVERLOADED, "slow consumer")
                return
            await self.ws.send_text(raw)


async def _authenticate(ws: WebSocket) -> User | None:
    app = ws.app
    origin = ws.headers.get("origin")
    if origin is None or origin not in app.state.settings.allowed_origins:
        return None
    sid = ws.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    sessions: SessionStore = app.state.sessions
    user_id = await sessions.get(sid)
    if user_id is None:
        return None
    factory: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    async with factory() as db:
        user = await db.get(User, user_id)
        if user is None or user.deleted_at is not None:
            return None
        db.expunge(user)
        return user


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    user = await _authenticate(ws)
    if user is None:
        await ws.close(POLICY, "unauthorized")
        return
    await ws.accept()
    conn = Connection(ws, user)
    hub: Hub = ws.app.state.hub
    writer = asyncio.create_task(conn.writer())
    try:
        while True:
            try:
                text = await ws.receive_text()
            except WebSocketDisconnect:
                break
            if writer.done():
                break
            if len(text.encode()) > MAX_FRAME_BYTES:
                await ws.close(TOO_BIG, "frame too large")
                break
            await _handle(conn, hub, text)
    finally:
        for chat_id in list(conn.subs):
            await hub.leave(chat_id, conn)
        writer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await writer
        if ws.application_state != WebSocketState.DISCONNECTED:
            with contextlib.suppress(Exception):
                await ws.close()


def _error(conn: Connection, ref: str | None, code: str, message: str) -> None:
    conn.send({"type": "error", "ref": ref, "code": code, "message": message})


async def _handle(conn: Connection, hub: Hub, text: str) -> None:
    try:
        raw = json.loads(text)
        frame = Frame.model_validate(raw)
    except (ValueError, ValidationError):
        _error(conn, None, "bad_frame", "Malformed frame")
        return
    try:
        if frame.type == "subscribe":
            await _subscribe(conn, hub, SubscribeIn.model_validate(raw))
        elif frame.type == "unsubscribe":
            msg = UnsubscribeIn.model_validate(raw)
            conn.subs.pop(msg.chat_id, None)
            await hub.leave(msg.chat_id, conn)
            conn.send({"type": "ack", "ref": frame.ref})
        elif frame.type == "message.send":
            await _send(conn, SendIn.model_validate(raw))
        else:
            _error(conn, frame.ref, "unknown_type", f"Unknown frame type {frame.type!r}")
    except ValidationError as e:
        fields = [".".join(str(p) for p in err["loc"]) for err in e.errors()]
        _error(conn, frame.ref, "validation_error", f"Invalid fields: {', '.join(fields)}")
    except ApiError as e:
        _error(conn, frame.ref, e.code, e.message)
    except Exception:
        log.exception("ws.handler_failed", type=frame.type)
        _error(conn, frame.ref, "internal", "Something went wrong")


async def _subscribe(conn: Connection, hub: Hub, msg: SubscribeIn) -> None:
    app = conn.ws.app
    async with app.state.sessionmaker() as db:
        m = await load_membership(db, msg.chat_id, conn.user.id)
        if not authorize(m.role, Action.READ):
            raise ApiError(404, "not_found", "Chat not found")

    # (re)subscribe: start buffering live events BEFORE reading the log
    sub = Sub(chat_id=msg.chat_id, last_seq=msg.after_seq)
    conn.subs[msg.chat_id] = sub
    await hub.join(msg.chat_id, conn)

    async with app.state.sessionmaker() as db:
        events = (
            await db.scalars(
                select(ChatEvent)
                .where(ChatEvent.chat_id == msg.chat_id, ChatEvent.seq > msg.after_seq)
                .order_by(ChatEvent.seq)
                .limit(REPLAY_LIMIT + 1)
            )
        ).all()
        running = await db.scalar(
            select(Turn.id).where(Turn.chat_id == msg.chat_id, Turn.status == "running")
        )
        latest = await db.scalar(
            select(ChatEvent.seq)
            .where(ChatEvent.chat_id == msg.chat_id)
            .order_by(ChatEvent.seq.desc())
            .limit(1)
        )

    if len(events) > REPLAY_LIMIT:
        # too far behind: client refetches GET /chats/{id}/state and resubscribes
        sub.last_seq = int(latest or 0)
        conn.send(
            {"type": "resync_required", "chat_id": str(msg.chat_id), "last_seq": sub.last_seq}
        )
    else:
        for ev in events:
            conn.send(
                {
                    "type": ev.type,
                    "chat_id": str(msg.chat_id),
                    "seq": ev.seq,
                    "payload": ev.payload,
                    "ts": ev.created_at.isoformat(),
                    "replay": True,
                }
            )
            sub.last_seq = ev.seq

    if running is not None:
        chunks = [json.loads(c) for c in await app.state.redis.lrange(buffer_key(running), 0, -1)]
        conn.send(
            {
                "type": "turn.snapshot",
                "chat_id": str(msg.chat_id),
                "payload": {"turn_id": str(running), "chunks": chunks},
            }
        )

    buffered, sub.buffering = sub.buffering or [], None
    for ev in buffered:
        conn._forward(sub, ev)
    conn.send({"type": "ack", "ref": msg.ref, "chat_id": str(msg.chat_id), "seq": sub.last_seq})


async def _send(conn: Connection, msg: SendIn) -> None:
    app = conn.ws.app
    async with app.state.sessionmaker() as db:
        res = await send_message(
            db,
            app.state.redis,
            chat_id=msg.chat_id,
            user=conn.user,
            client_msg_id=msg.client_msg_id,
            content=msg.content,
            debounce_ms=app.state.settings.debounce_ms,
        )
    conn.send(
        {
            "type": "ack",
            "ref": msg.ref,
            "created": res.created,
            "message": message_payload(res.message),
        }
    )
