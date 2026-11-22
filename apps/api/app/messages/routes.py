"""Message REST routes: send (fallback to WS `message.send`) and the chat state
snapshot clients load before subscribing for live events."""

from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.chats.permissions import Action
from app.chats.schemas import MessageView
from app.chats.service import require
from app.db.models import Chat, Message, Turn, TurnMessage
from app.deps import AuthDep, CsrfAuthDep, DbDep, RedisDep, SettingsDep
from app.messages.service import (
    MAX_MESSAGE_CHARS,
    message_payload,
    payloads_with_anchors,
    send_message,
)

router = APIRouter(tags=["messages"])

STATE_PAGE = 50


class SendIn(BaseModel):
    client_msg_id: uuid.UUID
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class SendOut(BaseModel):
    message: MessageView
    created: bool


class RunningTurn(BaseModel):
    id: uuid.UUID
    trigger: Literal["batch", "retry", "regenerate"]
    started_seq: int | None
    message_ids: list[uuid.UUID]
    author_ids: list[uuid.UUID]


class ChatState(BaseModel):
    last_seq: int
    messages: list[MessageView]
    has_more: bool
    running_turn: RunningTurn | None


def to_view(m: Message) -> MessageView:
    return MessageView.model_validate(message_payload(m))


@router.post("/chats/{chat_id}/messages", response_model=SendOut)
async def post_message(
    chat_id: uuid.UUID,
    body: SendIn,
    response: Response,
    auth: CsrfAuthDep,
    db: DbDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> SendOut:
    res = await send_message(
        db,
        redis,
        chat_id=chat_id,
        user=auth.user,
        client_msg_id=body.client_msg_id,
        content=body.content,
        debounce_ms=settings.debounce_ms,
        rate_per_min=settings.message_rate_per_min,
        chat_rate_per_min=settings.chat_rate_per_min,
    )
    response.status_code = 201 if res.created else 200
    return SendOut(message=to_view(res.message), created=res.created)


@router.get("/chats/{chat_id}/state", response_model=ChatState)
async def chat_state(chat_id: uuid.UUID, auth: AuthDep, db: DbDep) -> ChatState:
    """One consistent snapshot (REPEATABLE READ): `last_seq` plus everything
    up to it. Clients then `subscribe {after_seq: last_seq}` with no gap."""
    await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    await require(db, chat_id, auth.user.id, Action.READ)
    last_seq = await db.scalar(select(Chat.last_seq).where(Chat.id == chat_id))
    rows = (
        await db.scalars(
            select(Message)
            .where(Message.chat_id == chat_id)
            .order_by(Message.seq.desc())
            .limit(STATE_PAGE + 1)
        )
    ).all()
    has_more = len(rows) > STATE_PAGE
    msgs = list(reversed(rows[:STATE_PAGE]))

    running = None
    turn = await db.scalar(select(Turn).where(Turn.chat_id == chat_id, Turn.status == "running"))
    if turn is not None:
        batch = (
            await db.execute(
                select(Message.id, Message.author_id)
                .join(TurnMessage, TurnMessage.message_id == Message.id)
                .where(TurnMessage.turn_id == turn.id)
                .order_by(Message.seq)
            )
        ).all()
        running = RunningTurn(
            id=turn.id,
            trigger=turn.trigger,
            started_seq=turn.started_seq,
            message_ids=[r[0] for r in batch],
            author_ids=list(dict.fromkeys(r[1] for r in batch if r[1] is not None)),
        )
    views = [MessageView.model_validate(p) for p in await payloads_with_anchors(db, msgs)]
    await db.rollback()
    return ChatState(
        last_seq=int(last_seq or 0),
        messages=views,
        has_more=has_more,
        running_turn=running,
    )
