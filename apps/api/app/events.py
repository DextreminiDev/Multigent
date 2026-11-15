"""Chat event log: seq allocation + publish-after-commit (ARCHITECTURE.md §5).

Usage inside a transaction:

    outbox = Outbox()
    await outbox.append(db, chat_id, "chat.updated", {...})
    await db.commit()
    await outbox.publish(redis)   # only after commit

The UPDATE ... RETURNING takes the chat row lock until commit, so commit order
equals seq order and there are no gaps.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatEvent
from app.errors import not_found


def chat_channel(chat_id: uuid.UUID) -> str:
    return f"chat:{chat_id}:events"


async def allocate_seq(db: AsyncSession, chat_id: uuid.UUID) -> int:
    seq = await db.scalar(
        text(
            "UPDATE chats SET last_seq = last_seq + 1, updated_at = now() "
            "WHERE id = :id RETURNING last_seq"
        ),
        {"id": chat_id},
    )
    if seq is None:
        raise not_found("Chat not found")
    return int(seq)


@dataclass
class Outbox:
    events: list[dict[str, Any]] = field(default_factory=list)
    ephemeral: list[tuple[uuid.UUID, dict[str, Any]]] = field(default_factory=list)

    async def append(
        self, db: AsyncSession, chat_id: uuid.UUID, type_: str, payload: dict[str, Any]
    ) -> int:
        """Persist an event (replayable) and queue it for publishing."""
        seq = await allocate_seq(db, chat_id)
        self.record(db, chat_id, seq, type_, payload)
        return seq

    def record(
        self,
        db: AsyncSession,
        chat_id: uuid.UUID,
        seq: int,
        type_: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist an event for a seq already allocated with `allocate_seq` in this
        transaction (when the payload needs the seq, e.g. a message row)."""
        db.add(ChatEvent(chat_id=chat_id, seq=seq, type=type_, payload=payload))
        self.events.append(
            {
                "type": type_,
                "chat_id": str(chat_id),
                "seq": seq,
                "payload": payload,
                "ts": dt.datetime.now(dt.UTC).isoformat(),
            }
        )

    def push_ephemeral(self, chat_id: uuid.UUID, type_: str, payload: dict[str, Any]) -> None:
        """Queue a non-persisted event (no seq), e.g. chat.deleted."""
        self.ephemeral.append(
            (chat_id, {"type": type_, "chat_id": str(chat_id), "payload": payload})
        )

    async def publish(self, redis: Redis) -> None:
        async with redis.pipeline(transaction=False) as p:
            for ev in self.events:
                p.publish(chat_channel(uuid.UUID(ev["chat_id"])), json.dumps(ev))
            for chat_id, ev in self.ephemeral:
                p.publish(chat_channel(chat_id), json.dumps(ev))
            await p.execute()
        self.events.clear()
        self.ephemeral.clear()
