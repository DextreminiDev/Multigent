"""Mentions and off the record: the AI replies only when mentioned; off-the-record
messages never reach it; mentions are ranges over plain text (UTF-16 offsets) and
become the prompt's `to` attribute."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import ChatMember, Message, Turn
from app.errors import ApiError
from app.messages.mentions import MentionIn, intent_for, normalize, render_for_prompt, spans
from app.providers.fake import FakeProvider
from app.providers.registry import ProviderRegistry
from app.turns.prompt import HistoryItem, Participant, render_history
from app.turns.runner import TurnRunner
from app.turns.schedule import DUE_KEY
from tests.conftest import LoginFn, StubValidator, UserClient, ready_chat

SM = async_sessionmaker[AsyncSession]
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
ALICE, BOB, CAROL = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
NAMES = {ALICE: "Alice", BOB: "Bob", CAROL: "Carol"}


def m(type_: str, offset: int, length: int, user: uuid.UUID | None = None) -> MentionIn:
    return MentionIn(type=type_, offset=offset, length=length, user_id=user)


# ------------------------------------------------------------------ pure units


def test_normalize_keeps_valid_mentions_with_name_snapshots() -> None:
    text = "@AI @Bob what do you both think?"
    kept = normalize(text, [m("ai", 0, 3), m("user", 4, 4, BOB)], sender_id=ALICE, members=NAMES)
    assert kept == [
        {"type": "ai", "offset": 0, "length": 3},
        {"type": "user", "offset": 4, "length": 4, "user_id": str(BOB), "name": "Bob"},
    ]


@pytest.mark.parametrize(
    "mention",
    [
        m("ai", 30, 3),  # out of bounds
        m("ai", 1, 3),  # doesn't start at "@"
        m("user", 4, 4, uuid.uuid4()),  # not a member (or removed)
        m("user", 4, 4, ALICE),  # the sender
        m("user", 4, 4, None),  # no user id
    ],
)
def test_normalize_drops_bad_mentions_and_keeps_the_text(mention: MentionIn) -> None:
    assert normalize("@AI @Bob hi", [mention], sender_id=ALICE, members=NAMES) == []


def test_overlapping_mentions_keep_the_first() -> None:
    kept = normalize(
        "@Bob hi", [m("user", 0, 4, BOB), m("user", 0, 2, CAROL)], sender_id=ALICE, members=NAMES
    )
    assert [k["user_id"] for k in kept] == [str(BOB)]


def test_offsets_are_utf16_like_the_browser() -> None:
    text = "🎉 party @Bob"  # the emoji is 2 UTF-16 units but 1 Python char
    assert text.index("@") == 8  # Python index
    [s] = spans(text, [{"type": "user", "offset": 9, "length": 4}])
    assert text[s.start : s.end] == "@Bob"
    assert spans(text, [{"type": "user", "offset": 8, "length": 4}]) == []  # Python-style: wrong


def test_intent_rules() -> None:
    ai = [{"type": "ai"}]
    bob = [{"type": "user"}]
    assert intent_for([], off=False) == "context"
    assert intent_for(bob, off=False) == "context"
    assert intent_for([{"type": "everyone"}], off=False) == "context"
    assert intent_for(ai + bob, off=False) == "reply"
    assert intent_for([], off=True) == "off"
    assert intent_for(bob, off=True) == "off"
    with pytest.raises(ApiError) as e:
        intent_for(ai, off=True)
    assert e.value.code == "off_with_ai"


@pytest.mark.parametrize(
    ("mentions", "intent", "body", "to"),
    [
        ([], "context", "hotel's booked", ["(everyone)"]),
        ([], "reply", "hotel's booked", ["(assistant)"]),  # legacy row: asked the AI
        ([{"type": "everyone", "offset": 0, "length": 9}], "context", "@everyone hi",
         ["(everyone)"]),
        (
            [{"type": "ai", "offset": 0, "length": 3},
             {"type": "user", "offset": 4, "length": 4, "user_id": str(BOB), "name": "Bob"}],
            "reply",
            "@assistant @Bob hi",
            ["(assistant)", "Bob"],
        ),
        (
            [{"type": "user", "offset": 0, "length": 4, "user_id": str(BOB), "name": "Bob"},
             {"type": "user", "offset": 5, "length": 4, "user_id": str(BOB), "name": "Bob"}],
            "context",
            "@Bob @Bob hi",
            ["Bob"],  # de-duplicated
        ),
    ],
)  # fmt: skip
def test_render_for_prompt(
    mentions: list[dict[str, Any]], intent: str, body: str, to: list[str]
) -> None:
    text = {
        "@assistant @Bob hi": "@AI @Bob hi",
        "@Bob @Bob hi": "@Bob @Bob hi",
        "@everyone hi": "@everyone hi",
    }.get(body, body)
    parts, got_to = render_for_prompt(text, mentions, {BOB: "Bob"}, intent=intent)
    assert "".join(seg for seg, _ in parts) == body
    assert got_to == to


def test_former_member_mention_uses_the_snapshot_name() -> None:
    mention = {"type": "user", "offset": 0, "length": 4, "user_id": str(BOB), "name": "Bob"}
    parts, to = render_for_prompt("@Bob thanks", [mention], {}, intent="context")
    assert parts[0] == ("@Bob", True) and to == ["Bob"]


def test_who_said_what_to_whom_with_three_people() -> None:
    people = [
        Participant(ALICE, "Alice", "owner"),
        Participant(BOB, "Bob", "editor"),
        Participant(CAROL, "Carol", "editor"),
    ]

    def say(seq: int, who: uuid.UUID, text: str, *, to: uuid.UUID | None = None) -> HistoryItem:
        mentions: tuple[dict[str, Any], ...] = ()
        if to is not None:
            n = len(NAMES[to]) + 1
            mentions = ({"type": "user", "offset": 0, "length": n, "user_id": str(to)},)
        return HistoryItem(seq=seq, kind="human", content=text, created_at=T0, author_id=who,
                           author_name=NAMES[who], intent="context", mentions=mentions)  # fmt: skip

    ask = HistoryItem(
        seq=4, kind="human", content="@AI what did Alice ask Bob?", created_at=T0,
        author_id=CAROL, author_name="Carol", intent="reply",
        mentions=({"type": "ai", "offset": 0, "length": 3},),
    )  # fmt: skip
    [msg] = render_history(
        [say(1, ALICE, "@Bob can you book it?", to=BOB), say(2, BOB, "@Alice done", to=ALICE),
         say(3, CAROL, "nice"), ask],
        people,
    )  # fmt: skip
    assert '<message from="Alice" role="owner" time="00:00" to="Bob">\n@Bob can you book it?' in (
        msg.content
    )
    assert '<message from="Bob" role="editor" time="00:00" to="Alice">\n@Alice done' in msg.content
    assert '<message from="Carol" role="editor" time="00:00" to="(everyone)">\nnice' in msg.content
    assert 'to="(assistant)">\n@assistant what did Alice ask Bob?' in msg.content


def test_special_labels_cannot_collide_with_names() -> None:
    """A human called "assistant" or "everyone" keeps their name; the special
    recipients are parenthesized, which display names can never be."""
    sam = uuid.uuid4()
    people = [Participant(ALICE, "Alice", "owner"), Participant(sam, "assistant", "editor")]
    item = HistoryItem(
        seq=1, kind="human", content="@assistant hi", created_at=T0, author_id=ALICE,
        intent="context",
        mentions=({"type": "user", "offset": 0, "length": 10, "user_id": str(sam)},),
    )  # fmt: skip
    [msg] = render_history([item], people)
    assert 'to="assistant">' in msg.content and "(assistant)" not in msg.content


def test_mention_text_is_still_escaped() -> None:
    item = HistoryItem(
        seq=1, kind="human", content="@AI <b>&</b>", created_at=T0, author_id=ALICE,
        mentions=({"type": "ai", "offset": 0, "length": 3},),
    )  # fmt: skip
    [msg] = render_history([item], [Participant(ALICE, "Alice", "owner")])
    assert "@assistant &lt;b&gt;&amp;&lt;/b&gt;" in msg.content


def test_demo_model_answers_only_messages_to_the_assistant() -> None:
    from app.providers.base import Msg, ProviderRequest

    content = (
        '<message from="Al" role="owner" time="00:00" to="(everyone)">\nfyi\n</message>\n\n'
        '<message from="Al" role="owner" time="00:00" to="(assistant), Bob">\n@assistant q\n'
        "</message>"
    )
    req = ProviderRequest(provider="fake", model="m", system="", messages=[Msg("user", content)],
                          max_tokens=64)  # fmt: skip
    reply = "".join(FakeProvider.echo_reply(req))
    assert "@assistant q" in reply and "fyi" not in reply


# ------------------------------------------------------------------ integration


async def send(u: UserClient, cid: str, text: str, mentions: list[dict[str, Any]] | None = None,
               off: bool = False) -> Any:  # fmt: skip
    body = {"client_msg_id": str(uuid.uuid4()), "content": text, "mentions": mentions or [],
            "off": off}  # fmt: skip
    return await u.post(f"/chats/{cid}/messages", json=body)


Trio = tuple[UserClient, UserClient, str]
AI_M = {"type": "ai", "offset": 0, "length": 3}


def bob_m(bob: UserClient) -> dict[str, Any]:
    return {"type": "user", "offset": 0, "length": 4, "user_id": bob.id}


async def with_bob(login: LoginFn, validator: StubValidator, sm: SM) -> Trio:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    async with sm() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(bob.id), role="editor"))
        await s.commit()
    return owner, bob, cid


async def turn_count(sm: SM, cid: str) -> int:
    async with sm() as s:
        return len((await s.scalars(select(Turn).where(Turn.chat_id == uuid.UUID(cid)))).all())


@pytest.mark.integration
async def test_only_a_mention_of_the_ai_gets_a_reply(
    login: LoginFn, validator: StubValidator, sessionmaker: SM, runner: TurnRunner, redis: Redis
) -> None:
    owner, bob, cid = await with_bob(login, validator, sessionmaker)
    await redis.delete(DUE_KEY)  # choosing the model during setup woke the chat

    plain = (await send(owner, cid, "hotel's booked")).json()["message"]
    assert (plain["intent"], plain["status"]) == ("context", "done")
    typed = (await send(owner, cid, "@ai typed by hand is just text")).json()["message"]
    assert typed["intent"] == "context" and typed["mentions"] == []
    to_bob = (await send(owner, cid, "@Bob can you pay?", [bob_m(bob)])).json()
    assert to_bob["message"]["intent"] == "context"
    assert to_bob["message"]["mentions"][0]["name"] == "Bob"
    assert await redis.zscore(DUE_KEY, cid) is None  # nothing woke the AI
    await runner.run_chat(uuid.UUID(cid))
    assert await turn_count(sessionmaker, cid) == 0

    r = await send(owner, cid, "@AI pick a restaurant", [{"type": "ai", "offset": 0, "length": 3}])
    assert r.json()["message"]["intent"] == "reply" and r.json()["message"]["status"] == "pending"
    await runner.run_chat(uuid.UUID(cid))
    assert await turn_count(sessionmaker, cid) == 1


@pytest.mark.integration
async def test_off_the_record_never_reaches_the_model(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner, bob, cid = await with_bob(login, validator, sessionmaker)
    r = await send(owner, cid, "@Bob door code is 4321", [bob_m(bob)], off=True)
    msg = r.json()["message"]
    assert (msg["intent"], msg["status"], msg["mentions"][0]["user_id"]) == ("off", "done", bob.id)

    blocked = await send(owner, cid, "@AI secret", [{"type": "ai", "offset": 0, "length": 3}],
                         off=True)  # fmt: skip
    assert blocked.status_code == 422
    assert blocked.json()["error"]["code"] == "off_with_ai"
    async with sessionmaker() as s:  # rejected, not saved
        assert not (await s.scalars(select(Message).where(Message.content == "@AI secret"))).all()

    await send(bob, cid, "@AI what's the plan?", [{"type": "ai", "offset": 0, "length": 3}])
    await runner.run_chat(uuid.UUID(cid))
    fake = providers.get("fake")
    assert isinstance(fake, FakeProvider)
    [req] = fake.calls
    prompt = req.system + "".join(x.content for x in req.messages)
    assert "4321" not in prompt
    assert 'to="(assistant)">\n@assistant what&#x27;s' not in prompt  # sanity: not escaped
    assert "@assistant what's the plan?" in prompt


@pytest.mark.integration
async def test_removed_member_mention_is_dropped_not_rejected(
    login: LoginFn, validator: StubValidator, sessionmaker: SM
) -> None:
    owner, bob, cid = await with_bob(login, validator, sessionmaker)
    async with sessionmaker() as s:
        await s.execute(
            update(ChatMember)
            .where(ChatMember.chat_id == uuid.UUID(cid), ChatMember.user_id == uuid.UUID(bob.id))
            .values(removed_at=dt.datetime.now(dt.UTC))
        )
        await s.commit()
    r = await send(owner, cid, "@Bob still there?",
                   [{"type": "user", "offset": 0, "length": 4, "user_id": bob.id}])  # fmt: skip
    assert r.status_code == 201
    assert r.json()["message"]["mentions"] == [] and r.json()["message"]["content"] == (
        "@Bob still there?"
    )


@pytest.mark.integration
async def test_mentions_are_broadcast_and_reply_mode_is_gone(
    login: LoginFn, validator: StubValidator, sessionmaker: SM, redis: Redis
) -> None:
    owner, _bob, cid = await with_bob(login, validator, sessionmaker)
    ps: Any = redis.pubsub()
    await ps.subscribe(f"chat:{cid}:events")
    await ps.get_message(timeout=1)
    await send(
        owner, cid, "@everyone dinner at 8", [{"type": "everyone", "offset": 0, "length": 9}]
    )
    frames = []
    while f := await ps.get_message(ignore_subscribe_messages=True, timeout=0.5):
        frames.append(json.loads(f["data"]))
    await ps.aclose()
    [created] = [f for f in frames if f["type"] == "message.created"]
    assert created["payload"]["message"]["mentions"] == [
        {"type": "everyone", "offset": 0, "length": 9}
    ]
    chat = (await owner.get(f"/chats/{cid}")).json()
    assert "reply_mode" not in chat
    r = await owner.patch(f"/chats/{cid}", json={"reply_mode": "tags"})
    assert r.status_code == 200 and "reply_mode" not in r.json()  # ignored


@pytest.mark.integration
async def test_member_cap_only_blocks_messages_to_the_ai(
    login: LoginFn, validator: StubValidator, sessionmaker: SM, runner: TurnRunner
) -> None:
    owner, bob, cid = await with_bob(login, validator, sessionmaker)
    await send(bob, cid, "@AI hello", [{"type": "ai", "offset": 0, "length": 3}])
    await runner.run_chat(uuid.UUID(cid))
    await owner.patch(f"/chats/{cid}/members/{bob.id}", json={"member_token_cap": 1})
    blocked = await send(bob, cid, "@AI more", [{"type": "ai", "offset": 0, "length": 3}])
    assert blocked.json()["error"]["code"] == "member_cap"
    assert (await send(bob, cid, "ok, just chatting")).status_code == 201
