"""Integration: health probes, schema invariants, worker heartbeat (real PG + Redis)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import Chat, ChatEvent, Message, Turn, User
from app.ids import uuid7
from app.main import create_app
from app.worker import HEARTBEAT_INDEX, Worker

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------- probes


async def test_healthz(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


async def test_readyz_ok(client: AsyncClient) -> None:
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "postgres": True, "redis": True}


async def test_readyz_reports_redis_down(settings: Settings) -> None:
    bad = settings.model_copy(update={"redis_url": "redis://localhost:1/0"})
    app = create_app(bad)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        r = await c.get("/readyz")
    assert r.status_code == 503
    assert r.json()["postgres"] is True
    assert r.json()["redis"] is False


async def test_cors_allows_only_configured_origins(client: AsyncClient) -> None:
    ok = await client.get("/healthz", headers={"Origin": "http://localhost:3000"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert ok.headers["access-control-allow-credentials"] == "true"
    evil = await client.get("/healthz", headers={"Origin": "https://evil.test"})
    assert "access-control-allow-origin" not in evil.headers


# --------------------------------------------------------------------- schema


async def _chat(s: AsyncSession) -> Chat:
    u = User(email=f"{uuid7().hex}@t.test", name="Owner", avatar_color="#000")
    s.add(u)
    await s.flush()
    c = Chat(owner_id=u.id, title="t")
    s.add(c)
    await s.flush()
    return c


async def test_one_running_turn_per_chat(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as s:
        chat_id = (await _chat(s)).id
        s.add(Turn(chat_id=chat_id, status="running", lock_token=uuid.uuid4()))
        await s.commit()
        s.add(Turn(chat_id=chat_id, status="running", lock_token=uuid.uuid4()))
        with pytest.raises(IntegrityError, match="one_running_turn_per_chat"):
            await s.commit()
        await s.rollback()
        # completed turns don't count against the index
        s.add(Turn(chat_id=chat_id, status="completed", lock_token=uuid.uuid4()))
        await s.commit()


async def test_client_msg_id_unique_per_chat(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    cmid = uuid.uuid4()
    async with sessionmaker() as s:
        c = await _chat(s)
        s.add(
            Message(
                chat_id=c.id, seq=1, kind="human", content="a", client_msg_id=cmid, status="pending"
            )
        )
        await s.commit()
        s.add(
            Message(
                chat_id=c.id, seq=2, kind="human", content="a", client_msg_id=cmid, status="pending"
            )
        )
        with pytest.raises(IntegrityError, match="uq_messages_chat_id_client_msg_id"):
            await s.commit()


async def test_check_constraints(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as s:
        c = await _chat(s)
        await s.commit()
        with pytest.raises(IntegrityError, match="ck_chats_status"):
            await s.execute(text("UPDATE chats SET status='bogus' WHERE id=:id"), {"id": c.id})


async def test_seq_allocation_is_gapless_under_concurrency(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Preview of §5: the row-lock UPDATE...RETURNING yields a strict total order."""
    async with sessionmaker() as s:
        chat_id = (await _chat(s)).id
        await s.commit()

    async def append(i: int) -> int:
        async with sessionmaker() as s, s.begin():
            seq = (
                await s.execute(
                    text(
                        "UPDATE chats SET last_seq = last_seq + 1 WHERE id=:id RETURNING last_seq"
                    ),
                    {"id": chat_id},
                )
            ).scalar_one()
            s.add(ChatEvent(chat_id=chat_id, seq=seq, type="test", payload={"i": i}))
            return int(seq)

    seqs = await asyncio.gather(*(append(i) for i in range(50)))
    assert sorted(seqs) == list(range(1, 51))


async def test_deleting_chat_cascades(sessionmaker: async_sessionmaker[AsyncSession]) -> None:
    async with sessionmaker() as s:
        c = await _chat(s)
        s.add(ChatEvent(chat_id=c.id, seq=1, type="x", payload={}))
        s.add(Message(chat_id=c.id, seq=1, kind="human", content="a", status="pending"))
        await s.commit()
        await s.delete(c)
        await s.commit()
        n = await s.scalar(select(func.count()).select_from(Message).where(Message.chat_id == c.id))
        assert n == 0


# --------------------------------------------------------------------- worker


async def test_worker_heartbeat_and_clean_stop(settings: Settings, redis: Redis) -> None:
    w = Worker(settings, redis)
    task = asyncio.create_task(w.run())
    for _ in range(50):
        if await redis.zscore(HEARTBEAT_INDEX, w.worker_id):
            break
        await asyncio.sleep(0.05)
    assert await redis.zscore(HEARTBEAT_INDEX, w.worker_id) is not None
    w.stop()
    await asyncio.wait_for(task, 2)
    assert await redis.zscore(HEARTBEAT_INDEX, w.worker_id) is None


async def test_security_headers(client: AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["cache-control"] == "no-store"


async def test_dev_login_is_rate_limited(client: AsyncClient) -> None:
    codes = [
        (
            await client.post("/auth/dev/login", json={"email": f"u{i}@example.com", "name": "U"})
        ).status_code
        for i in range(31)
    ]
    assert codes[:30] == [200] * 30
    assert codes[30] == 429
