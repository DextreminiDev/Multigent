"""OAuth sign-in with Google and GitHub (ARCHITECTURE.md §11.1).

Authorization-code flow, server side:
  GET /auth/{provider}/login     -> 302 to the provider (state + PKCE for Google)
  GET /auth/{provider}/callback  -> exchange code, read the verified email, find or
                                    create the user, start a session, 302 to the web app

- `state` is random, single-use (Redis GETDEL, 10 min) and bound to the browser by
  a short-lived HttpOnly cookie, which prevents login CSRF.
- Only *verified* emails are accepted, and an existing account is linked by email
  only when the provider says that email is verified.
- `next` is a same-origin relative path (no open redirects).
- Provider tokens are used once to read the profile and never stored.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.routes import set_session_cookie
from app.config import Settings
from app.db.models import AuthIdentity, User
from app.deps import DbDep, RedisDep, SessionsDep, SettingsDep
from app.errors import not_found
from app.ids import uuid7
from app.logging import get_logger
from app.security.ratelimit import limit_ip
from app.users.names import avatar_color, sanitize_display_name

router = APIRouter(tags=["auth"])
log = get_logger("oauth")

Provider = Literal["google", "github"]
STATE_TTL_S = 600
STATE_COOKIE = "oauth_state"


@dataclass(frozen=True)
class ProviderConfig:
    authorize_url: str
    token_url: str
    scope: str
    pkce: bool


CONFIG: dict[str, ProviderConfig] = {
    "google": ProviderConfig(
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://oauth2.googleapis.com/token",
        "openid email profile",
        pkce=True,
    ),
    "github": ProviderConfig(
        "https://github.com/login/oauth/authorize",
        "https://github.com/login/oauth/access_token",
        "read:user user:email",
        pkce=False,
    ),
}


@dataclass(frozen=True)
class Profile:
    subject: str
    email: str
    name: str


class OAuthError(Exception):
    """Anything that should send the user back to /login with an error."""


class AuthProviders(BaseModel):
    dev: bool
    google: bool
    github: bool


def _credentials(settings: Settings, provider: str) -> tuple[str, str]:
    if provider == "google":
        return settings.oauth_google_client_id, settings.oauth_google_client_secret
    if provider == "github":
        return settings.oauth_github_client_id, settings.oauth_github_client_secret
    return "", ""


def enabled(settings: Settings, provider: str) -> bool:
    cid, secret = _credentials(settings, provider)
    return bool(cid and secret)


def redirect_uri(settings: Settings, provider: str) -> str:
    return f"{settings.public_api_url.rstrip('/')}/auth/{provider}/callback"


def safe_next(raw: str | None) -> str:
    if not raw or not raw.startswith("/") or raw.startswith("//") or raw.startswith("/\\"):
        return "/"
    return raw


def _http(request: Request) -> httpx.AsyncClient:
    client: httpx.AsyncClient | None = getattr(request.app.state, "oauth_http", None)
    if client is None:
        client = httpx.AsyncClient(timeout=10.0, follow_redirects=False)
        request.app.state.oauth_http = client
    return client


@router.get("/auth/providers", response_model=AuthProviders)
async def auth_providers(settings: SettingsDep) -> AuthProviders:
    return AuthProviders(
        dev=settings.dev_login,
        google=enabled(settings, "google"),
        github=enabled(settings, "github"),
    )


@router.get("/auth/{provider}/login")
async def oauth_login(
    provider: str,
    request: Request,
    settings: SettingsDep,
    redis: RedisDep,
    next: str | None = None,
) -> RedirectResponse:
    if provider not in CONFIG or not enabled(settings, provider):
        raise not_found("Sign-in provider not available")
    await limit_ip(request, redis, "oauth_login", limit=30, window_s=60)
    cfg = CONFIG[provider]
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(48) if cfg.pkce else None
    await redis.set(
        f"oauth:{state}",
        json.dumps({"provider": provider, "next": safe_next(next), "verifier": verifier}),
        ex=STATE_TTL_S,
    )
    cid, _ = _credentials(settings, provider)
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri(settings, provider),
        "response_type": "code",
        "scope": cfg.scope,
        "state": state,
    }
    if verifier:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        params |= {"code_challenge": challenge.rstrip(b"=").decode(),
                   "code_challenge_method": "S256", "prompt": "select_account"}  # fmt: skip
    resp = RedirectResponse(f"{cfg.authorize_url}?{urlencode(params)}", status_code=302)
    resp.set_cookie(STATE_COOKIE, state, max_age=STATE_TTL_S, httponly=True,
                    secure=settings.secure_cookies, samesite="lax",
                    path=f"/auth/{provider}/callback")  # fmt: skip
    return resp


async def _exchange(
    http: httpx.AsyncClient, settings: Settings, provider: str, code: str, verifier: str | None
) -> str:
    cfg = CONFIG[provider]
    cid, secret = _credentials(settings, provider)
    data = {
        "client_id": cid,
        "client_secret": secret,
        "code": code,
        "redirect_uri": redirect_uri(settings, provider),
        "grant_type": "authorization_code",
    }
    if verifier:
        data["code_verifier"] = verifier
    r = await http.post(cfg.token_url, data=data, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OAuthError(f"token exchange failed ({r.status_code})")
    token = r.json().get("access_token")
    if not token:
        raise OAuthError("no access token")
    return str(token)


async def _profile(http: httpx.AsyncClient, provider: str, token: str) -> Profile:
    auth = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if provider == "google":
        r = await http.get("https://openidconnect.googleapis.com/v1/userinfo", headers=auth)
        if r.status_code != 200:
            raise OAuthError("userinfo failed")
        u = r.json()
        if not u.get("email") or u.get("email_verified") is not True:
            raise OAuthError("email not verified")
        return Profile(str(u["sub"]), str(u["email"]).lower(), str(u.get("name") or ""))
    r = await http.get("https://api.github.com/user", headers=auth)
    emails = await http.get("https://api.github.com/user/emails", headers=auth)
    if r.status_code != 200 or emails.status_code != 200:
        raise OAuthError("github profile failed")
    u = r.json()
    primary = next((e for e in emails.json() if e.get("primary") and e.get("verified")), None)
    if primary is None:
        raise OAuthError("no verified primary email")
    return Profile(
        str(u["id"]), str(primary["email"]).lower(), str(u.get("name") or u.get("login") or "")
    )


def _display_name(profile: Profile) -> str:
    for candidate in (profile.name, profile.email.split("@", 1)[0]):
        try:
            return sanitize_display_name(candidate)
        except ValueError:
            continue
    return "User"


async def upsert_user(db: AsyncSession, provider: str, profile: Profile) -> User:
    ident = await db.scalar(
        select(AuthIdentity).where(
            AuthIdentity.provider == provider, AuthIdentity.provider_user_id == profile.subject
        )
    )
    if ident is not None:
        user = await db.get(User, ident.user_id)
        if user is None or user.deleted_at is not None:
            raise OAuthError("account deleted")
        return user
    # link by verified email, else create
    user = await db.scalar(select(User).where(User.email == profile.email))
    if user is None:
        uid = uuid7()
        user = User(id=uid, email=profile.email, name=_display_name(profile),
                    avatar_color=avatar_color(uid))  # fmt: skip
        db.add(user)
        await db.flush()
    elif user.deleted_at is not None:
        raise OAuthError("account deleted")
    db.add(AuthIdentity(user_id=user.id, provider=provider, provider_user_id=profile.subject))
    await db.commit()
    return user


@router.get("/auth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    db: DbDep,
    redis: RedisDep,
    sessions: SessionsDep,
    settings: SettingsDep,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    web = settings.web_url.rstrip("/")

    def fail(reason: str) -> RedirectResponse:
        log.info("oauth.failed", provider=provider, reason=reason)
        resp = RedirectResponse(f"{web}/login?error=oauth_failed", status_code=302)
        resp.delete_cookie(STATE_COOKIE, path=f"/auth/{provider}/callback")
        return resp

    if provider not in CONFIG or not enabled(settings, provider):
        raise not_found("Sign-in provider not available")
    if error or not code or not state:
        return fail(error or "missing code/state")
    raw = await redis.getdel(f"oauth:{state}")  # single use
    if raw is None:
        return fail("unknown or expired state")
    saved = json.loads(raw)
    cookie = request.cookies.get(STATE_COOKIE)
    if saved.get("provider") != provider or not cookie or not secrets.compare_digest(cookie, state):
        return fail("state mismatch")
    try:
        http = _http(request)
        token = await _exchange(http, settings, provider, code, saved.get("verifier"))
        profile = await _profile(http, provider, token)
        user = await upsert_user(db, provider, profile)
    except (OAuthError, httpx.HTTPError, ValueError, KeyError) as e:
        return fail(type(e).__name__ if not isinstance(e, OAuthError) else str(e))
    sid = await sessions.create(user.id)
    resp = RedirectResponse(f"{web}{safe_next(saved.get('next'))}", status_code=302)
    set_session_cookie(resp, sid, settings)
    resp.delete_cookie(STATE_COOKIE, path=f"/auth/{provider}/callback")
    log.info("oauth.signed_in", provider=provider, user_id=str(user.id))
    return resp
