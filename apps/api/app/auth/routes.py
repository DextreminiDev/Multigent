"""Auth routes: dev login (env-gated), logout, /me.

OAuth (Google, GitHub) plugs into the same SessionStore later; see
ARCHITECTURE.md §11.1.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy import select

from app.auth.sessions import SESSION_COOKIE, csrf_token
from app.config import Settings
from app.db.models import AuthIdentity, User
from app.deps import AuthDep, CsrfAuthDep, DbDep, SessionsDep, SettingsDep, check_origin
from app.errors import ApiError
from app.ids import uuid7
from app.security.ratelimit import limit_ip
from app.users.names import avatar_color, sanitize_display_name

router = APIRouter(tags=["auth"])


class UserView(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    avatar_color: str


class MeView(BaseModel):
    user: UserView
    csrf_token: str


class DevLoginIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=200)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        return sanitize_display_name(v)


def user_view(u: User) -> UserView:
    return UserView(id=u.id, email=u.email, name=u.name, avatar_color=u.avatar_color)


def set_session_cookie(response: Response, sid: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=settings.session_ttl_s,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        domain=settings.cookie_domain or None,
        path="/",
    )


@router.post("/auth/dev/login", response_model=MeView)
async def dev_login(
    body: DevLoginIn,
    request: Request,
    response: Response,
    db: DbDep,
    sessions: SessionsDep,
    settings: SettingsDep,
) -> MeView:
    if not settings.dev_login:
        raise ApiError(404, "not_found", "Not found")
    check_origin(request, settings)
    await limit_ip(request, request.app.state.redis, "dev_login", limit=30, window_s=60)
    email = body.email.lower()
    user = await db.scalar(select(User).where(User.email == email))
    if user is None:
        uid = uuid7()
        user = User(id=uid, email=email, name=body.name, avatar_color=avatar_color(uid))
        db.add(user)
        await db.flush()  # no ORM relationships: order inserts explicitly
        db.add(AuthIdentity(user_id=uid, provider="dev", provider_user_id=email))
        await db.commit()
    elif user.deleted_at is not None:
        raise ApiError(403, "account_deleted", "This account was deleted")
    sid = await sessions.create(user.id)
    set_session_cookie(response, sid, settings)
    return MeView(
        user=user_view(user), csrf_token=csrf_token(request.app.state.session_secret, sid)
    )


@router.post("/auth/logout", status_code=204)
async def logout(
    auth: CsrfAuthDep, response: Response, sessions: SessionsDep, settings: SettingsDep
) -> None:
    await sessions.delete(auth.sid)
    response.delete_cookie(SESSION_COOKIE, domain=settings.cookie_domain or None, path="/")


@router.get("/me", response_model=MeView)
async def me(auth: AuthDep, request: Request) -> MeView:
    return MeView(
        user=user_view(auth.user),
        csrf_token=csrf_token(request.app.state.session_secret, auth.sid),
    )
