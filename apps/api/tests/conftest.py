"""Test fixtures. Integration tests use real Postgres + Redis (docker compose
services locally, GitHub Actions services in CI). Override with TEST_DATABASE_URL /
TEST_REDIS_URL."""

from __future__ import annotations

import base64
import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.session import make_engine, make_sessionmaker
from app.main import create_app
from app.providers.base import KeyCheck, ModelInfo
from app.providers.errors import AuthError, ProviderError, ProviderUnavailable
from app.providers.fake import FakeProvider
from app.providers.registry import ProviderRegistry
from app.redis_client import make_redis
from app.turns.runner import TurnRunner

API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://multigent:multigent@localhost:55432/multigent_test",
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:56379/15")
TEST_MASTER_KEYS = {
    "k1": base64.b64encode(b"\x01" * 32).decode(),
    "k2": base64.b64encode(b"\x02" * 32).decode(),
}


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        env="test",
        database_url=TEST_DATABASE_URL,
        redis_url=TEST_REDIS_URL,
        session_secret="test-session-secret-0123456789abcdef",
        key_encryption_keys=json.dumps(TEST_MASTER_KEYS),
        key_encryption_active_id="k2",
        dev_login=True,
    )


@pytest.fixture(scope="session")
def migrated_db() -> str:
    """Run migrations from scratch once per session (also proves down/up works)."""
    cfg = Config(os.path.join(API_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(API_DIR, "app/db/alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    return TEST_DATABASE_URL


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = make_engine(migrated_db)
    yield eng
    await eng.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_sessionmaker(engine)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    r = make_redis(TEST_REDIS_URL)
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


# --------------------------------------------------------------------- provider stub


@dataclass
class StubValidator:
    """KeyValidator stand-in. Keys in `valid` pass; anything else is an AuthError.
    `mode="unavailable"` simulates a provider outage; `echo_key_in_errors` makes
    the 'provider' put the raw key into its error text (like Gemini's ?key= URLs)."""

    valid: set[str] = field(default_factory=set)
    mode: str = "normal"
    echo_key_in_errors: bool = False
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    def _err(self, api_key: str) -> ProviderError:
        detail = f" (request url: https://p.test/v1?key={api_key} key={api_key})"
        if self.mode == "unavailable":
            return ProviderUnavailable("timeout" + (detail if self.echo_key_in_errors else ""))
        return AuthError("bad key" + (detail if self.echo_key_in_errors else ""))

    async def list_models(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> list[ModelInfo]:
        self.calls.append((provider, base_url))
        if self.mode != "normal" or api_key not in self.valid:
            raise self._err(api_key)
        return [ModelInfo(id="model-a", context_window=1000, extra={"label": "Model A"})]

    async def validate_key(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> KeyCheck:
        try:
            await self.list_models(provider, api_key, base_url)
        except ProviderError as e:
            return KeyCheck(ok=False, error_code=e.code, message=e.message)
        return KeyCheck(ok=True)


@pytest.fixture
def validator() -> StubValidator:
    return StubValidator()


# --------------------------------------------------------------------- app + clients


@pytest.fixture
async def clean_db(engine: AsyncEngine) -> None:
    """Every app-level test starts from empty tables."""
    from sqlalchemy import text

    from app.db.models import Base

    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} CASCADE"))


@pytest.fixture
async def app(
    settings: Settings, clean_db: None, redis: Redis, validator: StubValidator
) -> AsyncIterator[FastAPI]:
    application = create_app(settings, key_validator=validator)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@dataclass
class UserClient:
    """An authenticated browser-ish client: own cookie jar + CSRF token."""

    http: AsyncClient
    csrf: str
    user: dict[str, Any]
    log: list[Response] = field(default_factory=list)

    @property
    def id(self) -> str:
        return str(self.user["id"])

    async def req(self, method: str, url: str, **kw: Any) -> Response:
        headers = {"X-CSRF-Token": self.csrf, **kw.pop("headers", {})}
        r = await self.http.request(method, url, headers=headers, **kw)
        self.log.append(r)
        return r

    async def get(self, url: str, **kw: Any) -> Response:
        return await self.req("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> Response:
        return await self.req("POST", url, **kw)

    async def put(self, url: str, **kw: Any) -> Response:
        return await self.req("PUT", url, **kw)

    async def patch(self, url: str, **kw: Any) -> Response:
        return await self.req("PATCH", url, **kw)

    async def delete(self, url: str, **kw: Any) -> Response:
        return await self.req("DELETE", url, **kw)


LoginFn = Callable[[str], Awaitable[UserClient]]


@pytest.fixture
async def login(app: FastAPI) -> AsyncIterator[LoginFn]:
    opened: list[AsyncClient] = []

    async def _login(name: str) -> UserClient:
        http = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        opened.append(http)
        r = await http.post(
            "/auth/dev/login", json={"email": f"{name.lower()}@example.com", "name": name}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        return UserClient(http=http, csrf=body["csrf_token"], user=body["user"])

    yield _login
    for c in opened:
        await c.aclose()


# --------------------------------------------------------------------- turn engine


@pytest.fixture
def providers() -> ProviderRegistry:
    return ProviderRegistry(fake=FakeProvider())


@pytest.fixture
def runner(app: FastAPI, providers: ProviderRegistry) -> TurnRunner:
    return TurnRunner(
        app.state.sessionmaker,
        app.state.redis,
        app.state.vault,
        providers,
        lock_ttl_ms=3000,
        heartbeat_s=0.3,
    )


async def ready_chat(
    owner: UserClient, validator: StubValidator, key: str = "demo-key-1234"
) -> str:
    """Create a chat with a working (fake) key and a model selected."""
    validator.valid.add(key)
    cid = (await owner.post("/chats", json={"title": "T"})).json()["id"]
    r = await owner.put(f"/chats/{cid}/key", json={"provider": "fake", "api_key": key})
    assert r.status_code == 200, r.text
    r = await owner.patch(f"/chats/{cid}", json={"model": "fake-small"})
    assert r.status_code == 200, r.text
    return str(cid)
