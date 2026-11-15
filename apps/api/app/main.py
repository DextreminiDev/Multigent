"""FastAPI app factory. The API never calls an LLM for chat turns (that's the
worker's job); it only talks to providers to validate keys and list models."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.routes import router as auth_router
from app.auth.sessions import SessionStore
from app.chats.routes import router as chats_router
from app.config import Settings, get_settings
from app.db.session import make_engine, make_sessionmaker
from app.errors import install_error_handlers
from app.health import router as health_router
from app.logging import configure_logging, get_logger
from app.providers.catalog import HttpKeyValidator, KeyValidator
from app.redis_client import make_redis
from app.security.keyvault import KeyVault, LocalKeyVault

log = get_logger("api")


def build_vault(settings: Settings) -> KeyVault | None:
    keys = settings.encryption_keys()
    if not keys:
        log.warning("keyvault.unconfigured", hint="set KEY_ENCRYPTION_KEYS to store API keys")
        return None
    return LocalKeyVault(keys, settings.key_encryption_active_id)


def create_app(
    settings: Settings | None = None, *, key_validator: KeyValidator | None = None
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, json=settings.env != "development")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        redis = make_redis(settings.redis_url)
        owned_validator = None
        if key_validator is None:
            owned_validator = HttpKeyValidator()
        app.state.settings = settings
        app.state.engine = engine
        app.state.sessionmaker = make_sessionmaker(engine)
        app.state.redis = redis
        app.state.sessions = SessionStore(redis, settings.session_ttl_s)
        app.state.vault = build_vault(settings)
        app.state.key_validator = key_validator or owned_validator
        if settings.session_secret:
            app.state.session_secret = settings.session_secret
        else:  # dev/test only (production requires it, see Settings)
            log.warning("session_secret.ephemeral", hint="set SESSION_SECRET")
            app.state.session_secret = secrets.token_urlsafe(32)
        try:
            yield
        finally:
            if owned_validator is not None:
                await owned_validator.aclose()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Multigent API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.env != "production" else None,
        openapi_url="/openapi.json" if settings.env != "production" else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,  # session cookie
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
    )
    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(chats_router)
    return app


app = create_app()
