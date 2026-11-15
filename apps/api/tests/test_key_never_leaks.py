"""I4: the owner's API key never appears in any API response, published event,
Redis value, log line, error message, or DB column other than `ciphertext`
(ARCHITECTURE.md §11.4). WebSocket frames are added to this sweep with the
realtime layer (milestone 3)."""

from __future__ import annotations

import logging
import uuid

import pytest
import structlog
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.models import Base, ChatMember
from app.logging import configure_logging
from tests.conftest import LoginFn, StubValidator

pytestmark = pytest.mark.integration

# Deliberately NOT key-shaped, so only literal registration (not regex) can catch it.
SECRET = "zq-" + uuid.uuid4().hex + "-owner-secret"
GEMINI_SHAPED = "AIzaSy" + uuid.uuid4().hex + "abcdefg"


class LeakyValidator(StubValidator):
    """Simulates the worst provider SDK: logs the raw key and echoes it in errors."""

    async def list_models(self, provider: str, api_key: str, base_url: str | None = None):  # type: ignore[no-untyped-def]
        structlog.get_logger("provider_sdk").info("request", url=f"https://p.test?key={api_key}")
        logging.getLogger("provider_sdk.http").warning("sending key %s", api_key)
        return await super().list_models(provider, api_key, base_url)


@pytest.fixture
def validator() -> StubValidator:
    return LeakyValidator(echo_key_in_errors=True)


async def test_key_never_leaks(
    login: LoginFn,
    validator: StubValidator,
    redis: Redis,
    engine: AsyncEngine,
    sessionmaker: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG", json=True)  # bind log output to capsys, log everything
    pubsub = redis.pubsub()
    await pubsub.psubscribe("chat:*")
    await pubsub.get_message(timeout=1)

    owner, editor, viewer = await login("Owner"), await login("Ed"), await login("Vi")
    users = (owner, editor, viewer)
    cid = (await owner.post("/chats", json={"title": "Leak test"})).json()["id"]
    async with sessionmaker() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(editor.id), role="editor"))
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(viewer.id), role="viewer"))
        await s.commit()

    for key in (SECRET, GEMINI_SHAPED):
        # 1) provider rejects the key and echoes it in its error + logs
        await owner.put(f"/chats/{cid}/key", json={"provider": "gemini", "api_key": key})
        # 2) malformed submission containing the key (fails request validation)
        await owner.put(f"/chats/{cid}/key", json={"provider": "gemini", "api_key": key + " x"})
        await owner.put(f"/chats/{cid}/key", json={"provider": "bogus", "api_key": key})
        # 3) outage path: stored as unvalidated
        validator.mode = "unavailable"
        await owner.put(f"/chats/{cid}/key", json={"provider": "gemini", "api_key": key})
        # 4) accepted
        validator.mode = "normal"
        validator.valid.add(key)
        r = await owner.put(f"/chats/{cid}/key", json={"provider": "gemini", "api_key": key})
        assert r.status_code == 200
        # 5) revalidate + list models, then make provider fail on list (error echo path)
        await owner.post(f"/chats/{cid}/key/validate")
        await owner.get(f"/chats/{cid}/models")
        validator.valid.discard(key)
        await owner.get(f"/chats/{cid}/models")
        await owner.post(f"/chats/{cid}/key/validate")
        # 6) collaborators try everything
        for u in (editor, viewer):
            await u.put(f"/chats/{cid}/key", json={"provider": "gemini", "api_key": "x" * 10})
            await u.get(f"/chats/{cid}/models")
            await u.post(f"/chats/{cid}/key/validate")
        # 7) every read endpoint for every user
        for u in users:
            await u.get("/me")
            await u.get("/chats")
            await u.get(f"/chats/{cid}")
            await u.get(f"/chats/{cid}/messages")
            await u.get("/providers")

    # ---- collect every surface
    surfaces: dict[str, str] = {}
    for u in users:
        for i, resp in enumerate(u.log):
            surfaces[f"{u.user['name']} resp {i} {resp.request.url.path}"] = resp.text + str(
                dict(resp.headers)
            )

    published = []
    while m := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2):
        published.append(str(m["data"]))
    await pubsub.aclose()  # type: ignore[no-untyped-call]
    assert published, "expected some events to have been published"
    surfaces["pubsub"] = "\n".join(published)

    redis_dump: list[str] = []
    async for k in redis.scan_iter("*"):
        t = await redis.type(k)
        if t == "string":
            redis_dump.append(f"{k!s}={await redis.get(k)!s}")
        elif t == "hash":
            redis_dump.append(f"{k}={await redis.hgetall(k)}")
        elif t == "set":
            redis_dump.append(f"{k}={await redis.smembers(k)}")
        elif t == "zset":
            redis_dump.append(f"{k}={await redis.zrange(k, 0, -1)}")
        elif t == "list":
            redis_dump.append(f"{k}={await redis.lrange(k, 0, -1)}")
    surfaces["redis"] = "\n".join(redis_dump)

    db_dump: list[str] = []
    async with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            cols = [c.name for c in table.columns if c.name != "ciphertext"]
            sub = f"SELECT {', '.join(cols)} FROM {table.name}"
            rows = await conn.execute(text(f"SELECT row_to_json(t)::text FROM ({sub}) t"))
            db_dump.extend(f"{table.name}: {r[0]}" for r in rows)
    surfaces["db"] = "\n".join(db_dump)
    assert "chat_keys" in surfaces["db"]  # sanity: the key row exists

    surfaces["logs"] = capsys.readouterr().out
    # sanity: the leaky "SDK" logs were emitted (and scrubbed) on both log paths
    assert "p.test?key=[REDACTED]" in surfaces["logs"]
    assert "sending key [REDACTED]" in surfaces["logs"]

    # ---- the actual invariant
    for name, blob in surfaces.items():
        for secret in (SECRET, GEMINI_SHAPED):
            assert secret not in blob, f"API key leaked via {name}"
            assert secret[-12:] not in blob, f"API key fragment leaked via {name}"
