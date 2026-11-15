"""Shared FastAPI dependencies: DB session, Redis, current user, CSRF."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.sessions import SESSION_COOKIE, SessionStore, csrf_valid
from app.config import Settings
from app.db.models import User
from app.errors import ApiError


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session


def get_redis(request: Request) -> Redis:
    return request.app.state.redis  # type: ignore[no-any-return]


def get_sessions(request: Request) -> SessionStore:
    return request.app.state.sessions  # type: ignore[no-any-return]


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
DbDep = Annotated[AsyncSession, Depends(get_db)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SessionsDep = Annotated[SessionStore, Depends(get_sessions)]


@dataclass(frozen=True)
class Authed:
    user: User
    sid: str


async def current_auth(request: Request, db: DbDep, sessions: SessionsDep) -> Authed:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        raise ApiError(401, "unauthorized", "Sign in required")
    user_id = await sessions.get(sid)
    if user_id is None:
        raise ApiError(401, "unauthorized", "Session expired")
    user = await db.get(User, user_id)
    if user is None or user.deleted_at is not None:
        await sessions.delete(sid)
        raise ApiError(401, "unauthorized", "Account not found")
    return Authed(user=user, sid=sid)


AuthDep = Annotated[Authed, Depends(current_auth)]


def check_origin(request: Request, settings: Settings) -> None:
    """Reject browser requests from origins we don't serve. Requests without an
    Origin header (non-browser clients) still need a valid CSRF token."""
    origin = request.headers.get("origin")
    if origin is not None and origin not in settings.allowed_origins:
        raise ApiError(403, "bad_origin", "Origin not allowed")


async def require_csrf(request: Request, auth: AuthDep, settings: SettingsDep) -> Authed:
    """State-changing routes: Origin allowlist + double-submit CSRF token."""
    check_origin(request, settings)
    if not csrf_valid(
        request.app.state.session_secret, auth.sid, request.headers.get("x-csrf-token")
    ):
        raise ApiError(403, "csrf", "Missing or invalid CSRF token")
    return auth


CsrfAuthDep = Annotated[Authed, Depends(require_csrf)]
