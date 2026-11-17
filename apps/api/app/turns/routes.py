"""Turn controls: stop, retry, regenerate (ARCHITECTURE.md §6.4, §6.6) and
deleting your own still-queued message.

All of them are idempotent or reject cleanly when the state has already moved on
(e.g. Stop pressed just as the reply completed: edge case 17).
"""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import exists, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.chats.permissions import Action, authorize
from app.chats.service import Membership, load_membership, require
from app.db.models import Message, Turn, TurnMessage
from app.deps import CsrfAuthDep, DbDep, RedisDep
from app.errors import ApiError, forbidden, not_found
from app.events import Outbox
from app.messages.service import message_payload
from app.turns.runner import cancel_key
from app.turns.schedule import wake

router = APIRouter(tags=["turns"])

CANCEL_TTL_S = 600


class TurnControlOut(BaseModel):
    turn_id: uuid.UUID
    status: Literal["cancelling", "running", "completed", "failed", "cancelled", "retrying"]


async def _turn(db: AsyncSession, chat_id: uuid.UUID, turn_id: uuid.UUID) -> Turn:
    turn = await db.get(Turn, turn_id)
    if turn is None or turn.chat_id != chat_id:
        raise not_found("Reply not found")
    return turn


async def _authored_in_batch(db: AsyncSession, turn_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return bool(
        await db.scalar(
            select(
                exists().where(
                    TurnMessage.turn_id == turn_id,
                    TurnMessage.message_id == Message.id,
                    Message.author_id == user_id,
                )
            )
        )
    )


async def _check(
    db: AsyncSession, m: Membership, turn: Turn, user_id: uuid.UUID, a: Action
) -> None:
    in_batch = await _authored_in_batch(db, turn.id, user_id)
    if not authorize(m.role, a, in_batch=in_batch):
        raise forbidden("Only the owner, or someone whose message is in this batch, can do that")


async def request_cancel(
    db: AsyncSession, redis: Redis, chat_id: uuid.UUID, turn_id: uuid.UUID, user_id: uuid.UUID
) -> str:
    """Shared by REST and the WS `turn.cancel` frame. Returns the resulting status."""
    m = await load_membership(db, chat_id, user_id)
    turn = await _turn(db, chat_id, turn_id)
    await _check(db, m, turn, user_id, Action.CANCEL_TURN)
    if turn.status != "running":  # already finished: no-op, report what happened
        return turn.status
    await redis.set(cancel_key(turn_id), str(user_id), ex=CANCEL_TTL_S)
    return "cancelling"


@router.post("/chats/{chat_id}/turns/{turn_id}/cancel", response_model=TurnControlOut)
async def cancel_turn(
    chat_id: uuid.UUID, turn_id: uuid.UUID, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> TurnControlOut:
    status = await request_cancel(db, redis, chat_id, turn_id, auth.user.id)
    return TurnControlOut(turn_id=turn_id, status=status)


@router.post("/chats/{chat_id}/turns/{turn_id}/retry", response_model=TurnControlOut)
async def retry_turn(
    chat_id: uuid.UUID, turn_id: uuid.UUID, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> TurnControlOut:
    """Put a failed/stopped batch back in the queue. The messages keep pointing at
    this turn, so the next claim runs as trigger='retry' (with any newer messages)."""
    m = await load_membership(db, chat_id, auth.user.id, for_update=True)
    turn = await _turn(db, chat_id, turn_id)
    await _check(db, m, turn, auth.user.id, Action.RETRY_TURN)
    if turn.status not in ("failed", "cancelled"):
        raise ApiError(409, "not_retryable", "Only failed or stopped replies can be retried")
    ids = list(
        (
            await db.scalars(
                update(Message)
                .where(
                    Message.turn_id == turn_id,
                    Message.kind == "human",
                    Message.status.in_(("batched", "done")),
                )
                .values(status="pending")
                .returning(Message.id)
            )
        ).all()
    )
    if not ids:
        raise ApiError(409, "already_retried", "This reply was already retried")
    outbox = Outbox()
    await outbox.append(
        db, chat_id, "turn.retried", {"turn_id": str(turn_id), "message_ids": [str(i) for i in ids]}
    )
    await db.commit()
    await outbox.publish(redis)
    await wake(redis, chat_id)
    return TurnControlOut(turn_id=turn_id, status="retrying")


@router.post("/chats/{chat_id}/messages/{message_id}/regenerate", response_model=TurnControlOut)
async def regenerate(
    chat_id: uuid.UUID, message_id: uuid.UUID, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> TurnControlOut:
    """Owner only, last AI reply only. The old reply is kept but `superseded`
    (hidden), and its batch is answered again (trigger='regenerate')."""
    await require(db, chat_id, auth.user.id, Action.REGENERATE, for_update=True)
    ai = await db.get(Message, message_id)
    if ai is None or ai.chat_id != chat_id or ai.kind != "ai":
        raise not_found("Reply not found")
    if ai.status != "done":
        raise ApiError(409, "not_regenerable", "This reply can't be regenerated")
    newer = await db.scalar(
        select(exists().where(
            Message.chat_id == chat_id, Message.kind == "ai",
            Message.status == "done", Message.seq > ai.seq,
        ))
    )  # fmt: skip
    if newer:
        raise ApiError(409, "not_latest", "Only the latest reply can be regenerated")
    busy = await db.scalar(
        select(exists().where(Turn.chat_id == chat_id, Turn.status == "running"))
    )
    if busy:
        raise ApiError(409, "busy", "Wait for the current reply to finish")
    assert ai.turn_id is not None
    ai.status = "superseded"
    ids = list(
        (
            await db.scalars(
                update(Message)
                .where(Message.turn_id == ai.turn_id, Message.kind == "human")
                .values(status="pending")
                .returning(Message.id)
            )
        ).all()
    )
    outbox = Outbox()
    await outbox.append(
        db,
        chat_id,
        "message.superseded",
        {
            "message_id": str(message_id),
            "turn_id": str(ai.turn_id),
            "message_ids": [str(i) for i in ids],
        },
    )
    await db.commit()
    await outbox.publish(redis)
    await wake(redis, chat_id)
    return TurnControlOut(turn_id=ai.turn_id, status="retrying")


@router.delete("/chats/{chat_id}/messages/{message_id}", status_code=204)
async def delete_pending_message(
    chat_id: uuid.UUID, message_id: uuid.UUID, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> Response:
    """Authors may withdraw a message while it is still queued. The row is locked
    first, and the claim uses SKIP LOCKED, so it can't be batched mid-delete."""
    await require(db, chat_id, auth.user.id, Action.DELETE_OWN_PENDING, for_update=True)
    msg = await db.scalar(select(Message).where(Message.id == message_id).with_for_update())
    if msg is None or msg.chat_id != chat_id or msg.kind != "human":
        raise not_found("Message not found")
    if msg.author_id != auth.user.id:
        raise forbidden("You can only delete your own messages")
    if msg.status == "deleted":
        return Response(status_code=204)
    if msg.status != "pending":
        raise ApiError(409, "already_sent", "The AI is already answering this message")
    msg.status = "deleted"
    msg.content = ""  # actually gone, not just hidden
    outbox = Outbox()
    await outbox.append(db, chat_id, "message.updated", {"message": message_payload(msg)})
    await db.commit()
    await outbox.publish(redis)
    return Response(status_code=204)
