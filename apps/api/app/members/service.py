"""Membership side effects shared by invites and member management:
system-event messages ("Alice joined"), `member.updated` events, and the
per-user control channel that kicks removed members' live sockets (I5)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatMember, Message, User
from app.events import Outbox, allocate_seq
from app.ids import uuid7
from app.messages.service import message_payload

MAX_COLLABORATORS = 10  # besides the owner (prompt §2.7)


def control_channel(user_id: uuid.UUID | str) -> str:
    return f"user:{user_id}:control"


def member_payload(user: User, member: ChatMember, *, removed: bool = False) -> dict[str, Any]:
    return {
        "user_id": str(user.id),
        "name": user.name,
        "avatar_color": user.avatar_color,
        "role": member.role,
        "joined_at": member.joined_at.isoformat() if member.joined_at else None,
        "removed": removed,
    }


async def system_event(db: AsyncSession, outbox: Outbox, chat_id: uuid.UUID, text: str) -> None:
    """A visible, persisted timeline entry. The model sees it as <event>text</event>."""
    seq = await allocate_seq(db, chat_id)
    msg = Message(
        id=uuid7(),
        chat_id=chat_id,
        seq=seq,
        kind="system_event",
        content=text,
        status="done",
        created_at=dt.datetime.now(dt.UTC),
    )
    db.add(msg)
    outbox.record(db, chat_id, seq, "message.created", {"message": message_payload(msg)})


async def member_updated(
    db: AsyncSession,
    outbox: Outbox,
    chat_id: uuid.UUID,
    user: User,
    member: ChatMember,
    *,
    removed: bool = False,
) -> None:
    await outbox.append(
        db, chat_id, "member.updated", {"member": member_payload(user, member, removed=removed)}
    )


async def active_collaborators(db: AsyncSession, chat_id: uuid.UUID) -> int:
    n = await db.scalar(
        select(func.count())
        .select_from(ChatMember)
        .where(
            ChatMember.chat_id == chat_id,
            ChatMember.removed_at.is_(None),
            ChatMember.role != "owner",
        )
    )
    return int(n or 0)


async def kick(redis: Redis, user_id: uuid.UUID, chat_id: uuid.UUID, reason: str) -> None:
    """Tell every API instance to drop this user's live subscription to the chat."""
    await redis.publish(
        control_channel(user_id),
        json.dumps({"type": "kicked", "chat_id": str(chat_id), "reason": reason}),
    )
