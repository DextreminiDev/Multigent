"""Reply modes + message tags: @ai asks the AI, @skip is context only, @off is
humans-only (never reaches the model). Untagged: reply in "always" mode, context in
"tags" mode. The most restrictive tag wins."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChatMember, Message, Turn
from app.messages.tags import parse_intent, tags_in
from app.providers.fake import FakeProvider
from app.providers.registry import ProviderRegistry
from app.turns.prompt import HistoryItem, Participant, render_history
from app.turns.runner import TurnRunner
from app.turns.schedule import DUE_KEY
from tests.conftest import LoginFn, StubValidator, UserClient, ready_chat

SM = async_sessionmaker[AsyncSession]
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    ("text", "always", "tags"),
    [
        ("hello", "reply", "context"),
        ("@ai what's 2+2?", "reply", "reply"),
        ("what's 2+2? @AI", "reply", "reply"),
        ("@skip just noting this", "context", "context"),
        ("@off lunch?", "off", "off"),
        ("@ai and @skip", "context", "context"),  # most restrictive wins
        ("@ai @off", "off", "off"),
        ("@skip @off", "off", "off"),
        ("mail bob@ai.com", "reply", "context"),  # emails aren't tags
        ("@aid @skipper @offline", "reply", "context"),  # whole words only
        ("(@ai) please", "reply", "reply"),
        ("@ai, thoughts?", "reply", "reply"),
    ],
)
def test_parse_intent(text: str, always: str, tags: str) -> None:
    assert parse_intent(text, "always") == always
    assert parse_intent(text, "tags") == tags


def test_tags_in() -> None:
    assert tags_in("@Ai hi @OFF x@skip") == {"ai", "off"}


def test_prompt_marks_group_messages() -> None:
    a = uuid.uuid4()
    people = [Participant(user_id=a, name="Al", role="owner")]
    items = [
        HistoryItem(seq=1, kind="human", content="fyi", created_at=T0, author_id=a, to_ai=False),
        HistoryItem(seq=2, kind="human", content="@ai q", created_at=T0, author_id=a),
    ]
    [msg] = render_history(items, people)
    assert '<message from="Al" role="owner" time="00:00" to="group">\nfyi' in msg.content
    assert '<message from="Al" role="owner" time="00:00">\n@ai q' in msg.content
    # the demo model, like a real one, only answers the unmarked message
    reply = "".join(FakeProvider.echo_reply(_req(msg.content)))
    assert "@ai q" in reply and "fyi" not in reply


def _req(content: str) -> Any:
    from app.providers.base import Msg, ProviderRequest

    return ProviderRequest(
        provider="fake", model="m", system="", messages=[Msg("user", content)], max_tokens=64
    )


# ------------------------------------------------------------------ helpers


async def send(u: UserClient, cid: str, text: str) -> Any:
    return await u.post(
        f"/chats/{cid}/messages", json={"client_msg_id": str(uuid.uuid4()), "content": text}
    )


async def turn_count(sm: SM, cid: str) -> int:
    async with sm() as s:
        return len((await s.scalars(select(Turn).where(Turn.chat_id == uuid.UUID(cid)))).all())


# ------------------------------------------------------------------ integration


@pytest.mark.integration
async def test_skip_and_off_never_call_the_ai(
    login: LoginFn, validator: StubValidator, sessionmaker: SM, runner: TurnRunner, redis: Redis
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    assert (await owner.get(f"/chats/{cid}")).json()["reply_mode"] == "always"
    await redis.delete(DUE_KEY)  # choosing the model during setup woke the chat

    for text, intent in [("@skip noted", "context"), ("@off secret plans", "off")]:
        r = await send(owner, cid, text)
        assert r.status_code == 201
        msg = r.json()["message"]
        assert (msg["intent"], msg["status"]) == (intent, "done")  # nothing to wait for
    assert await redis.zscore(DUE_KEY, cid) is None  # the turn engine wasn't woken
    await runner.run_chat(uuid.UUID(cid))
    assert await turn_count(sessionmaker, cid) == 0


@pytest.mark.integration
async def test_off_messages_never_reach_the_model(
    login: LoginFn,
    validator: StubValidator,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await send(owner, cid, "@off the password is hunter2")
    await send(owner, cid, "@skip we're planning a trip")
    r = await send(owner, cid, "where should we go?")
    assert r.json()["message"]["intent"] == "reply"
    await runner.run_chat(uuid.UUID(cid))

    fake = providers.get("fake")
    assert isinstance(fake, FakeProvider)
    [req] = fake.calls
    prompt = req.system + "".join(m.content for m in req.messages)
    assert "hunter2" not in prompt
    assert 'to="group">\n@skip we\'re planning a trip' in prompt
    assert "where should we go?" in prompt


@pytest.mark.integration
async def test_tags_mode_only_answers_at_ai(
    login: LoginFn, validator: StubValidator, sessionmaker: SM, runner: TurnRunner
) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    async with sessionmaker() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(bob.id), role="editor"))
        await s.commit()

    # only the owner switches modes
    r = await bob.patch(f"/chats/{cid}", json={"reply_mode": "tags"})
    assert r.status_code == 403
    r = await owner.patch(f"/chats/{cid}", json={"reply_mode": "tags"})
    assert r.status_code == 200 and r.json()["reply_mode"] == "tags"
    assert (await bob.get(f"/chats/{cid}")).json()["reply_mode"] == "tags"  # visible to all
    assert (await owner.patch(f"/chats/{cid}", json={"reply_mode": "never"})).status_code == 422

    r = await send(bob, cid, "hey alice, lunch?")
    assert r.json()["message"]["intent"] == "context"
    await runner.run_chat(uuid.UUID(cid))
    assert await turn_count(sessionmaker, cid) == 0

    r = await send(bob, cid, "@ai suggest a lunch spot")
    assert r.json()["message"]["intent"] == "reply"
    await runner.run_chat(uuid.UUID(cid))
    async with sessionmaker() as s:
        turns = (await s.scalars(select(Turn).where(Turn.chat_id == uuid.UUID(cid)))).all()
        ai = (
            await s.scalars(
                select(Message).where(Message.chat_id == uuid.UUID(cid), Message.kind == "ai")
            )
        ).all()
    assert [t.status for t in turns] == ["completed"]
    assert len(ai) == 1 and "suggest a lunch spot" in ai[0].content
    assert "lunch?" not in ai[0].content.replace("lunch spot", "")  # context wasn't "answered"

    # switching back: untagged messages reply again
    await owner.patch(f"/chats/{cid}", json={"reply_mode": "always"})
    assert (await send(bob, cid, "thanks")).json()["message"]["intent"] == "reply"


@pytest.mark.integration
async def test_mode_change_is_broadcast(
    login: LoginFn, validator: StubValidator, redis: Redis
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    ps: Any = redis.pubsub()
    await ps.subscribe(f"chat:{cid}:events")
    await ps.get_message(timeout=1)
    await owner.patch(f"/chats/{cid}", json={"reply_mode": "tags"})
    frames = []
    while m := await ps.get_message(ignore_subscribe_messages=True, timeout=0.5):
        frames.append(json.loads(m["data"]))
    await ps.aclose()
    upd = [f for f in frames if f.get("type") == "chat.updated"]
    assert upd and upd[0]["payload"]["reply_mode"] == "tags"
    assert upd[0]["payload"]["changed"] == ["reply_mode"]


@pytest.mark.integration
async def test_plain_chat_has_its_own_rate_limit(
    login: LoginFn, validator: StubValidator, app: FastAPI
) -> None:
    app.state.settings = app.state.settings.model_copy(
        update={"message_rate_per_min": 2, "chat_rate_per_min": 4}
    )
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    assert [(await send(owner, cid, f"q{i}")).status_code for i in range(3)] == [201, 201, 429]
    # AI bucket empty, but non-AI messages still go through (until their own limit)
    codes = [(await send(owner, cid, f"@skip n{i}")).status_code for i in range(5)]
    assert codes == [201, 201, 201, 201, 429]


@pytest.mark.integration
async def test_member_cap_only_blocks_ai_messages(
    login: LoginFn, validator: StubValidator, sessionmaker: SM, runner: TurnRunner
) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    async with sessionmaker() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(bob.id), role="editor"))
        await s.commit()
    await send(bob, cid, "hello")
    await runner.run_chat(uuid.UUID(cid))
    await owner.patch(f"/chats/{cid}/members/{bob.id}", json={"member_token_cap": 1})
    blocked = await send(bob, cid, "more please")
    assert blocked.json()["error"]["code"] == "member_cap"
    assert "@skip" in blocked.json()["error"]["message"]
    assert (await send(bob, cid, "@off ok, just chatting")).status_code == 201
