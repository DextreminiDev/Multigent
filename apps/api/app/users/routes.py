"""Account deletion (prompt §7 edge case 20; retention rule in docs/SECURITY.md).

DELETE /me:
- every chat the user owns is hard-deleted (keys, messages, events: one cascade);
- in chats owned by others the user leaves: membership removed, "X left" event,
  live sockets kicked. Their past messages stay, attributed by the name snapshot
  taken when each was sent, so the conversation still makes sense to others;
- the profile is scrubbed (email/name replaced), sign-in identities deleted, and
  all sessions revoked. The row itself stays (soft-deleted) so foreign keys hold.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Response
from sqlalchemy import delete, select

from app.auth.sessions import SESSION_COOKIE
from app.db.models import AuthIdentity, Chat, ChatMember, User
from app.deps import CsrfAuthDep, DbDep, RedisDep, SessionsDep, SettingsDep
from app.events import Outbox
from app.members.service import kick, member_updated, system_event

router = APIRouter(tags=["auth"])


@router.delete("/me", status_code=204)
async def delete_account(
    auth: CsrfAuthDep,
    db: DbDep,
    redis: RedisDep,
    sessions: SessionsDep,
    settings: SettingsDep,
) -> Response:
    user = await db.get(User, auth.user.id, with_for_update=True)
    assert user is not None
    owned = list((await db.scalars(select(Chat).where(Chat.owner_id == user.id))).all())
    owned_ids = [c.id for c in owned]
    stmt = select(ChatMember).where(ChatMember.user_id == user.id, ChatMember.removed_at.is_(None))
    if owned_ids:
        stmt = stmt.where(ChatMember.chat_id.not_in(owned_ids))
    memberships = list((await db.scalars(stmt)).all())

    outbox = Outbox()
    now = dt.datetime.now(dt.UTC)
    for m in memberships:
        m.removed_at = now
        await system_event(db, outbox, m.chat_id, f"{user.name} left the chat")
        await member_updated(db, outbox, m.chat_id, user, m, removed=True)
    for chat in owned:
        outbox.push_ephemeral(chat.id, "chat.deleted", {})
        await db.delete(chat)

    await db.execute(delete(AuthIdentity).where(AuthIdentity.user_id == user.id))
    user.deleted_at = now
    user.email = f"deleted-{user.id}@deleted.invalid"
    user.name = "Deleted user"
    await db.commit()

    await outbox.publish(redis)
    for m in memberships:
        await kick(redis, user.id, m.chat_id, "account_deleted")
    await sessions.delete_all_for_user(user.id)
    resp = Response(status_code=204)
    resp.delete_cookie(SESSION_COOKIE, domain=settings.cookie_domain or None, path="/")
    return resp
