"""Member management (prompt §6 permissions matrix).

- GET    /chats/{id}/members              any member
- PATCH  /chats/{id}/members/{user_id}    owner: change an editor/viewer's role
- DELETE /chats/{id}/members/{user_id}    owner removes someone, or a member leaves
Removal is soft (`removed_at`): their messages stay, labelled "former member".
Every change emits `member.updated`; removals also kick live sockets (I5).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.chats.permissions import Action, authorize
from app.chats.service import load_membership, require
from app.db.models import ChatMember, User
from app.deps import AuthDep, CsrfAuthDep, DbDep, RedisDep
from app.errors import ApiError, forbidden, not_found
from app.events import Outbox
from app.members.service import kick, member_updated, system_event
from app.realtime.presence import online_user_ids

router = APIRouter(tags=["members"])


class MemberView(BaseModel):
    user_id: uuid.UUID
    name: str
    avatar_color: str
    role: Literal["owner", "editor", "viewer"]
    joined_at: dt.datetime
    online: bool
    member_token_cap: int | None = None  # owner-only (null for collaborators)


class MemberPatch(BaseModel):
    """Either field may be sent; `member_token_cap: null` removes the cap."""

    role: Literal["editor", "viewer"] | None = None
    member_token_cap: int | None = Field(default=None, ge=1)


@router.get("/chats/{chat_id}/members", response_model=list[MemberView])
async def list_members(
    chat_id: uuid.UUID, request: Request, auth: AuthDep, db: DbDep, redis: RedisDep
) -> list[MemberView]:
    me = await require(db, chat_id, auth.user.id, Action.READ)
    owner_view = me.role == "owner"
    rows = (
        await db.execute(
            select(User, ChatMember)
            .join(ChatMember, ChatMember.user_id == User.id)
            .where(ChatMember.chat_id == chat_id, ChatMember.removed_at.is_(None))
            .order_by(ChatMember.joined_at)
        )
    ).all()
    online = await online_user_ids(redis, chat_id)
    order = {"owner": 0, "editor": 1, "viewer": 2}
    views = [
        MemberView(
            user_id=u.id,
            name=u.name,
            avatar_color=u.avatar_color,
            role=m.role,
            joined_at=m.joined_at,
            online=str(u.id) in online,
            member_token_cap=m.member_token_cap if owner_view else None,
        )
        for u, m in rows
    ]
    return sorted(views, key=lambda v: (order[v.role], v.joined_at))


async def _target(db: DbDep, chat_id: uuid.UUID, user_id: uuid.UUID) -> tuple[ChatMember, User]:
    member = await db.get(ChatMember, (chat_id, user_id))
    user = await db.get(User, user_id)
    if member is None or member.removed_at is not None or user is None:
        raise not_found("Member not found")
    return member, user


@router.patch("/chats/{chat_id}/members/{user_id}", response_model=MemberView)
async def change_role(
    chat_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberPatch,
    auth: CsrfAuthDep,
    db: DbDep,
    redis: RedisDep,
) -> MemberView:
    await require(db, chat_id, auth.user.id, Action.MANAGE_MEMBERS, for_update=True)
    member, user = await _target(db, chat_id, user_id)
    if member.role == "owner" and body.role is not None:
        raise ApiError(409, "owner_role", "The owner's role can't be changed")
    changed = False
    if body.role is not None and member.role != body.role:
        member.role = body.role
        changed = True
    if (
        "member_token_cap" in body.model_fields_set
        and member.member_token_cap != body.member_token_cap
    ):
        member.member_token_cap = body.member_token_cap
        changed = True
    if changed:
        outbox = Outbox()
        await member_updated(db, outbox, chat_id, user, member)
        await db.commit()
        await outbox.publish(redis)
    online = await online_user_ids(redis, chat_id)
    return MemberView(
        user_id=user.id,
        name=user.name,
        avatar_color=user.avatar_color,
        role=member.role,
        joined_at=member.joined_at,
        online=str(user.id) in online,
        member_token_cap=member.member_token_cap,
    )


@router.delete("/chats/{chat_id}/members/{user_id}", status_code=204)
async def remove_member(
    chat_id: uuid.UUID, user_id: uuid.UUID, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> Response:
    me = await load_membership(db, chat_id, auth.user.id, for_update=True)
    leaving = user_id == auth.user.id
    if leaving:
        if not authorize(me.role, Action.LEAVE):
            raise ApiError(
                409, "owner_cannot_leave", "The owner can't leave; delete the chat instead"
            )
    elif not authorize(me.role, Action.MANAGE_MEMBERS):
        raise forbidden()
    member, user = await _target(db, chat_id, user_id)
    if member.role == "owner":
        raise ApiError(409, "owner_role", "The owner can't be removed")

    member.removed_at = dt.datetime.now(dt.UTC)
    outbox = Outbox()
    text = f"{user.name} left the chat" if leaving else f"{user.name} was removed by the owner"
    await system_event(db, outbox, chat_id, text)
    await member_updated(db, outbox, chat_id, user, member, removed=True)
    await db.commit()
    await outbox.publish(redis)
    await kick(redis, user_id, chat_id, "left" if leaving else "removed")
    return Response(status_code=204)
