"""Chat loading + membership/authorization helpers and view builders."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.chats.permissions import Action, Role, authorize
from app.chats.schemas import ChatView, KeyView, UserRef
from app.db.models import Chat, ChatKey, ChatMember, User
from app.errors import forbidden, not_found


@dataclass
class Membership:
    chat: Chat
    member: ChatMember

    @property
    def role(self) -> Role:
        return self.member.role  # type: ignore[return-value]


async def load_membership(
    db: AsyncSession, chat_id: uuid.UUID, user_id: uuid.UUID, *, for_update: bool = False
) -> Membership:
    """404 (not 403) for non-members so chat ids can't be probed."""
    stmt = (
        select(Chat, ChatMember)
        .join(ChatMember, ChatMember.chat_id == Chat.id)
        .where(Chat.id == chat_id, ChatMember.user_id == user_id, ChatMember.removed_at.is_(None))
    )
    if for_update:
        stmt = stmt.with_for_update(of=Chat)
    row = (await db.execute(stmt)).first()
    if row is None:
        raise not_found("Chat not found")
    return Membership(chat=row[0], member=row[1])


async def require(
    db: AsyncSession,
    chat_id: uuid.UUID,
    user_id: uuid.UUID,
    action: Action,
    *,
    for_update: bool = False,
) -> Membership:
    m = await load_membership(db, chat_id, user_id, for_update=for_update)
    if not authorize(m.role, action):
        raise forbidden()
    return m


async def member_counts(db: AsyncSession, chat_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not chat_ids:
        return {}
    rows = await db.execute(
        select(ChatMember.chat_id, func.count())
        .where(ChatMember.chat_id.in_(chat_ids), ChatMember.removed_at.is_(None))
        .group_by(ChatMember.chat_id)
    )
    return {cid: int(n) for cid, n in rows.all()}


def key_view(key: ChatKey | None, *, owner: bool) -> KeyView | None:
    if key is None:
        return None
    return KeyView(
        provider=key.provider,
        status=key.status,
        last4=key.last4 if owner else None,
        validated_at=key.validated_at,
    )


def chat_view(
    chat: Chat, role: Role, owner: User, key: ChatKey | None, member_count: int
) -> ChatView:
    is_owner = role == "owner"
    return ChatView(
        id=chat.id,
        title=chat.title,
        role=role,
        owner=UserRef(id=owner.id, name=owner.name, avatar_color=owner.avatar_color),
        status=chat.status,
        provider=chat.provider,
        model=chat.model,
        max_output_tokens=chat.max_output_tokens,
        member_count=member_count,
        key=key_view(key, owner=is_owner),
        last_seq=chat.last_seq,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        custom_instructions=chat.custom_instructions if is_owner else None,
        daily_token_cap=chat.daily_token_cap if is_owner else None,
        base_url=chat.base_url if is_owner else None,
    )


async def build_chat_view(db: AsyncSession, m: Membership) -> ChatView:
    owner = await db.get(User, m.chat.owner_id)
    assert owner is not None
    key = await db.get(ChatKey, m.chat.id)
    counts = await member_counts(db, [m.chat.id])
    return chat_view(m.chat, m.role, owner, key, counts.get(m.chat.id, 0))
