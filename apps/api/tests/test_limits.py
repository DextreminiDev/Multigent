"""Milestone 6: model limits, budgeting + rolling summaries, 429/503 backoff, RPM
ceiling, message rate limit, daily/member caps, usage attribution + panel, context
meter endpoints."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
import time
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.turns.runner as runner_mod
from app.db.models import Chat, ChatMember, ChatSummary, Turn, UsageEvent
from app.providers.base import ModelInfo
from app.providers.errors import ContextTooLong, ProviderUnavailable
from app.providers.fake import FakeScript
from app.providers.registry import ProviderRegistry
from app.security.keyvault import EncryptedKey
from app.turns.context import SUMMARY_SYSTEM, measure, split_for_compaction
from app.turns.limits import effective_max_output, effective_rpm, resolve_limits
from app.turns.prompt import ChatContext, HistoryItem, Participant
from app.turns.runner import Claim, TurnRunner, rpm_key
from app.turns.schedule import DUE_KEY
from app.turns.sweeper import Sweeper
from app.usage.service import estimate_cost, split_tokens
from tests.conftest import LoginFn, StubValidator, UserClient, ready_chat

SM = async_sessionmaker[AsyncSession]
T0 = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)


# ------------------------------------------------------------------ pure units


def test_resolve_limits_prefers_provider_list() -> None:
    listed = ModelInfo(id="m", context_window=1234, max_output_tokens=99)
    assert resolve_limits("gemini", "m", listed).__dict__ == {
        "context_window": 1234,
        "max_output": 99,
        "source": "provider",
    }
    known = resolve_limits("gemini", "gemini-2.5-flash")
    assert known.source == "litellm"
    assert known.context_window >= 1_000_000
    unknown = resolve_limits("openrouter", "nobody/never-heard-of-it")
    assert (unknown.context_window, unknown.source) == (32_000, "default")


def test_effective_limits() -> None:
    assert effective_max_output(4096, 1024) == 1024
    assert effective_max_output(512, None) == 512
    assert effective_rpm("gemini", None) == 10
    assert effective_rpm("gemini", 3) == 3
    assert effective_rpm("anthropic", None) is None


def test_budget_math() -> None:
    b = measure(window=1000, max_output=100, system_tokens=200, history_tokens=300)
    assert (b.budget, b.available, b.needed, b.fits) == (700, 400, 330, True)
    assert not measure(1000, 100, 200, 400).fits  # 440 > 400


def _turn(start: int, who: uuid.UUID, q: str, a: str) -> list[HistoryItem]:
    return [
        HistoryItem(
            seq=start - 1,
            kind="human",
            content=q,
            created_at=T0,
            author_id=who,
            author_name="Al",
            anchor=start,
        ),
        HistoryItem(seq=start + 1, kind="ai", content=a, created_at=T0, anchor=start),
    ]


def test_compaction_split_keeps_whole_turns_and_the_batch() -> None:
    al = uuid.uuid4()
    people = [Participant(al, "Al", "owner")]
    hist = _turn(10, al, "old q " * 50, "old a " * 50) + _turn(20, al, "mid q", "mid a")
    batch = [
        HistoryItem(
            seq=29,
            kind="human",
            content="new " * 400,
            created_at=T0,
            author_id=al,
            author_name="Al",
            anchor=30,
        )
    ]

    def count(msgs: Any, system: str) -> int:
        return sum(len(m.content) for m in msgs) // 4

    fold, keep = split_for_compaction(
        hist + batch, people, keep_tokens=60, count=count, protect_from=30
    )
    assert [h.content for h in fold] == ["old q " * 50, "old a " * 50]  # q+a fold together
    assert keep[-1] is batch[0]  # the batch is never folded, even though it's huge
    fold2, keep2 = split_for_compaction(
        hist + batch, people, keep_tokens=0, count=count, protect_from=30
    )
    assert keep2 == batch
    assert len(fold2) == 4


def test_usage_split_and_cost() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    assert split_tokens(10, [a, b]) == {a: 5, b: 5}
    assert split_tokens(11, [a, b]) == {a: 6, b: 5}
    assert split_tokens(7, []) == {None: 7}
    assert estimate_cost("fake", "m", 100, 100) is None
    cost = estimate_cost("openai", "gpt-4o", 1_000_000, 0)
    assert cost is not None and cost > 0


# ------------------------------------------------------------------ helpers


async def send(u: UserClient, cid: str, text: str) -> Any:
    return await u.post(
        f"/chats/{cid}/messages", json={"client_msg_id": str(uuid.uuid4()), "content": text}
    )


async def set_chat(sm: SM, cid: str, **values: Any) -> None:
    async with sm() as s:
        await s.execute(update(Chat).where(Chat.id == uuid.UUID(cid)).values(**values))
        await s.commit()


async def turns(sm: SM, cid: str) -> list[Turn]:
    async with sm() as s:
        return list(
            (
                await s.scalars(
                    select(Turn).where(Turn.chat_id == uuid.UUID(cid)).order_by(Turn.started_at)
                )
            ).all()
        )


async def status_frames(redis: Redis, cid: str) -> Any:
    ps = redis.pubsub()
    await ps.subscribe(f"chat:{cid}:events")
    await ps.get_message(timeout=1)
    return ps


async def drain(ps: Any) -> list[dict[str, Any]]:
    out = []
    while m := await ps.get_message(ignore_subscribe_messages=True, timeout=0.2):
        out.append(json.loads(m["data"]))
    await ps.aclose()
    return out


# ------------------------------------------------------------------ model limits


@pytest.mark.integration
async def test_choosing_a_model_records_its_limits_and_clamps_output(
    login: LoginFn, validator: StubValidator, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)  # model "fake-small": not in the list
    chat = (await owner.get(f"/chats/{cid}")).json()
    assert (chat["context_window"], chat["limits_source"]) == (32_000, "default")
    chat = (await owner.patch(f"/chats/{cid}", json={"model": "model-a"})).json()
    assert (chat["context_window"], chat["limits_source"]) == (1000, "provider")

    async def models(*a: Any, **k: Any) -> list[ModelInfo]:
        return [ModelInfo(id="tiny", context_window=4000, max_output_tokens=256)]

    monkeypatch.setattr(validator, "list_models", models)
    chat = (await owner.patch(f"/chats/{cid}", json={"model": "tiny"})).json()
    assert (chat["context_window"], chat["max_output_limit"]) == (4000, 256)
    assert chat["max_output_tokens"] == 256  # was 2048, clamped to what the model allows


# ------------------------------------------------------------------ compaction


@pytest.mark.integration
async def test_long_chat_is_compacted_into_a_rolling_summary(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await set_chat(sessionmaker, cid, context_window=900, max_output_tokens=64)
    for i in range(8):
        r = await send(owner, cid, f"Question {i}: " + "lorem ipsum dolor sit amet " * 12)
        assert r.status_code == 201
        await runner.run_chat(uuid.UUID(cid))
    async with sessionmaker() as s:
        summaries = (
            await s.scalars(
                select(ChatSummary)
                .where(ChatSummary.chat_id == uuid.UUID(cid))
                .order_by(ChatSummary.created_at)
            )
        ).all()
        usage_kinds = set(
            (
                await s.scalars(select(UsageEvent.kind).where(UsageEvent.chat_id == uuid.UUID(cid)))
            ).all()
        )
    assert summaries, "history outgrew the window, a summary should exist"
    assert summaries[-1].covers_through_seq > summaries[0].covers_through_seq or len(summaries) == 1
    assert usage_kinds == {"turn", "summary"}
    assert all(t.status == "completed" for t in await turns(sessionmaker, cid))

    calls = providers.fake.calls
    assert any(c.system == SUMMARY_SYSTEM for c in calls)
    last = [c for c in calls if c.system != SUMMARY_SYSTEM][-1]
    assert "Summary of the earlier part" in last.system
    joined = " ".join(m.content for m in last.messages)
    assert "Question 0:" not in joined  # folded away
    assert "Question 7:" in joined  # the batch is always there

    raw = await redis.get(f"chat:{cid}:context")
    assert raw is not None
    ctx = json.loads(raw)
    assert ctx["window"] == 900
    assert 0 < ctx["percent"] <= 120
    got = (await owner.get(f"/chats/{cid}/context")).json()
    assert got["summary"]["covers_through_seq"] == summaries[-1].covers_through_seq
    assert got["summary"]["content"]  # owner sees the text


@pytest.mark.integration
async def test_context_too_long_compacts_harder_and_retries_once(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    for i in range(3):
        await send(owner, cid, f"earlier {i}")
        await runner.run_chat(uuid.UUID(cid))
    providers.fake.calls.clear()
    providers.fake.script = FakeScript(fail_first_n=1, fail_first_with=ContextTooLong("too long"))
    await send(owner, cid, "now")
    await runner.run_chat(uuid.UUID(cid))
    assert (await turns(sessionmaker, cid))[-1].status == "completed"
    systems = [c.system for c in providers.fake.calls]
    assert systems.count(SUMMARY_SYSTEM) == 1  # one forced compaction between the two tries


@pytest.mark.integration
async def test_compact_now_and_clear_summary(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    redis: Redis,
) -> None:
    owner, ed = await login("Alice"), await login("Ed")
    cid = await ready_chat(owner, validator)
    async with sessionmaker() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(ed.id), role="editor"))
        await s.commit()
    for i in range(3):
        await send(owner, cid, f"msg {i}")
        await runner.run_chat(uuid.UUID(cid))
    assert (await ed.post(f"/chats/{cid}/context/compact")).status_code == 403
    assert (await owner.post(f"/chats/{cid}/context/compact")).status_code == 202
    assert (await owner.get(f"/chats/{cid}/context")).json()["compact_requested"] is True
    await send(owner, cid, "next")
    await runner.run_chat(uuid.UUID(cid))
    view = (await ed.get(f"/chats/{cid}/context")).json()
    assert view["summary"] is not None
    assert view["summary"]["content"] is None  # collaborators don't see the summary text
    assert view["compact_requested"] is False
    assert (await owner.delete(f"/chats/{cid}/context/summary")).status_code == 204
    assert (await owner.get(f"/chats/{cid}/context")).json()["summary"] is None


# ------------------------------------------------------------------ backoff / RPM


@pytest.fixture
def no_jitter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(random, "uniform", lambda a, b: 0.0)


@pytest.mark.integration
async def test_rate_limited_then_succeeds_with_live_status(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
    redis: Redis,
    no_jitter: None,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(rate_limit_first_n=2, retry_after=0.1)
    ps = await status_frames(redis, cid)
    await send(owner, cid, "hello")
    await runner.run_chat(uuid.UUID(cid))
    frames = await drain(ps)
    statuses = [f["payload"]["status"] for f in frames if f["type"] == "turn.status"]
    assert statuses == ["rate_limited", "retrying", "rate_limited", "retrying"]
    first = next(f for f in frames if f["type"] == "turn.status")
    assert first["payload"]["retry_in"] == 0.1
    assert first["payload"]["attempt"] == 1
    assert (await turns(sessionmaker, cid))[-1].status == "completed"


@pytest.mark.integration
async def test_gives_up_after_max_retries(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
    no_jitter: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner_mod, "MAX_BACKOFF_S", 0.05)
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(
        fail_first_n=99, fail_first_with=ProviderUnavailable("high demand")
    )
    await send(owner, cid, "hello")
    await runner.run_chat(uuid.UUID(cid))
    [t] = await turns(sessionmaker, cid)
    assert (t.status, t.error_code) == ("failed", "unavailable")
    assert len(providers.fake.calls) == 1 + runner_mod.MAX_PROVIDER_RETRIES


@pytest.mark.integration
async def test_stop_during_backoff(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    providers: ProviderRegistry,
    no_jitter: None,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    providers.fake.script = FakeScript(rate_limit_first_n=5, retry_after=30)
    await send(owner, cid, "hello")
    task = asyncio.create_task(runner.run_chat(uuid.UUID(cid)))
    for _ in range(100):
        if providers.fake.calls:
            break
        await asyncio.sleep(0.02)
    [t] = await turns(sessionmaker, cid)
    await owner.post(f"/chats/{cid}/turns/{t.id}/cancel")
    await asyncio.wait_for(task, 3)  # didn't sit out the 30 s backoff
    assert (await turns(sessionmaker, cid))[0].status == "cancelled"


@pytest.mark.integration
async def test_rpm_ceiling_waits_for_a_free_slot(redis: Redis, app: FastAPI) -> None:
    r = TurnRunner(app.state.sessionmaker, redis, app.state.vault)
    claim = Claim(
        turn_id=uuid.uuid4(),
        chat_id=uuid.uuid4(),
        token=uuid.uuid4(),
        ctx=ChatContext(title="t", provider="fake", model="m", max_output_tokens=10),
        participants=[],
        history=[],
        key=EncryptedKey(b"", b"", "k"),
        key_provider="fake",
        message_ids=[],
        author_ids=[],
        started_seq=1,
        rpm=2,
    )
    now = time.time()
    await redis.zadd(rpm_key(claim.chat_id), {"a": now - 59.6, "b": now - 30})
    t0 = time.monotonic()
    assert await r._acquire_rpm(claim, None) is False
    waited = time.monotonic() - t0
    assert 0.3 < waited < 2.0  # waited for the oldest call to age out
    assert await redis.zcard(rpm_key(claim.chat_id)) == 2


# ------------------------------------------------------------------ rate limit / caps / usage


@pytest.mark.integration
async def test_message_rate_limit(login: LoginFn, validator: StubValidator, app: FastAPI) -> None:
    app.state.settings = app.state.settings.model_copy(update={"message_rate_per_min": 3})
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    codes = [(await send(owner, cid, f"m{i}")).status_code for i in range(4)]
    assert codes == [201, 201, 201, 429]
    r = await send(owner, cid, "again")
    assert r.json()["error"]["code"] == "rate_limited"
    assert "Try again in" in r.json()["error"]["message"]


@pytest.mark.integration
async def test_daily_cap_pauses_and_owner_can_raise_it(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await owner.patch(f"/chats/{cid}", json={"daily_token_cap": 40})
    await send(owner, cid, "hi")
    await runner.run_chat(uuid.UUID(cid))  # runs, and pushes usage past the cap
    chat = (await owner.get(f"/chats/{cid}")).json()
    assert chat["status"] == "paused_cap"

    await send(owner, cid, "are you there?")
    await redis.delete(DUE_KEY)
    await runner.run_chat(uuid.UUID(cid))
    assert len(await turns(sessionmaker, cid)) == 1  # paused: not answered
    sw = Sweeper(sessionmaker, redis, "n1", interval_s=0.01)
    await sw.sweep()
    assert (await owner.get(f"/chats/{cid}")).json()["status"] == "paused_cap"  # still over

    await owner.patch(f"/chats/{cid}", json={"daily_token_cap": 1_000_000})
    assert (await owner.get(f"/chats/{cid}")).json()["status"] == "active"
    await runner.run_chat(uuid.UUID(cid))
    assert [t.status for t in await turns(sessionmaker, cid)] == ["completed", "completed"]


@pytest.mark.integration
async def test_cap_resets_at_midnight_via_sweeper(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await owner.patch(f"/chats/{cid}", json={"daily_token_cap": 40})
    await send(owner, cid, "hi")
    await runner.run_chat(uuid.UUID(cid))
    assert (await owner.get(f"/chats/{cid}")).json()["status"] == "paused_cap"
    # "a new day": yesterday's usage no longer counts
    async with sessionmaker() as s:
        await s.execute(update(UsageEvent).values(day=dt.date(2020, 1, 1)))
        await s.commit()
    async for k in redis.scan_iter("usage:*"):
        await redis.delete(k)
    await Sweeper(sessionmaker, redis, "n1", interval_s=0.01).sweep()
    assert (await owner.get(f"/chats/{cid}")).json()["status"] == "active"


@pytest.mark.integration
async def test_member_cap_and_usage_attribution(
    login: LoginFn,
    validator: StubValidator,
    sessionmaker: SM,
    runner: TurnRunner,
) -> None:
    owner, bob = await login("Alice"), await login("Bob")
    cid = await ready_chat(owner, validator)
    async with sessionmaker() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(bob.id), role="editor"))
        await s.commit()
    await asyncio.gather(send(owner, cid, "from alice"), send(bob, cid, "from bob"))
    await runner.run_chat(uuid.UUID(cid))
    async with sessionmaker() as s:
        rows = (
            await s.scalars(
                select(UsageEvent).where(
                    UsageEvent.chat_id == uuid.UUID(cid), UsageEvent.kind == "turn"
                )
            )
        ).all()
    assert {str(r.user_id) for r in rows} == {owner.id, bob.id}  # split across the batch
    assert abs(rows[0].tokens_out - rows[1].tokens_out) <= 1

    usage = (await owner.get(f"/chats/{cid}/usage")).json()
    names = {m["name"]: m for m in usage["members"]}
    assert names["Bob"]["tokens_today"] > 0
    assert usage["used_today"] == sum(m["tokens_today"] for m in usage["members"])
    assert len(usage["days"]) == 7
    assert usage["pricing_known"] is False  # demo provider has no price
    assert (await bob.get(f"/chats/{cid}/usage")).status_code == 403

    r = await owner.patch(f"/chats/{cid}/members/{bob.id}", json={"member_token_cap": 1})
    assert r.json()["member_token_cap"] == 1
    members = (await bob.get(f"/chats/{cid}/members")).json()
    assert all(m["member_token_cap"] is None for m in members)  # private to the owner
    blocked = await send(bob, cid, "more please")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "member_cap"
    assert (await send(owner, cid, "owner unaffected")).status_code == 201
    r = await owner.patch(f"/chats/{cid}/members/{bob.id}", json={"member_token_cap": None})
    assert r.json()["member_token_cap"] is None
    assert (await send(bob, cid, "thanks")).status_code == 201


@pytest.mark.integration
async def test_usage_counters_rebuild_from_db(
    login: LoginFn,
    validator: StubValidator,
    runner: TurnRunner,
    redis: Redis,
) -> None:
    owner = await login("Alice")
    cid = await ready_chat(owner, validator)
    await send(owner, cid, "hi")
    await runner.run_chat(uuid.UUID(cid))
    before = (await owner.get(f"/chats/{cid}/usage")).json()["used_today"]
    async for k in redis.scan_iter("usage:*"):
        await redis.delete(k)
    assert (await owner.get(f"/chats/{cid}/usage")).json()["used_today"] == before > 0


def test_summary_budget_scales_with_available_space() -> None:
    from app.turns.context import summary_budget

    assert summary_budget(100_000) == 1024
    assert summary_budget(2_000) == 500
    assert summary_budget(100) == 128
