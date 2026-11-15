"""Human message intake (ARCHITECTURE.md §6.2). Used by WS `message.send` and REST.

Idempotent on (chat_id, client_msg_id): retries after reconnects return the
original message instead of creating a duplicate (I2).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.chats.permissions import Action
from app.chats.service import require
from app.db.models import Message, User
from app.errors import ApiError
from app.events import Outbox, allocate_seq
from app.ids import uuid7
from app.turns.schedule import wake
from app.users.names import avatar_color

MAX_MESSAGE_CHARS = 8000


def message_payload(
    m: Message, *, anchor_seq: int | None = None, client_msg_id: bool = True
) -> dict[str, Any]:
    """`anchor_seq` is where the message sits in the timeline: its own seq, except
    for AI replies, which sit at their turn's `turn.started` seq."""
    out: dict[str, Any] = {
        "id": str(m.id),
        "seq": m.seq,
        "anchor_seq": anchor_seq if anchor_seq is not None else m.seq,
        "kind": m.kind,
        "author_id": str(m.author_id) if m.author_id else None,
        "author_name": m.author_name_snapshot,
        "author_color": avatar_color(m.author_id) if m.author_id else None,
        "content": "" if m.status == "deleted" else m.content,
        "status": m.status,
        "turn_id": str(m.turn_id) if m.turn_id else None,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }
    if client_msg_id:
        out["client_msg_id"] = str(m.client_msg_id) if m.client_msg_id else None
    return out


@dataclass
class SendResult:
    message: Message
    created: bool


def validate_content(content: str) -> str:
    if not content.strip():
        raise ApiError(422, "empty_message", "Message is empty")
    if len(content) > MAX_MESSAGE_CHARS:
        raise ApiError(
            422, "message_too_long", f"Messages are limited to {MAX_MESSAGE_CHARS} characters"
        )
    return content.strip("\n")


async def _existing(db: AsyncSession, chat_id: uuid.UUID, cmid: uuid.UUID) -> Message | None:
    return await db.scalar(
        select(Message).where(Message.chat_id == chat_id, Message.client_msg_id == cmid)
    )


async def send_message(
    db: AsyncSession,
    redis: Redis,
    *,
    chat_id: uuid.UUID,
    user: User,
    client_msg_id: uuid.UUID,
    content: str,
    debounce_ms: int,
) -> SendResult:
    content = validate_content(content)
    await require(db, chat_id, user.id, Action.SEND)
    if (prior := await _existing(db, chat_id, client_msg_id)) is not None:
        return SendResult(prior, created=False)

    outbox = Outbox()
    try:
        # seq + message + event in one txn; the chat row lock orders concurrent sends
        seq = await allocate_seq(db, chat_id)
        msg = Message(
            id=uuid7(),
            chat_id=chat_id,
            seq=seq,
            kind="human",
            author_id=user.id,
            author_name_snapshot=user.name,
            content=content,
            client_msg_id=client_msg_id,
            status="pending",
            created_at=dt.datetime.now(dt.UTC),
        )
        db.add(msg)
        outbox.record(db, chat_id, seq, "message.created", {"message": message_payload(msg)})
        await db.commit()
    except IntegrityError:
        # concurrent duplicate with the same client_msg_id won the race
        await db.rollback()
        prior = await _existing(db, chat_id, client_msg_id)
        if prior is None:
            raise
        return SendResult(prior, created=False)

    await outbox.publish(redis)
    await wake(redis, chat_id, debounce_ms)
    return SendResult(msg, created=True)


async def payloads_with_anchors(db: AsyncSession, msgs: list[Message]) -> list[dict[str, Any]]:
    """Message payloads for reads: AI replies are anchored at their turn's start."""
    from app.db.models import Turn

    turn_ids = {m.turn_id for m in msgs if m.kind == "ai" and m.turn_id is not None}
    anchors: dict[uuid.UUID, int] = {}
    if turn_ids:
        rows = await db.execute(select(Turn.id, Turn.started_seq).where(Turn.id.in_(turn_ids)))
        anchors = {tid: int(s) for tid, s in rows.all() if s is not None}
    return [
        message_payload(
            m, anchor_seq=anchors.get(m.turn_id) if m.kind == "ai" and m.turn_id else None
        )
        for m in msgs
    ]
