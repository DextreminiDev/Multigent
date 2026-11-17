"""Invites + member management (REST). Live/WS behaviour is in test_ws.py."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChatEvent, ChatInvite, ChatMember, Message, User
from app.members.service import MAX_COLLABORATORS
from app.security.redaction import redact
from app.turns.prompt import HistoryItem, Participant, render_history, render_roster
from tests.conftest import LoginFn, StubValidator, UserClient, ready_chat

pytestmark = pytest.mark.integration
SM = async_sessionmaker[AsyncSession]


async def invite(owner: UserClient, cid: str, **body: Any) -> dict[str, Any]:
    r = await owner.post(f"/chats/{cid}/invites", json=body)
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def accept(u: UserClient, token: str) -> Any:
    return await u.post(f"/invites/{token}/accept")


async def event_types(sm: SM, cid: str) -> list[str]:
    async with sm() as s:
        return list(
            (
                await s.scalars(
                    select(ChatEvent.type)
                    .where(ChatEvent.chat_id == uuid.UUID(cid))
                    .order_by(ChatEvent.seq)
                )
            ).all()
        )


# ------------------------------------------------------------------ invites


async def test_invite_create_preview_accept(
    login: LoginFn, validator: StubValidator, client: AsyncClient, sessionmaker: SM
) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    created = await invite(owner, cid, role="viewer", expires_in_hours=24, max_uses=5)
    token = created["token"]
    assert len(token) >= 40
    assert created["invite"]["state"] == "active"
    listed = (await owner.get(f"/chats/{cid}/invites")).json()
    assert [i["id"] for i in listed] == [created["invite"]["id"]]
    assert "token" not in listed[0]  # never shown again

    # public preview: no auth, no message content
    pv = await client.get(f"/invites/{token}/preview")
    assert pv.status_code == 200
    assert pv.json() == {
        "chat_title": "T",
        "owner_name": "Alice",
        "owner_color": owner.user["avatar_color"],
        "role": "viewer",
        "member_count": 1,
        "state": "active",
    }

    r = await accept(bob, token)
    assert r.status_code == 200
    assert r.json() == {"chat_id": cid, "role": "viewer", "already_member": False}
    chats = (await bob.get("/chats")).json()
    assert [(c["id"], c["role"]) for c in chats] == [(cid, "viewer")]
    assert (await event_types(sessionmaker, cid))[-2:] == ["message.created", "member.updated"]
    async with sessionmaker() as s:
        sysmsg = (
            await s.scalars(
                select(Message).where(
                    Message.chat_id == uuid.UUID(cid), Message.kind == "system_event"
                )
            )
        ).one()
        assert sysmsg.content == "Bob joined as viewer"
        inv = await s.get(ChatInvite, uuid.UUID(created["invite"]["id"]))
        assert inv is not None and inv.uses == 1

    again = await accept(bob, token)  # idempotent, consumes nothing
    assert again.json()["already_member"] is True
    async with sessionmaker() as s:
        inv = await s.get(ChatInvite, uuid.UUID(created["invite"]["id"]))
        assert inv is not None and inv.uses == 1


async def test_only_owner_manages_invites(
    login: LoginFn, validator: StubValidator, sessionmaker: SM
) -> None:
    owner, ed = await login("Alice"), await login("Ed")
    cid = await ready_chat(owner, validator)
    token = (await invite(owner, cid, role="editor"))["token"]
    await accept(ed, token)
    assert (await ed.post(f"/chats/{cid}/invites", json={})).status_code == 403
    assert (await ed.get(f"/chats/{cid}/invites")).status_code == 403
    inv_id = (await owner.get(f"/chats/{cid}/invites")).json()[0]["id"]
    assert (await ed.delete(f"/invites/{inv_id}")).status_code == 403


async def test_accept_requires_login_and_csrf(
    login: LoginFn, validator: StubValidator, client: AsyncClient
) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    token = (await invite(owner, cid))["token"]
    assert (await client.post(f"/invites/{token}/accept")).status_code == 401
    assert (await bob.http.post(f"/invites/{token}/accept")).status_code == 403  # no CSRF


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        ({"expires_at": dt.datetime(2020, 1, 1, tzinfo=dt.UTC)}, "invite_expired"),
        ({"revoked_at": dt.datetime(2020, 1, 1, tzinfo=dt.UTC)}, "invite_revoked"),
        ({"max_uses": 1, "uses": 1}, "invite_used_up"),
    ],
)
async def test_dead_invites_rejected(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    client: AsyncClient,
    mutate: dict[str, Any],
    code: str,
) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    created = await invite(owner, cid)
    async with sessionmaker() as s:
        await s.execute(
            update(ChatInvite)
            .where(ChatInvite.id == uuid.UUID(created["invite"]["id"]))
            .values(**mutate)
        )
        await s.commit()
    pv = (await client.get(f"/invites/{created['token']}/preview")).json()
    assert pv["state"] == code.removeprefix("invite_")
    r = await accept(bob, created["token"])
    assert r.status_code == 410
    assert r.json()["error"]["code"] == code
    assert (await bob.get(f"/chats/{cid}")).status_code == 404


async def test_revoke_endpoint(login: LoginFn, validator: StubValidator) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    created = await invite(owner, cid)
    assert (await owner.delete(f"/invites/{created['invite']['id']}")).status_code == 204
    assert (await accept(bob, created["token"])).json()["error"]["code"] == "invite_revoked"


async def test_unknown_or_malformed_token_is_404(client: AsyncClient, login: LoginFn) -> None:
    bob = await login("Bob")
    assert (await client.get("/invites/" + "x" * 43 + "/preview")).status_code == 404
    assert (await client.get("/invites/short/preview")).status_code == 404
    assert (await accept(bob, "y" * 43)).status_code == 404


async def test_max_uses_holds_under_concurrency(
    login: LoginFn, validator: StubValidator, sessionmaker: SM
) -> None:
    owner = await login("Alice")
    people = [await login(f"P{i}") for i in range(5)]
    cid = await ready_chat(owner, validator)
    token = (await invite(owner, cid, max_uses=2))["token"]
    results = await asyncio.gather(*(accept(p, token) for p in people))
    assert sorted(r.status_code for r in results) == [200, 200, 410, 410, 410]
    async with sessionmaker() as s:
        n = len(
            (
                await s.scalars(
                    select(ChatMember).where(
                        ChatMember.chat_id == uuid.UUID(cid), ChatMember.role != "owner"
                    )
                )
            ).all()
        )
    assert n == 2


async def test_chat_full(login: LoginFn, validator: StubValidator, sessionmaker: SM) -> None:
    owner, late = await login("Alice"), await login("Late")
    cid = await ready_chat(owner, validator)
    async with sessionmaker() as s:
        for i in range(MAX_COLLABORATORS):
            u = User(email=f"filler{i}@example.com", name=f"F{i}", avatar_color="#000")
            s.add(u)
            await s.flush()
            s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=u.id, role="viewer"))
        await s.commit()
    r = await accept(late, (await invite(owner, cid))["token"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "chat_full"


async def test_preview_is_rate_limited(
    login: LoginFn, validator: StubValidator, client: AsyncClient
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    token = (await invite(owner, cid))["token"]
    codes = [(await client.get(f"/invites/{token}/preview")).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30
    assert codes[-1] == 429


def test_invite_tokens_are_redacted_from_logged_paths() -> None:
    token = "AbCdEf0123456789_-AbCdEf0123456789xyz"
    line = f'"POST /invites/{token}/accept HTTP/1.1" 200'
    assert token not in redact(line)


# ------------------------------------------------------------------ members


async def test_members_roles_and_removal(
    login: LoginFn, validator: StubValidator, sessionmaker: SM
) -> None:
    owner, ed, vi = await login("Alice"), await login("Ed"), await login("Vi")
    cid = await ready_chat(owner, validator)
    await accept(ed, (await invite(owner, cid, role="editor"))["token"])
    await accept(vi, (await invite(owner, cid, role="viewer"))["token"])

    members = (await ed.get(f"/chats/{cid}/members")).json()
    assert [(m["name"], m["role"]) for m in members] == [
        ("Alice", "owner"),
        ("Ed", "editor"),
        ("Vi", "viewer"),
    ]

    # role changes: owner only, never the owner's own role
    assert (
        await ed.patch(f"/chats/{cid}/members/{vi.id}", json={"role": "editor"})
    ).status_code == 403
    r = await owner.patch(f"/chats/{cid}/members/{owner.id}", json={"role": "viewer"})
    assert r.json()["error"]["code"] == "owner_role"
    r = await owner.patch(f"/chats/{cid}/members/{vi.id}", json={"role": "editor"})
    assert r.json()["role"] == "editor"
    assert (await vi.get(f"/chats/{cid}")).json()["role"] == "editor"

    # removal: owner only; editors can't remove others; owner can't leave
    assert (await ed.delete(f"/chats/{cid}/members/{vi.id}")).status_code == 403
    r = await owner.delete(f"/chats/{cid}/members/{owner.id}")
    assert r.json()["error"]["code"] == "owner_cannot_leave"
    assert (await owner.delete(f"/chats/{cid}/members/{vi.id}")).status_code == 204
    assert (await vi.get(f"/chats/{cid}")).status_code == 404
    body = {"client_msg_id": str(uuid.uuid4()), "content": "still here?"}
    assert (await vi.post(f"/chats/{cid}/messages", json=body)).status_code == 404

    # editor leaves on their own
    assert (await ed.delete(f"/chats/{cid}/members/{ed.id}")).status_code == 204
    async with sessionmaker() as s:
        texts = (
            await s.scalars(
                select(Message.content)
                .where(Message.chat_id == uuid.UUID(cid), Message.kind == "system_event")
                .order_by(Message.seq)
            )
        ).all()
    assert texts[-2:] == ["Vi was removed by the owner", "Ed left the chat"]
    assert [m["name"] for m in (await owner.get(f"/chats/{cid}/members")).json()] == ["Alice"]


async def test_removed_member_can_rejoin_with_new_invite(
    login: LoginFn, validator: StubValidator
) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    await accept(bob, (await invite(owner, cid, role="editor"))["token"])
    await owner.delete(f"/chats/{cid}/members/{bob.id}")
    r = await accept(bob, (await invite(owner, cid, role="viewer"))["token"])
    assert r.json() == {"chat_id": cid, "role": "viewer", "already_member": False}


async def test_former_member_messages_keep_attribution(
    login: LoginFn, validator: StubValidator
) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    await accept(bob, (await invite(owner, cid))["token"])
    body = {"client_msg_id": str(uuid.uuid4()), "content": "bye all"}
    assert (await bob.post(f"/chats/{cid}/messages", json=body)).status_code == 201
    await owner.delete(f"/chats/{cid}/members/{bob.id}")
    msgs = (await owner.get(f"/chats/{cid}/messages")).json()
    mine = next(m for m in msgs if m["content"] == "bye all")
    assert mine["author_name"] == "Bob"
    assert mine["author_id"] == bob.id  # kept; the UI labels non-members "former member"


# ------------------------------------------------------------------ prompt


def test_namesakes_are_disambiguated_and_former_members_labelled() -> None:
    a, b, gone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    people = [Participant(a, "Sam", "owner"), Participant(b, "sam", "editor")]
    t = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    hist = [
        HistoryItem(1, "human", "first", t, a, "Sam"),
        HistoryItem(2, "human", "second", t, b, "sam"),
        HistoryItem(3, "human", "third", t, gone, "Sam"),
    ]
    out = render_history(hist, people)[0].content
    assert 'from="Sam" role="owner"' in out
    assert 'from="sam (2)" role="editor"' in out
    assert 'from="Sam" role="former member"' in out
    assert "- sam (2) (can send messages)" in render_roster(people)
