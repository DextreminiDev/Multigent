"""Turn engine integration: send path, batching, claim, streaming, fenced finish,
failures, orphan recovery, scheduler (real Postgres + Redis, FakeProvider)."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import (
    Chat,
    ChatEvent,
    ChatKey,
    ChatMember,
    Message,
    Turn,
    TurnMessage,
    UsageEvent,
)
from app.providers.errors import ProviderUnavailable
from app.providers.fake import FakeScript
from app.providers.registry import ProviderRegistry
from app.turns.lock import lock_key
from app.turns.runner import TurnRunner, buffer_key
from app.turns.schedule import DUE_KEY
from app.worker import Worker
from tests.conftest import LoginFn, StubValidator, UserClient, ask_ai, ready_chat

pytestmark = pytest.mark.integration

SM = async_sessionmaker[AsyncSession]


async def send(u: UserClient, cid: str, text: str, cmid: str | None = None) -> dict[str, Any]:
    r = await u.post(
        f"/chats/{cid}/messages", json={"client_msg_id": cmid or str(uuid.uuid4()), **ask_ai(text)}
    )
    assert r.status_code in (200, 201), r.text
    return r.json()  # type: ignore[no-any-return]


async def events(sm: SM, cid: str) -> list[tuple[int, str, dict[str, Any]]]:
    async with sm() as s:
        rows = (
            await s.scalars(
                select(ChatEvent).where(ChatEvent.chat_id == uuid.UUID(cid)).order_by(ChatEvent.seq)
            )
        ).all()
        return [(e.seq, e.type, e.payload) for e in rows]


async def messages(sm: SM, cid: str) -> list[Message]:
    async with sm() as s:
        return list(
            (
                await s.scalars(
                    select(Message).where(Message.chat_id == uuid.UUID(cid)).order_by(Message.seq)
                )
            ).all()
        )


async def turns(sm: SM, cid: str) -> list[Turn]:
    async with sm() as s:
        return list(
            (
                await s.scalars(
                    select(Turn).where(Turn.chat_id == uuid.UUID(cid)).order_by(Turn.started_at)
                )
            ).all()
        )


async def add_member(sm: SM, cid: str, u: UserClient, role: str) -> None:
    async with sm() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(u.id), role=role))
        await s.commit()


# ------------------------------------------------------------------ send path


async def test_send_creates_pending_message_event_and_wakeup(
    login: LoginFn, validator: StubValidator, sessionmaker: SM, redis: Redis, settings: Settings
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    body = await send(owner, cid, "  hello  \n")
    msg = body["message"]
    assert body["created"] is True
    assert msg["status"] == "pending"
    assert msg["author_name"] == "Alice"
    assert msg["content"] == "@AI   hello  "  # inner whitespace kept, trailing newlines stripped
    assert msg["intent"] == "reply"
    [mention] = msg["mentions"]
    assert (mention["type"], mention["offset"], mention["length"]) == ("ai", 0, 3)
    evs = await events(sessionmaker, cid)
    assert evs[-1][1] == "message.created"
    assert evs[-1][0] == msg["seq"]
    assert evs[-1][2]["message"]["id"] == msg["id"]
    score = await redis.zscore(DUE_KEY, cid)
    assert score is not None  # debounced wake-up scheduled


async def test_duplicate_client_msg_id_is_idempotent(
    login: LoginFn, validator: StubValidator, sessionmaker: SM
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    cmid = str(uuid.uuid4())
    results = await asyncio.gather(*(send(owner, cid, "once", cmid) for _ in range(10)))
    assert sum(r["created"] for r in results) == 1
    assert len({r["message"]["id"] for r in results}) == 1
    assert len([m for m in await messages(sessionmaker, cid) if m.kind == "human"]) == 1


async def test_send_authorization_and_limits(
    login: LoginFn, validator: StubValidator, sessionmaker: SM
) -> None:
    owner, viewer, stranger = await login("Alice"), await login("Vi"), await login("Zed")
    cid = await ready_chat(owner, validator)
    await add_member(sessionmaker, cid, viewer, "viewer")
    body = {"client_msg_id": str(uuid.uuid4()), "content": "hi"}
    assert (await viewer.post(f"/chats/{cid}/messages", json=body)).status_code == 403
    assert (await stranger.post(f"/chats/{cid}/messages", json=body)).status_code == 404
    too_long = {"client_msg_id": str(uuid.uuid4()), "content": "x" * 8001}
    assert (await owner.post(f"/chats/{cid}/messages", json=too_long)).status_code == 422
    blank = {"client_msg_id": str(uuid.uuid4()), "content": "   "}
    r = await owner.post(f"/chats/{cid}/messages", json=blank)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "empty_message"


# ------------------------------------------------------------------ happy path


async def test_single_turn_end_to_end(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    redis: Redis,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    pubsub = redis.pubsub()
    await pubsub.subscribe(f"chat:{cid}:events")
    await pubsub.get_message(timeout=1)
    sent = (await send(owner, cid, "How do I reverse a list?"))["message"]

    await runner.run_chat(uuid.UUID(cid))

    msgs = await messages(sessionmaker, cid)
    human, ai = msgs[-2], msgs[-1]
    assert human.status == "done"
    assert ai.kind == "ai"
    assert ai.status == "done"
    assert "**Alice**, you said:" in ai.content
    assert "How do I reverse a list?" in ai.content
    [turn] = await turns(sessionmaker, cid)
    assert turn.status == "completed"
    assert turn.ai_message_id == ai.id
    assert human.turn_id == turn.id

    types = [t for _, t, _ in await events(sessionmaker, cid)]
    assert types[-3:] == ["message.created", "turn.started", "turn.completed"]
    started = (await events(sessionmaker, cid))[-2][2]
    assert started["message_ids"] == [sent["id"]]
    assert started["author_ids"] == [owner.id]
    completed = (await events(sessionmaker, cid))[-1]
    assert completed[0] == ai.seq
    assert completed[2]["message"]["content"] == ai.content

    # live: deltas reassemble to the final text, idx strictly increasing from 0
    deltas: list[dict[str, Any]] = []
    while m := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2):
        ev = json.loads(m["data"])
        if ev["type"] == "turn.delta":
            deltas.append(ev["payload"])
    await pubsub.aclose()  # type: ignore[no-untyped-call]
    assert [d["idx"] for d in deltas] == list(range(len(deltas)))
    assert "".join(d["text"] for d in deltas) == ai.content

    # the provider saw a speaker-tagged prompt
    req = providers.fake.calls[-1]
    assert req.model == "fake-small"
    assert req.messages[-1].content.startswith('<message from="Alice" role="owner"')

    async with sessionmaker() as s:
        usage = (await s.scalars(select(UsageEvent))).all()
        assert len(usage) == 1
        assert usage[0].tokens_out > 0
    assert await redis.exists(buffer_key(turn.id)) == 0
    assert await redis.exists(lock_key(cid)) == 0


async def test_simultaneous_sends_from_three_people_form_one_batch(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    alice, bob, carol = await login("Alice"), await login("Bob"), await login("Carol")
    cid = await ready_chat(alice, validator)
    await add_member(sessionmaker, cid, bob, "editor")
    await add_member(sessionmaker, cid, carol, "editor")
    await asyncio.gather(
        send(alice, cid, "Reverse a list?"),
        send(bob, cid, "Explain Big-O"),
        send(carol, cid, "Best pizza topping?"),
    )
    await runner.run_chat(uuid.UUID(cid))

    [turn] = await turns(sessionmaker, cid)
    humans = [m for m in await messages(sessionmaker, cid) if m.kind == "human"]
    assert len(humans) == 3
    assert all(m.status == "done" and m.turn_id == turn.id for m in humans)
    async with sessionmaker() as s:
        batch = (
            await s.scalars(select(TurnMessage.message_id).where(TurnMessage.turn_id == turn.id))
        ).all()
    assert set(batch) == {m.id for m in humans}
    prompt = providers.fake.calls[-1].messages[-1].content
    assert prompt.count("<message from=") == 3
    ai = (await messages(sessionmaker, cid))[-1].content
    for name in ("Alice", "Bob", "Carol"):
        assert f"**{name}**" in ai


async def test_message_during_running_turn_is_answered_next_exactly_once(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(chunks=["slow ", "reply"], delay_s=0.3)
    await send(owner, cid, "first")
    task = asyncio.create_task(runner.run_chat(uuid.UUID(cid)))
    for _ in range(50):  # wait until the first turn has claimed its batch
        if await turns(sessionmaker, cid):
            break
        await asyncio.sleep(0.02)
    second = (await send(owner, cid, "second"))["message"]
    assert second["status"] == "pending"  # queued for next turn
    await task
    # the finishing turn saw pending work and re-woke the chat immediately
    assert await redis.zscore(DUE_KEY, cid) is not None
    await runner.run_chat(uuid.UUID(cid))

    t1, t2 = await turns(sessionmaker, cid)
    msgs = {
        m.content.removeprefix("@AI "): m
        for m in await messages(sessionmaker, cid)
        if m.kind == "human"
    }
    assert msgs["first"].turn_id == t1.id
    assert msgs["second"].turn_id == t2.id
    assert all(m.status == "done" for m in msgs.values())
    assert [t.status for t in (t1, t2)] == ["completed", "completed"]


# ------------------------------------------------------------------ not runnable / locks


async def test_no_model_means_no_turn_until_owner_picks_one(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    validator.valid.add("demo-key-1234")
    cid = (await owner.post("/chats", json={})).json()["id"]
    await owner.put(f"/chats/{cid}/key", json={"provider": "fake", "api_key": "demo-key-1234"})
    await send(owner, cid, "anyone there?")
    await runner.run_chat(uuid.UUID(cid))
    assert await turns(sessionmaker, cid) == []
    assert (await messages(sessionmaker, cid))[-1].status == "pending"
    await redis.delete(DUE_KEY)
    await owner.patch(f"/chats/{cid}", json={"model": "fake-small"})
    assert await redis.zscore(DUE_KEY, cid) is not None  # model choice wakes the queue
    await runner.run_chat(uuid.UUID(cid))
    assert [t.status for t in await turns(sessionmaker, cid)] == ["completed"]


async def test_lock_contention_requeues_without_running(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await send(owner, cid, "hi")
    await redis.delete(DUE_KEY)
    await redis.set(lock_key(cid), "someone-else", px=5000)
    await runner.run_chat(uuid.UUID(cid))
    assert await turns(sessionmaker, cid) == []
    assert await redis.zscore(DUE_KEY, cid) is not None
    assert await redis.get(lock_key(cid)) == "someone-else"  # never stole/deleted it


async def test_db_index_rejects_second_running_turn(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
) -> None:
    from sqlalchemy.exc import IntegrityError

    owner = await login("Alice")
    cid = uuid.UUID(await ready_chat(owner, validator))
    async with sessionmaker() as s:
        s.add(Turn(chat_id=cid, status="running", lock_token=uuid.uuid4()))
        await s.commit()
        s.add(Turn(chat_id=cid, status="running", lock_token=uuid.uuid4()))
        with pytest.raises(IntegrityError):
            await s.commit()


# ------------------------------------------------------------------ failures


async def test_auth_error_requeues_and_pauses_until_key_fixed(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(valid_keys={"the-new-key-0000"})  # stored key now revoked
    await send(owner, cid, "please answer")
    await runner.run_chat(uuid.UUID(cid))

    [t] = await turns(sessionmaker, cid)
    assert (t.status, t.error_code) == ("failed", "auth")
    human = (await messages(sessionmaker, cid))[-1]
    assert (human.status, human.turn_id) == ("pending", None)
    async with sessionmaker() as s:
        chat = await s.get(Chat, uuid.UUID(cid))
        key = await s.get(ChatKey, uuid.UUID(cid))
        assert chat is not None and chat.status == "paused_key"
        assert key is not None and key.status == "invalid"
    types = [x[1] for x in await events(sessionmaker, cid)]
    assert types[-2:] == ["turn.failed", "key.status"]
    failed = (await events(sessionmaker, cid))[-2][2]
    assert failed["requeued"] is True
    assert "owner" in failed["error_message"]

    await redis.delete(DUE_KEY)
    await runner.run_chat(uuid.UUID(cid))  # paused: nothing happens
    assert len(await turns(sessionmaker, cid)) == 1

    validator.valid.add("the-new-key-0000")
    r = await owner.put(
        f"/chats/{cid}/key", json={"provider": "fake", "api_key": "the-new-key-0000"}
    )
    assert r.status_code == 200
    assert await redis.zscore(DUE_KEY, cid) is not None
    await runner.run_chat(uuid.UUID(cid))
    _t1, t2 = await turns(sessionmaker, cid)
    assert t2.status == "completed"
    assert (await messages(sessionmaker, cid))[-2].turn_id == t2.id  # same message, answered once


async def test_failure_after_partial_output_keeps_partial_text(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(
        chunks=["Half ", "an ", "answer"],
        fail_at_chunk=2,
        fail_with=ProviderUnavailable("503 upstream"),
    )
    await send(owner, cid, "q")
    await runner.run_chat(uuid.UUID(cid))
    [t] = await turns(sessionmaker, cid)
    assert (t.status, t.error_code, t.partial_text) == ("failed", "unavailable", "Half an ")
    human = (await messages(sessionmaker, cid))[-1]
    assert human.status == "batched"  # not auto-retried; Retry arrives with milestone 5
    payload = (await events(sessionmaker, cid))[-1][2]
    assert payload["partial_text"] == "Half an "
    assert payload["requeued"] is False
    async with sessionmaker() as s:
        chat = await s.get(Chat, uuid.UUID(cid))
        assert chat is not None and chat.status == "active"


async def test_provider_error_echoing_key_is_redacted_in_db(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner = await login("Alice")
    odd_key = "my-odd-key-" + uuid.uuid4().hex
    cid = await ready_chat(owner, validator, key=odd_key)
    providers.fake.script = FakeScript(
        fail_at_chunk=0, fail_with=ProviderUnavailable(f"GET https://p.test/?k={odd_key} failed")
    )
    await send(owner, cid, "q")
    await runner.run_chat(uuid.UUID(cid))
    [t] = await turns(sessionmaker, cid)
    assert t.error_detail is not None
    assert odd_key not in t.error_detail
    for _, _, payload in await events(sessionmaker, cid):
        assert odd_key not in json.dumps(payload)


# ------------------------------------------------------------------ recovery / fencing


async def test_orphaned_running_turn_is_recovered_and_rerun_once(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
) -> None:
    owner = await login("Alice")
    cid = uuid.UUID(await ready_chat(owner, validator))
    msg_id = uuid.UUID((await send(owner, str(cid), "lost in a crash"))["message"]["id"])
    # simulate a worker that claimed the batch and then died (its lock expired)
    async with sessionmaker() as s:
        dead = Turn(chat_id=cid, status="running", lock_token=uuid.uuid4())
        s.add(dead)
        await s.flush()
        s.add(TurnMessage(turn_id=dead.id, message_id=msg_id))
        await s.execute(
            update(Message).where(Message.id == msg_id).values(status="batched", turn_id=dead.id)
        )
        await s.commit()
        dead_id = dead.id

    await runner.run_chat(cid)

    ts = await turns(sessionmaker, str(cid))
    by_id = {t.id: t for t in ts}
    assert (by_id[dead_id].status, by_id[dead_id].error_code) == ("failed", "crash")
    new = next(t for t in ts if t.id != dead_id)
    assert new.status == "completed"
    human = next(m for m in await messages(sessionmaker, str(cid)) if m.id == msg_id)
    assert (human.status, human.turn_id) == ("done", new.id)
    ais = [m for m in await messages(sessionmaker, str(cid)) if m.kind == "ai"]
    assert len(ais) == 1  # answered exactly once


async def test_fenced_out_worker_cannot_write(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(chunks=["a", "b", "c"], delay_s=0.2)
    await send(owner, cid, "q")
    task = asyncio.create_task(runner.run_chat(uuid.UUID(cid)))
    for _ in range(50):
        if await turns(sessionmaker, cid):
            break
        await asyncio.sleep(0.02)
    # another worker took over (new fencing token) while this one was streaming
    async with sessionmaker() as s:
        await s.execute(
            update(Turn).where(Turn.chat_id == uuid.UUID(cid)).values(lock_token=uuid.uuid4())
        )
        await s.commit()
    await task
    [t] = await turns(sessionmaker, cid)
    assert t.status == "running"  # untouched by the zombie
    assert [m for m in await messages(sessionmaker, cid) if m.kind == "ai"] == []


# ------------------------------------------------------------------ scheduler


async def test_worker_tick_runs_due_chats(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    redis: Redis,
    settings: Settings,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await send(owner, cid, "hi")
    await redis.zadd(DUE_KEY, {cid: 0})  # skip the debounce
    worker = Worker(settings, redis, runner)
    await worker.tick()
    assert worker.in_flight == 1
    await worker.drain(5)
    assert [t.status for t in await turns(sessionmaker, cid)] == ["completed"]
    assert await redis.zscore(DUE_KEY, cid) is None


async def test_debounce_holds_wakeup_until_due(
    login: LoginFn,
    validator: StubValidator,
    runner: TurnRunner,
    redis: Redis,
    settings: Settings,
    sessionmaker: SM,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await redis.delete(DUE_KEY)
    await send(owner, cid, "one")
    first_score = await redis.zscore(DUE_KEY, cid)
    await asyncio.sleep(0.05)
    await send(owner, cid, "two")  # inside the window: due time must NOT move later
    assert await redis.zscore(DUE_KEY, cid) == first_score
    worker = Worker(settings, redis, runner)
    await worker.tick()
    assert worker.in_flight == 0  # not due yet (800 ms debounce)
    await asyncio.sleep(settings.debounce_ms / 1000 + 0.1)
    await worker.tick()
    await worker.drain(5)
    [t] = await turns(sessionmaker, cid)
    humans = [m for m in await messages(sessionmaker, cid) if m.kind == "human"]
    assert all(m.turn_id == t.id for m in humans)  # both landed in one batch
