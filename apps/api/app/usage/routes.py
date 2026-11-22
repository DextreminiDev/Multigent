"""Usage panel + context meter endpoints (ARCHITECTURE.md §10.2, §12)."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from app.chats.permissions import Action
from app.chats.service import require
from app.db.models import ChatMember, ChatSummary, Message, UsageEvent, User
from app.deps import AuthDep, CsrfAuthDep, DbDep, RedisDep
from app.turns.runner import compact_key, context_key
from app.usage.service import today, tokens_used

router = APIRouter(tags=["usage"])

WEEK = 7


class MemberUsage(BaseModel):
    user_id: uuid.UUID | None  # None = chat-level (summaries)
    name: str
    avatar_color: str | None
    role: str | None  # None = former member
    tokens_today: int
    tokens_week: int
    member_token_cap: int | None


class DayUsage(BaseModel):
    day: dt.date
    tokens_in: int
    tokens_out: int
    est_cost_usd: float | None


class UsageView(BaseModel):
    day: dt.date
    daily_token_cap: int | None
    used_today: int
    members: list[MemberUsage]
    days: list[DayUsage]
    est_cost_week_usd: float | None
    pricing_known: bool


@router.get("/chats/{chat_id}/usage", response_model=UsageView)
async def get_usage(chat_id: uuid.UUID, auth: AuthDep, db: DbDep, redis: RedisDep) -> UsageView:
    m = await require(db, chat_id, auth.user.id, Action.VIEW_PRIVATE_SETTINGS)
    day0 = today()
    since = day0 - dt.timedelta(days=WEEK - 1)

    per_day = (
        await db.execute(
            select(
                UsageEvent.day,
                func.sum(UsageEvent.tokens_in),
                func.sum(UsageEvent.tokens_out),
                func.sum(UsageEvent.est_cost_usd),
                func.count(UsageEvent.est_cost_usd),
            )
            .where(UsageEvent.chat_id == chat_id, UsageEvent.day >= since)
            .group_by(UsageEvent.day)
        )
    ).all()
    by_day = {d: (int(i or 0), int(o or 0), c, int(n)) for d, i, o, c, n in per_day}
    days = []
    total_cost = Decimal(0)
    priced = False
    for k in range(WEEK):
        d = since + dt.timedelta(days=k)
        i, o, c, n = by_day.get(d, (0, 0, None, 0))
        if n:
            priced = True
            total_cost += Decimal(c or 0)
        days.append(DayUsage(day=d, tokens_in=i, tokens_out=o,
                             est_cost_usd=float(c or 0) if n else None))  # fmt: skip

    per_user = (
        await db.execute(
            select(
                UsageEvent.user_id,
                func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out).filter(
                    UsageEvent.day == day0
                ),
                func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out),
            )
            .where(UsageEvent.chat_id == chat_id, UsageEvent.day >= since)
            .group_by(UsageEvent.user_id)
        )
    ).all()
    usage: dict[uuid.UUID | None, tuple[int, int]] = {
        u: (int(t or 0), int(w or 0)) for u, t, w in per_user
    }
    current = {
        u.id: (u, mem)
        for u, mem in (
            await db.execute(
                select(User, ChatMember)
                .join(ChatMember, ChatMember.user_id == User.id)
                .where(ChatMember.chat_id == chat_id, ChatMember.removed_at.is_(None))
            )
        ).all()
    }
    former_ids = [u for u in usage if u is not None and u not in current]
    former = {
        u.id: u for u in (await db.scalars(select(User).where(User.id.in_(former_ids)))).all()
    } if former_ids else {}  # fmt: skip

    members: list[MemberUsage] = []
    for uid, (user, mem) in current.items():
        t, w = usage.get(uid, (0, 0))
        members.append(MemberUsage(user_id=uid, name=user.name, avatar_color=user.avatar_color,
                                   role=mem.role, tokens_today=t, tokens_week=w,
                                   member_token_cap=mem.member_token_cap))  # fmt: skip
    for uid in former_ids:
        t, w = usage[uid]
        u = former.get(uid)
        members.append(
            MemberUsage(
                user_id=uid,
                name=u.name if u else "Former member",
                avatar_color=u.avatar_color if u else None,
                role=None,
                tokens_today=t,
                tokens_week=w,
                member_token_cap=None,
            )
        )
    if None in usage:
        t, w = usage[None]
        members.append(MemberUsage(user_id=None, name="Summaries & unattributed", avatar_color=None,
                                   role=None, tokens_today=t, tokens_week=w,
                                   member_token_cap=None))  # fmt: skip
    members.sort(key=lambda x: -x.tokens_week)

    return UsageView(
        day=day0,
        daily_token_cap=m.chat.daily_token_cap,
        used_today=await tokens_used(redis, db, chat_id),
        members=members,
        days=days,
        est_cost_week_usd=float(total_cost) if priced else None,
        pricing_known=priced,
    )


# ------------------------------------------------------------------ context meter


class SummaryInfo(BaseModel):
    covers_through_seq: int
    covered_messages: int
    created_at: dt.datetime
    tokens: int
    content: str | None  # owner only


class ContextView(BaseModel):
    window: int | None
    budget: int | None
    used: int | None
    percent: float | None
    source: str | None
    max_output: int | None
    summary: SummaryInfo | None
    compact_requested: bool


@router.get("/chats/{chat_id}/context", response_model=ContextView)
async def get_context(chat_id: uuid.UUID, auth: AuthDep, db: DbDep, redis: RedisDep) -> ContextView:
    """Snapshot written by the worker at the last turn (null fields before any)."""
    m = await require(db, chat_id, auth.user.id, Action.READ)
    raw = await redis.get(context_key(chat_id))
    info: dict[str, Any] = json.loads(raw) if raw else {}
    latest = await db.scalar(
        select(ChatSummary)
        .where(ChatSummary.chat_id == chat_id)
        .order_by(ChatSummary.created_at.desc())
        .limit(1)
    )
    summary = None
    if latest is not None:
        covered = await db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.chat_id == chat_id,
                Message.seq <= latest.covers_through_seq,
                Message.kind != "system_event",
            )
        )
        summary = SummaryInfo(
            covers_through_seq=latest.covers_through_seq,
            covered_messages=int(covered or 0),
            created_at=latest.created_at,
            tokens=latest.tokens,
            content=latest.content if m.role == "owner" else None,
        )
    return ContextView(
        window=info.get("window"),
        budget=info.get("budget"),
        used=info.get("used"),
        percent=info.get("percent"),
        source=info.get("source"),
        max_output=info.get("max_output"),
        summary=summary,
        compact_requested=bool(await redis.exists(compact_key(chat_id))),
    )


@router.post("/chats/{chat_id}/context/compact", status_code=202)
async def request_compaction(
    chat_id: uuid.UUID, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> Response:
    """Owner: fold older history into the summary before the next reply."""
    await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT)
    await redis.set(compact_key(chat_id), "1", ex=7 * 24 * 3600)
    return Response(status_code=202)


@router.delete("/chats/{chat_id}/context/summary", status_code=204)
async def clear_summary(
    chat_id: uuid.UUID, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> Response:
    """Owner: drop the rolling summary; the next reply sees the full history again
    (and re-summarizes if it doesn't fit)."""
    await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT)
    await db.execute(delete(ChatSummary).where(ChatSummary.chat_id == chat_id))
    await db.commit()
    await redis.delete(context_key(chat_id), compact_key(chat_id))
    return Response(status_code=204)
