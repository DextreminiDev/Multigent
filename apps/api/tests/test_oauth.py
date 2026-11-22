"""OAuth sign-in (Google, GitHub) against a mocked identity provider."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.db.models import AuthIdentity, User
from app.main import create_app
from tests.conftest import StubValidator

pytestmark = pytest.mark.integration


class FakeIdP:
    """Answers Google's and GitHub's token + profile endpoints."""

    def __init__(self) -> None:
        self.google_user: dict[str, Any] = {
            "sub": "g-123",
            "email": "Ann@Example.com",
            "email_verified": True,
            "name": "Ann Lee",
        }
        self.github_user: dict[str, Any] = {"id": 42, "login": "octo", "name": None}
        self.github_emails: list[dict[str, Any]] = [
            {"email": "octo@example.com", "primary": True, "verified": True}
        ]
        self.token_status = 200
        self.seen: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.seen.append(req)
        url = str(req.url)
        if url.startswith(
            ("https://oauth2.googleapis.com/token", "https://github.com/login/oauth/access_token")
        ):
            if self.token_status != 200:
                return httpx.Response(self.token_status, json={"error": "bad_code"})
            return httpx.Response(200, json={"access_token": "tok-" + uuid.uuid4().hex})
        if url.startswith("https://openidconnect.googleapis.com/v1/userinfo"):
            return httpx.Response(200, json=self.google_user)
        if url == "https://api.github.com/user":
            return httpx.Response(200, json=self.github_user)
        if url == "https://api.github.com/user/emails":
            return httpx.Response(200, json=self.github_emails)
        return httpx.Response(404)


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@pytest.fixture
async def oauth_app(
    settings: Settings, clean_db: None, redis: Redis, idp: FakeIdP
) -> AsyncIterator[FastAPI]:
    s = settings.model_copy(
        update={
            "oauth_google_client_id": "gid",
            "oauth_google_client_secret": "gsecret",
            "oauth_github_client_id": "hid",
            "oauth_github_client_secret": "hsecret",
            "public_api_url": "http://api.test",
            "web_url": "http://web.test",
        }
    )
    app = create_app(s, key_validator=StubValidator())
    async with app.router.lifespan_context(app):
        app.state.oauth_http = httpx.AsyncClient(transport=httpx.MockTransport(idp))
        yield app


@pytest.fixture
async def browser(oauth_app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=oauth_app), base_url="http://api.test", follow_redirects=False
    ) as c:
        yield c


async def sign_in(c: AsyncClient, provider: str, next_path: str = "/c/abc") -> httpx.Response:
    start = await c.get(f"/auth/{provider}/login", params={"next": next_path})
    assert start.status_code == 302
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    return await c.get(f"/auth/{provider}/callback", params={"code": "the-code", "state": state})


async def test_providers_listed(browser: AsyncClient, client: AsyncClient) -> None:
    assert (await browser.get("/auth/providers")).json() == {
        "dev": True,
        "google": True,
        "github": True,
    }
    assert (await client.get("/auth/providers")).json()["google"] is False  # no credentials
    assert (await client.get("/auth/google/login")).status_code == 404


async def test_login_redirect_has_state_pkce_and_cookie(browser: AsyncClient, redis: Redis) -> None:
    r = await browser.get("/auth/google/login", params={"next": "/invite/xyz?join=1"})
    loc = urlsplit(r.headers["location"])
    q = {k: v[0] for k, v in parse_qs(loc.query).items()}
    assert loc.netloc == "accounts.google.com"
    assert q["redirect_uri"] == "http://api.test/auth/google/callback"
    assert q["code_challenge_method"] == "S256"
    assert "openid" in q["scope"]
    assert (
        "oauth_state" in r.headers["set-cookie"].lower()
        and "httponly" in r.headers["set-cookie"].lower()
    )
    saved = json.loads(await redis.get(f"oauth:{q['state']}") or "{}")
    assert saved["next"] == "/invite/xyz?join=1"
    assert len(saved["verifier"]) >= 43


async def test_google_sign_in_creates_user_and_session(
    browser: AsyncClient, idp: FakeIdP, oauth_app: FastAPI
) -> None:
    r = await sign_in(browser, "google", "/c/abc")
    assert r.status_code == 302
    assert r.headers["location"] == "http://web.test/c/abc"
    me = (await browser.get("/me")).json()
    assert me["user"]["email"] == "ann@example.com"
    assert me["user"]["name"] == "Ann Lee"
    # the code verifier went to the token endpoint (PKCE)
    token_req = next(x for x in idp.seen if "oauth2.googleapis.com/token" in str(x.url))
    assert b"code_verifier=" in token_req.content
    # signing in again maps to the same user via the stored identity
    first_id = me["user"]["id"]
    await browser.post("/auth/logout", headers={"X-CSRF-Token": me["csrf_token"]})
    await sign_in(browser, "google")
    assert (await browser.get("/me")).json()["user"]["id"] == first_id


async def test_github_uses_verified_primary_email_and_links_existing_account(
    browser: AsyncClient, oauth_app: FastAPI
) -> None:
    sm: async_sessionmaker[AsyncSession] = oauth_app.state.sessionmaker
    async with sm() as s:  # an account that already exists (e.g. from dev login)
        s.add(User(email="octo@example.com", name="Octo Cat", avatar_color="#000"))
        await s.commit()
    r = await sign_in(browser, "github")
    assert r.headers["location"] == "http://web.test/c/abc"
    me = (await browser.get("/me")).json()["user"]
    assert (me["email"], me["name"]) == ("octo@example.com", "Octo Cat")
    async with sm() as s:
        ids = (await s.scalars(select(AuthIdentity).where(AuthIdentity.provider == "github"))).all()
        assert [i.provider_user_id for i in ids] == ["42"]


@pytest.mark.parametrize("case", ["unverified_google", "no_verified_github", "token_fails"])
async def test_rejected_sign_ins_go_back_to_login(
    browser: AsyncClient, idp: FakeIdP, case: str
) -> None:
    provider = "google"
    if case == "unverified_google":
        idp.google_user["email_verified"] = False
    elif case == "no_verified_github":
        provider = "github"
        idp.github_emails = [{"email": "x@example.com", "primary": True, "verified": False}]
    else:
        idp.token_status = 400
    r = await sign_in(browser, provider)
    assert r.headers["location"] == "http://web.test/login?error=oauth_failed"
    assert (await browser.get("/me")).status_code == 401


async def test_state_is_single_use_and_bound_to_the_browser(
    browser: AsyncClient, oauth_app: FastAPI
) -> None:
    start = await browser.get("/auth/google/login")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    # an attacker's browser (no state cookie) can't complete the victim's flow
    async with AsyncClient(
        transport=ASGITransport(app=oauth_app), base_url="http://api.test", follow_redirects=False
    ) as attacker:
        r = await attacker.get("/auth/google/callback", params={"code": "c", "state": state})
        assert r.headers["location"].endswith("/login?error=oauth_failed")
    # ...and the state can't be replayed afterwards either
    r = await browser.get("/auth/google/callback", params={"code": "c", "state": state})
    assert r.headers["location"].endswith("/login?error=oauth_failed")


async def test_next_cannot_redirect_off_site(browser: AsyncClient) -> None:
    r = await sign_in(browser, "google", "//evil.test/phish")
    assert r.headers["location"] == "http://web.test/"
