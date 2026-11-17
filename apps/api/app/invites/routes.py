"""Invite links (prompt §9 "Invite flow", ARCHITECTURE.md §4, §11.5).

- Token: 256 random bits, shown once at creation; only sha256(token) is stored.
- Owner sets role, expiry and max uses; can revoke.
- Public preview shows title/owner/role/member count only (no content).
- Accept is idempotent for existing members and never over-consumes uses: the
  invite row is locked while `uses` is checked and incremented.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import uuid
from typing import Literal

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.chats.permissions import Action
from app.chats.service import member_counts, require
from app.db.models import Chat, ChatInvite, ChatMember, User
from app.deps import AuthDep, CsrfAuthDep, DbDep, RedisDep
from app.errors import ApiError, not_found
from app.events import Outbox
from app.members.service import (
    MAX_COLLABORATORS,
    active_collaborators,
    member_updated,
    system_event,
)
from app.security.ratelimit import limit_ip

router = APIRouter(tags=["invites"])

InviteState = Literal["active", "expired", "used_up", "revoked"]


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def invite_state(inv: ChatInvite, now: dt.datetime | None = None) -> InviteState:
    now = now or dt.datetime.now(dt.UTC)
    if inv.revoked_at is not None:
        return "revoked"
    if inv.expires_at is not None and inv.expires_at <= now:
        return "expired"
    if inv.max_uses is not None and inv.uses >= inv.max_uses:
        return "used_up"
    return "active"


class InviteCreate(BaseModel):
    role: Literal["editor", "viewer"] = "editor"
    expires_in_hours: int | None = Field(default=24 * 7, ge=1, le=24 * 30)
    max_uses: int | None = Field(default=None, ge=1, le=100)


class InviteView(BaseModel):
    id: uuid.UUID
    role: Literal["editor", "viewer"]
    expires_at: dt.datetime | None
    max_uses: int | None
    uses: int
    state: InviteState
    created_at: dt.datetime


class InviteCreated(BaseModel):
    invite: InviteView
    token: str  # shown exactly once


class InvitePreview(BaseModel):
    chat_title: str
    owner_name: str
    owner_color: str
    role: Literal["editor", "viewer"]
    member_count: int
    state: InviteState


class AcceptOut(BaseModel):
    chat_id: uuid.UUID
    role: Literal["owner", "editor", "viewer"]
    already_member: bool


def view(inv: ChatInvite) -> InviteView:
    return InviteView(
        id=inv.id,
        role=inv.role,
        expires_at=inv.expires_at,
        max_uses=inv.max_uses,
        uses=inv.uses,
        state=invite_state(inv),
        created_at=inv.created_at,
    )


@router.post("/chats/{chat_id}/invites", response_model=InviteCreated, status_code=201)
async def create_invite(
    chat_id: uuid.UUID, body: InviteCreate, auth: CsrfAuthDep, db: DbDep
) -> InviteCreated:
    await require(db, chat_id, auth.user.id, Action.MANAGE_MEMBERS)
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now(dt.UTC)
    inv = ChatInvite(
        chat_id=chat_id,
        token_hash=hash_token(token),
        role=body.role,
        expires_at=now + dt.timedelta(hours=body.expires_in_hours)
        if body.expires_in_hours
        else None,
        max_uses=body.max_uses,
        created_by=auth.user.id,
        created_at=now,
        uses=0,
    )
    db.add(inv)
    await db.commit()
    return InviteCreated(invite=view(inv), token=token)


@router.get("/chats/{chat_id}/invites", response_model=list[InviteView])
async def list_invites(chat_id: uuid.UUID, auth: AuthDep, db: DbDep) -> list[InviteView]:
    await require(db, chat_id, auth.user.id, Action.MANAGE_MEMBERS)
    rows = (
        await db.scalars(
            select(ChatInvite)
            .where(ChatInvite.chat_id == chat_id)
            .order_by(ChatInvite.created_at.desc())
        )
    ).all()
    return [view(i) for i in rows]


@router.delete("/invites/{invite_id}", status_code=204)
async def revoke_invite(invite_id: uuid.UUID, auth: CsrfAuthDep, db: DbDep) -> Response:
    inv = await db.get(ChatInvite, invite_id)
    if inv is None:
        raise not_found("Invite not found")
    await require(db, inv.chat_id, auth.user.id, Action.MANAGE_MEMBERS)
    if inv.revoked_at is None:
        inv.revoked_at = dt.datetime.now(dt.UTC)
        await db.commit()
    return Response(status_code=204)


async def _find(db: DbDep, token: str, *, for_update: bool = False) -> ChatInvite:
    if not 20 <= len(token) <= 100:
        raise not_found("Invite not found")
    stmt = select(ChatInvite).where(ChatInvite.token_hash == hash_token(token))
    if for_update:
        stmt = stmt.with_for_update()
    inv = await db.scalar(stmt)
    if inv is None:
        raise not_found("Invite not found")
    return inv


@router.get("/invites/{token}/preview", response_model=InvitePreview)
async def preview_invite(token: str, request: Request, db: DbDep, redis: RedisDep) -> InvitePreview:
    """Public (no auth): what the invitee is about to join. No message content."""
    await limit_ip(request, redis, "invite_preview", limit=30, window_s=60)
    inv = await _find(db, token)
    chat = await db.get(Chat, inv.chat_id)
    owner = await db.get(User, chat.owner_id) if chat else None
    if chat is None or owner is None:
        raise not_found("Invite not found")
    counts = await member_counts(db, [chat.id])
    return InvitePreview(
        chat_title=chat.title,
        owner_name=owner.name,
        owner_color=owner.avatar_color,
        role=inv.role,
        member_count=counts.get(chat.id, 0),
        state=invite_state(inv),
    )


_STATE_ERRORS: dict[str, tuple[str, str]] = {
    "expired": ("invite_expired", "This invite link has expired. Ask the owner for a new one."),
    "used_up": (
        "invite_used_up",
        "This invite link has already been used the maximum number of times.",
    ),
    "revoked": ("invite_revoked", "This invite link was revoked by the owner."),
}


@router.post("/invites/{token}/accept", response_model=AcceptOut)
async def accept_invite(
    token: str, request: Request, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> AcceptOut:
    await limit_ip(request, redis, "invite_accept", limit=10, window_s=60)
    inv = await _find(db, token, for_update=True)
    chat_id = inv.chat_id
    # lock the chat row first-come-first-served (also orders the events we append)
    chat = await db.scalar(select(Chat).where(Chat.id == chat_id).with_for_update())
    if chat is None:
        raise not_found("Invite not found")

    member = await db.get(ChatMember, (chat_id, auth.user.id))
    if member is not None and member.removed_at is None:
        role = member.role  # read before rollback expires the instance
        await db.rollback()
        return AcceptOut(chat_id=chat_id, role=role, already_member=True)

    state = invite_state(inv)
    if state != "active":
        code, msg = _STATE_ERRORS[state]
        raise ApiError(410, code, msg)
    if await active_collaborators(db, chat_id) >= MAX_COLLABORATORS:
        raise ApiError(
            409, "chat_full", f"This chat already has {MAX_COLLABORATORS} collaborators."
        )

    inv.uses += 1
    now = dt.datetime.now(dt.UTC)
    if member is None:
        member = ChatMember(chat_id=chat_id, user_id=auth.user.id, role=inv.role, joined_at=now)
        db.add(member)
    else:  # previously removed or left: rejoin with the invite's role
        member.removed_at = None
        member.role = inv.role
        member.joined_at = now
    await db.flush()

    outbox = Outbox()
    await system_event(db, outbox, chat_id, f"{auth.user.name} joined as {inv.role}")
    await member_updated(db, outbox, chat_id, auth.user, member)
    await db.commit()
    await outbox.publish(redis)
    return AcceptOut(chat_id=chat_id, role=inv.role, already_member=False)
