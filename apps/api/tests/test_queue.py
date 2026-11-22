"""Milestone 5: stop / retry / regenerate / delete-pending, sweeper recovery
(worker crash, lost wake-ups, Redis flush), and a multi-worker stress test that
checks the core invariants (I1 total order, I2 no loss/no dupes, I3 one turn)."""

from __future__ import annotations

import asyncio
import random
import uuid
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Chat, ChatEvent, ChatMember, Message, Turn, TurnMessage
from app.providers.fake import FakeProvider, FakeScript
from app.providers.registry import ProviderRegistry
from app.turns.lock import ChatLock, lock_key
from app.turns.runner import TurnRunner
from app.turns.schedule import DUE_KEY
from app.turns.sweeper import LEADER_KEY, Sweeper
from app.worker import Worker
from tests.conftest import LoginFn, StubValidator, UserClient, ask_ai, ready_chat

pytestmark = pytest.mark.integration
SM = async_sessionmaker[AsyncSession]


async def send(u: UserClient, cid: str, text: str) -> dict[str, Any]:
    r = await u.post(
        f"/chats/{cid}/messages", json={"client_msg_id": str(uuid.uuid4()), **ask_ai(text)}
    )
    assert r.status_code == 201, r.text
    return r.json()["message"]  # type: ignore[no-any-return]


async def turns(sm: SM, cid: str) -> list[Turn]:
    async with sm() as s:
        return list(
            (
                await s.scalars(
                    select(Turn).where(Turn.chat_id == uuid.UUID(cid)).order_by(Turn.started_at)
                )
            ).all()
        )


async def msgs(sm: SM, cid: str, kind: str | None = None) -> list[Message]:
    async with sm() as s:
        q = select(Message).where(Message.chat_id == uuid.UUID(cid))
        if kind:
            q = q.where(Message.kind == kind)
        return list((await s.scalars(q.order_by(Message.seq))).all())


async def wait_for(pred: Any, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not await pred():  # noqa: ASYNC110 (polling DB state, not an event)
            await asyncio.sleep(0.02)


async def add_member(sm: SM, cid: str, u: UserClient, role: str) -> None:
    async with sm() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(u.id), role=role))
        await s.commit()


# ------------------------------------------------------------------ stop


async def test_stop_mid_stream_keeps_partial_and_retry_answers_once(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(chunks=[f"w{i} " for i in range(40)], delay_s=0.05)
    await send(owner, cid, "long answer please")
    task = asyncio.create_task(runner.run_chat(uuid.UUID(cid)))
    await wait_for(lambda: _has_text(redis, sessionmaker, cid))
    [t] = await turns(sessionmaker, cid)
    r = await owner.post(f"/chats/{cid}/turns/{t.id}/cancel")
    assert r.json() == {"turn_id": str(t.id), "status": "cancelling"}
    await asyncio.wait_for(task, 5)

    [t] = await turns(sessionmaker, cid)
    assert t.status == "cancelled"
    assert t.partial_text and t.partial_text.startswith("w0 ")
    assert len(t.partial_text.split()) < 40
    [human] = await msgs(sessionmaker, cid, "human")
    assert human.status == "done"
    assert await msgs(sessionmaker, cid, "ai") == []  # no AI message for a stopped reply
    assert await redis.exists(f"turn:{t.id}:cancel") == 0

    # Stop pressed again after it ended: idempotent, reports the final state
    again = await owner.post(f"/chats/{cid}/turns/{t.id}/cancel")
    assert again.json()["status"] == "cancelled"

    # Retry re-runs the same batch as trigger=retry, exactly once
    providers.fake.script = FakeScript(chunks=["full ", "answer"])
    r = await owner.post(f"/chats/{cid}/turns/{t.id}/retry")
    assert r.json()["status"] == "retrying"
    assert (await owner.post(f"/chats/{cid}/turns/{t.id}/retry")).status_code == 409
    await runner.run_chat(uuid.UUID(cid))
    _t1, t2 = await turns(sessionmaker, cid)
    assert (t2.status, t2.trigger) == ("completed", "retry")
    [human] = await msgs(sessionmaker, cid, "human")
    assert (human.status, human.turn_id) == ("done", t2.id)
    assert [m.content for m in await msgs(sessionmaker, cid, "ai")] == ["full answer"]


async def _has_text(redis: Redis, sm: SM, cid: str) -> bool:
    ts = await turns(sm, cid)
    return bool(ts) and await redis.llen(f"turn:{ts[0].id}:buf") >= 2


async def test_stop_while_provider_is_silent(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(chunks=["x"], hang=True, hang_after=0)
    await send(owner, cid, "q")
    task = asyncio.create_task(runner.run_chat(uuid.UUID(cid)))
    await wait_for(lambda: _turn_exists(sessionmaker, cid))
    [t] = await turns(sessionmaker, cid)
    await owner.post(f"/chats/{cid}/turns/{t.id}/cancel")
    await asyncio.wait_for(task, 3)  # not stuck waiting on the provider
    assert (await turns(sessionmaker, cid))[0].status == "cancelled"


async def _turn_exists(sm: SM, cid: str) -> bool:
    return bool(await turns(sm, cid))


async def test_cancel_permissions(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner, ed, other, vi = (
        await login("Alice"),
        await login("Ed"),
        await login("Otto"),
        await login("Vi"),
    )
    cid = await ready_chat(owner, validator)
    for u, role in ((ed, "editor"), (other, "editor"), (vi, "viewer")):
        await add_member(sessionmaker, cid, u, role)
    providers.fake.script = FakeScript(chunks=["x"], hang=True, hang_after=0)
    await send(ed, cid, "ed's question")
    task = asyncio.create_task(runner.run_chat(uuid.UUID(cid)))
    await wait_for(lambda: _turn_exists(sessionmaker, cid))
    [t] = await turns(sessionmaker, cid)
    assert (await other.post(f"/chats/{cid}/turns/{t.id}/cancel")).status_code == 403
    assert (await vi.post(f"/chats/{cid}/turns/{t.id}/cancel")).status_code == 403
    assert (await ed.post(f"/chats/{cid}/turns/{t.id}/cancel")).json()["status"] == "cancelling"
    await asyncio.wait_for(task, 3)
    assert (await other.post(f"/chats/{cid}/turns/{t.id}/retry")).status_code == 403
    assert (await ed.post(f"/chats/{cid}/turns/{t.id}/retry")).status_code == 200


async def test_deleting_the_key_stops_the_running_reply(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(chunks=["x"], hang=True, hang_after=0)
    await send(owner, cid, "q")
    task = asyncio.create_task(runner.run_chat(uuid.UUID(cid)))
    await wait_for(lambda: _turn_exists(sessionmaker, cid))
    assert (await owner.delete(f"/chats/{cid}/key")).status_code == 204
    await asyncio.wait_for(task, 3)
    assert (await turns(sessionmaker, cid))[0].status == "cancelled"


# ------------------------------------------------------------------ regenerate / delete


async def test_regenerate_last_reply(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner, ed = await login("Alice"), await login("Ed")
    cid = await ready_chat(owner, validator)
    await add_member(sessionmaker, cid, ed, "editor")
    providers.fake.script = FakeScript(chunks=["first take"])
    await send(owner, cid, "q1")
    await runner.run_chat(uuid.UUID(cid))
    [old] = await msgs(sessionmaker, cid, "ai")

    assert (await ed.post(f"/chats/{cid}/messages/{old.id}/regenerate")).status_code == 403
    providers.fake.script = FakeScript(chunks=["second take"])
    r = await owner.post(f"/chats/{cid}/messages/{old.id}/regenerate")
    assert r.status_code == 200
    await runner.run_chat(uuid.UUID(cid))

    ais = await msgs(sessionmaker, cid, "ai")
    assert [(m.content, m.status) for m in ais] == [
        ("first take", "superseded"),
        ("second take", "done"),
    ]
    _t1, t2 = await turns(sessionmaker, cid)
    assert t2.trigger == "regenerate"
    # the model did not see the superseded answer
    prompt = providers.fake.calls[-1]
    assert all("first take" not in m.content for m in prompt.messages)
    # only the latest reply can be regenerated
    assert (await owner.post(f"/chats/{cid}/messages/{old.id}/regenerate")).status_code == 409


async def test_delete_own_pending_message(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner, ed = await login("Alice"), await login("Ed")
    cid = await ready_chat(owner, validator)
    await add_member(sessionmaker, cid, ed, "editor")
    oops = await send(ed, cid, "oops, wrong chat")
    keep = await send(ed, cid, "real question")
    assert (await owner.delete(f"/chats/{cid}/messages/{oops['id']}")).status_code == 403
    assert (await ed.delete(f"/chats/{cid}/messages/{oops['id']}")).status_code == 204
    assert (await ed.delete(f"/chats/{cid}/messages/{oops['id']}")).status_code == 204  # idempotent
    await runner.run_chat(uuid.UUID(cid))
    prompt = providers.fake.calls[-1].messages[-1].content
    assert "wrong chat" not in prompt
    assert "real question" in prompt
    by_id = {str(m.id): m for m in await msgs(sessionmaker, cid, "human")}
    assert (by_id[oops["id"]].status, by_id[oops["id"]].content) == ("deleted", "")
    assert (await ed.delete(f"/chats/{cid}/messages/{keep['id']}")).json()["error"][
        "code"
    ] == "already_sent"


# ------------------------------------------------------------------ sweeper / recovery


async def test_sweeper_recovers_lost_wakeup(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await send(owner, cid, "anyone?")
    await redis.delete(DUE_KEY)
    sw = Sweeper(sessionmaker, redis, "node-a", interval_s=0.01)
    assert await sw.sweep() == 1
    assert await redis.zscore(DUE_KEY, cid) is not None
    await redis.set(lock_key(cid), "busy", px=5000)  # someone's on it: leave it alone
    await redis.delete(DUE_KEY)
    assert await sw.sweep() == 0


async def test_only_one_sweeper_leads(redis: Redis, sessionmaker: SM) -> None:
    a = Sweeper(sessionmaker, redis, "node-a", interval_s=1)
    b = Sweeper(sessionmaker, redis, "node-b", interval_s=1)
    assert await a.is_leader() is True
    assert await b.is_leader() is False
    assert await a.is_leader() is True  # renews
    await a.resign()
    assert await b.is_leader() is True
    assert await redis.get(LEADER_KEY) == "node-b"


async def test_worker_killed_mid_turn_is_recovered_exactly_once(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    redis: Redis,
    settings: Settings,
    app: Any,
) -> None:
    """kill -9 simulation: a worker claims the batch and dies without cleanup, so
    its lock lingers until the TTL. The sweeper on another worker recovers it."""
    owner = await login("Alice")
    cid = uuid.UUID(await ready_chat(owner, validator))
    await send(owner, str(cid), "please survive the crash")
    await redis.delete(DUE_KEY)

    doomed = TurnRunner(app.state.sessionmaker, redis, app.state.vault, lock_ttl_ms=400)
    token = uuid.uuid4()
    lock = ChatLock(redis, cid, token, ttl_ms=400)
    assert await lock.acquire()
    claim = await doomed.claim(cid, token)  # ...and the process dies here
    assert claim is not None

    healthy = TurnRunner(
        app.state.sessionmaker, redis, app.state.vault, ProviderRegistry(fake=FakeProvider())
    )
    worker = Worker(settings, redis, healthy, sweep_interval_s=0.05)
    run = asyncio.create_task(worker.run())
    try:
        await wait_for(lambda: _answered(sessionmaker, str(cid)), timeout=10)
    finally:
        worker.stop()
        await asyncio.wait_for(run, 5)

    ts = await turns(sessionmaker, str(cid))
    assert [(t.status, t.error_code) for t in ts] == [("failed", "crash"), ("completed", None)]
    assert len(await msgs(sessionmaker, str(cid), "ai")) == 1
    [human] = await msgs(sessionmaker, str(cid), "human")
    assert (human.status, human.turn_id) == ("done", ts[1].id)


async def _answered(sm: SM, cid: str) -> bool:
    return any(t.status == "completed" for t in await turns(sm, cid))


async def test_redis_flush_self_heals(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    redis: Redis,
    settings: Settings,
    app: Any,
) -> None:
    """I6: losing all of Redis (locks, due-queue) only costs latency."""
    owner = await login("Alice")
    chats = [await ready_chat(owner, validator) for _ in range(3)]
    for cid in chats:
        await send(owner, cid, f"hello {cid[:4]}")
    sessions: dict[str, dict[str, str]] = {}
    async for k in redis.scan_iter("sess:*"):
        sessions[str(k)] = {str(a): str(b) for a, b in (await redis.hgetall(k)).items()}
    await redis.flushdb()
    for k, v in sessions.items():  # keep the test's own login alive
        await redis.hset(k, mapping=v)  # type: ignore[arg-type]

    worker = Worker(
        settings,
        redis,
        TurnRunner(app.state.sessionmaker, redis, app.state.vault),
        sweep_interval_s=0.05,
    )
    run = asyncio.create_task(worker.run())
    try:
        for cid in chats:
            await wait_for(lambda c=cid: _answered(sessionmaker, c), timeout=10)
    finally:
        worker.stop()
        await asyncio.wait_for(run, 5)


# ------------------------------------------------------------------ stress


async def test_stress_many_users_many_messages_many_workers(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    redis: Redis,
    settings: Settings,
    app: Any,
) -> None:
    n_chats, n_users, per_user, n_workers = 3, 4, 8, 3
    owner = await login("Owner")
    users = [await login(f"U{i}") for i in range(n_users)]
    chats = [await ready_chat(owner, validator) for _ in range(n_chats)]
    for cid in chats:
        for u in users:
            await add_member(sessionmaker, cid, u, "editor")

    stress_settings = settings.model_copy(update={"debounce_ms": 30})
    # the API decides the debounce (when a send wakes the queue): make it short so
    # turns run *while* messages keep arriving, which is the case worth stressing
    app.state.settings = stress_settings
    workers = [
        Worker(
            stress_settings,
            redis,
            TurnRunner(
                app.state.sessionmaker,
                redis,
                app.state.vault,
                ProviderRegistry(fake=FakeProvider(FakeScript(delay_s=0.004))),
            ),
            sweep_interval_s=0.5,
        )
        for _ in range(n_workers)
    ]
    tasks = [asyncio.create_task(w.run()) for w in workers]

    violations: list[str] = []
    stop_monitor = asyncio.Event()

    async def monitor() -> None:  # I3, sampled continuously
        while not stop_monitor.is_set():
            async with sessionmaker() as s:
                rows = (
                    await s.execute(
                        select(Turn.chat_id, func.count())
                        .where(Turn.status == "running")
                        .group_by(Turn.chat_id)
                    )
                ).all()
            violations.extend(f"{c}: {n} running" for c, n in rows if n > 1)
            await asyncio.sleep(0.01)

    mon = asyncio.create_task(monitor())

    async def user_traffic(u: UserClient, cid: str) -> None:
        for i in range(per_user):
            await asyncio.sleep(random.uniform(0.02, 0.25))
            await send(u, cid, f"{u.user['name']} msg {i}")

    expected = n_chats * n_users * per_user
    try:
        await asyncio.gather(*(user_traffic(u, c) for c in chats for u in users))

        async def all_done() -> bool:
            async with sessionmaker() as s:
                n = await s.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.kind == "human", Message.status == "done")
                )
            return n == expected

        await wait_for(all_done, timeout=60)
    finally:
        stop_monitor.set()
        await mon
        for w in workers:
            w.stop()
        await asyncio.gather(*tasks)

    assert violations == []
    async with sessionmaker() as s:
        for cid in chats:
            chat_uuid = uuid.UUID(cid)
            # I1: one gapless, strictly increasing event log per chat
            seqs = list(
                (
                    await s.scalars(
                        select(ChatEvent.seq)
                        .where(ChatEvent.chat_id == chat_uuid)
                        .order_by(ChatEvent.seq)
                    )
                ).all()
            )
            last = await s.scalar(select(Chat.last_seq).where(Chat.id == chat_uuid))
            assert seqs == list(range(1, int(last or 0) + 1))
            # I2: every human message consumed by exactly one completed turn
            humans = list(
                (
                    await s.scalars(
                        select(Message).where(Message.chat_id == chat_uuid, Message.kind == "human")
                    )
                ).all()
            )
            assert len(humans) == n_users * per_user
            assert len({h.client_msg_id for h in humans}) == len(humans)
            completed = {
                t.id: t
                for t in (await s.scalars(select(Turn).where(Turn.chat_id == chat_uuid))).all()
            }
            assert all(t.status == "completed" for t in completed.values())
            for h in humans:
                n = await s.scalar(
                    select(func.count())
                    .select_from(TurnMessage)
                    .where(TurnMessage.message_id == h.id)
                )
                assert n == 1, f"message batched {n} times"
            # one AI reply per turn, and batching actually happened
            ais = list(
                (
                    await s.scalars(
                        select(Message).where(Message.chat_id == chat_uuid, Message.kind == "ai")
                    )
                ).all()
            )
            assert len(ais) == len(completed)
            assert 1 < len(completed) < len(humans)  # several batches, each >= 1 msg
            # some turns must have started while other messages were still arriving
            starts = sorted(t.started_at for t in completed.values())
            assert starts[0] < max(h.created_at for h in humans)
