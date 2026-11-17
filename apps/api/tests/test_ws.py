"""WebSocket gateway against a real uvicorn server + real WS client: auth, replay,
live streaming, mid-stream join (snapshot + dedupe by idx), resync, key secrecy."""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidStatus

from app.db.models import Chat, ChatEvent, ChatMember
from app.providers.fake import FakeScript
from app.providers.registry import ProviderRegistry
from app.turns.runner import TurnRunner
from tests.conftest import StubValidator

pytestmark = pytest.mark.integration
ORIGIN = "http://localhost:3000"
KEY = "ws-demo-key-" + uuid.uuid4().hex


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def base(app: FastAPI) -> AsyncIterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 (uvicorn exposes a flag, not an Event)
        await asyncio.sleep(0.01)
    yield f"127.0.0.1:{port}"
    server.should_exit = True
    await task


class Client:
    def __init__(self, base: str, http: httpx.AsyncClient, csrf: str, user: dict[str, Any]):
        self.base, self.http, self.csrf, self.user = base, http, csrf, user
        self.ws: ClientConnection | None = None
        self.frames: list[dict[str, Any]] = []
        self.raw: list[str] = []
        self._n = 0
        self._matched: set[int] = set()

    @property
    def id(self) -> str:
        return str(self.user["id"])

    async def connect(self) -> None:
        sid = self.http.cookies.get("sid")
        self.ws = await connect(
            f"ws://{self.base}/ws",
            origin=ORIGIN,  # type: ignore[arg-type]
            additional_headers={"Cookie": f"sid={sid}"},
        )

    async def send(self, type_: str, **kw: Any) -> str:
        assert self.ws is not None
        self._n += 1
        ref = f"r{self._n}"
        await self.ws.send(json.dumps({"type": type_, "ref": ref, **kw}))
        return ref

    async def until(
        self, pred: Callable[[dict[str, Any]], bool], timeout: float = 5
    ) -> dict[str, Any]:
        """Return the first not-yet-matched frame satisfying `pred`, including frames
        that arrived earlier while waiting for something else (e.g. before an ack)."""
        assert self.ws is not None
        for i, f in enumerate(self.frames):
            if i not in self._matched and pred(f):
                self._matched.add(i)
                return f
        async with asyncio.timeout(timeout):
            while True:
                raw = str(await self.ws.recv())
                self.raw.append(raw)
                frame = json.loads(raw)
                self.frames.append(frame)
                if pred(frame):
                    self._matched.add(len(self.frames) - 1)
                    return frame  # type: ignore[no-any-return]

    async def ack(self, ref: str) -> dict[str, Any]:
        f = await self.until(lambda f: f.get("ref") == ref)
        assert f["type"] == "ack", f
        return f

    async def rest(self, method: str, url: str, **kw: Any) -> httpx.Response:
        return await self.http.request(method, url, headers={"X-CSRF-Token": self.csrf}, **kw)

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
        await self.http.aclose()


@pytest.fixture
async def clients(base: str) -> AsyncIterator[Callable[[str], Any]]:
    made: list[Client] = []

    async def make(name: str) -> Client:
        http = httpx.AsyncClient(base_url=f"http://{base}")
        r = await http.post(
            "/auth/dev/login", json={"email": f"{name.lower()}@example.com", "name": name}
        )
        body = r.json()
        c = Client(base, http, body["csrf_token"], body["user"])
        made.append(c)
        return c

    yield make
    for c in made:
        await c.close()


async def ready(owner: Client, validator: StubValidator) -> str:
    validator.valid.add(KEY)
    cid = (await owner.rest("POST", "/chats", json={"title": "WS"})).json()["id"]
    assert (
        await owner.rest("PUT", f"/chats/{cid}/key", json={"provider": "fake", "api_key": KEY})
    ).status_code == 200
    await owner.rest("PATCH", f"/chats/{cid}", json={"model": "fake-small"})
    return str(cid)


async def add_member(sm: async_sessionmaker[AsyncSession], cid: str, u: Client, role: str) -> None:
    async with sm() as s:
        s.add(ChatMember(chat_id=uuid.UUID(cid), user_id=uuid.UUID(u.id), role=role))
        await s.commit()


# ------------------------------------------------------------------ auth


async def test_rejects_missing_cookie_and_foreign_origin(base: str, clients: Any) -> None:
    with pytest.raises(InvalidStatus):
        await connect(f"ws://{base}/ws", origin=ORIGIN)  # type: ignore[arg-type]
    c = await clients("Alice")
    sid = c.http.cookies.get("sid")
    with pytest.raises(InvalidStatus):
        await connect(
            f"ws://{base}/ws",
            origin="https://evil.test",  # type: ignore[arg-type]
            additional_headers={"Cookie": f"sid={sid}"},
        )
    await c.connect()  # sanity: correct origin + cookie works


async def test_bad_frames_and_non_member(clients: Any, validator: StubValidator) -> None:
    owner, stranger = await clients("Alice"), await clients("Zed")
    cid = await ready(owner, validator)
    await stranger.connect()
    assert stranger.ws is not None
    await stranger.ws.send("not json")
    assert (await stranger.until(lambda f: f["type"] == "error"))["code"] == "bad_frame"
    ref = await stranger.send("subscribe", chat_id=cid, after_seq=0)
    err = await stranger.until(lambda f: f.get("ref") == ref)
    assert (err["type"], err["code"]) == ("error", "not_found")
    ref = await stranger.send("nope")
    assert (await stranger.until(lambda f: f.get("ref") == ref))["code"] == "unknown_type"
    ref = await stranger.send(
        "message.send", chat_id=cid, client_msg_id=str(uuid.uuid4()), content="let me in"
    )
    assert (await stranger.until(lambda f: f.get("ref") == ref))["code"] == "not_found"


# ------------------------------------------------------------------ replay


async def test_subscribe_replays_from_after_seq(clients: Any, validator: StubValidator) -> None:
    owner = await clients("Alice")
    cid = await ready(owner, validator)  # chat.created, key.status, chat.updated x2...
    await owner.connect()
    ref = await owner.send("subscribe", chat_id=cid, after_seq=0)
    ack = await owner.ack(ref)
    replayed = [f for f in owner.frames if f.get("replay")]
    seqs = [f["seq"] for f in replayed]
    assert seqs == list(range(1, ack["seq"] + 1))
    assert replayed[0]["type"] == "chat.created"

    owner.frames.clear()
    owner._matched.clear()
    ref = await owner.send("subscribe", chat_id=cid, after_seq=ack["seq"] - 1)
    await owner.ack(ref)
    assert [f["seq"] for f in owner.frames if f.get("replay")] == [ack["seq"]]


async def test_resync_required_when_too_far_behind(
    clients: Any, validator: StubValidator, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    owner = await clients("Alice")
    cid = await ready(owner, validator)
    async with sessionmaker() as s:
        chat = await s.get(Chat, uuid.UUID(cid))
        assert chat is not None
        start = chat.last_seq
        for i in range(1, 502):
            s.add(ChatEvent(chat_id=chat.id, seq=start + i, type="noise", payload={}))
        await s.execute(update(Chat).where(Chat.id == chat.id).values(last_seq=start + 501))
        await s.commit()
    await owner.connect()
    ref = await owner.send("subscribe", chat_id=cid, after_seq=0)
    f = await owner.until(lambda f: f["type"] == "resync_required")
    assert f["last_seq"] == start + 501
    assert (await owner.ack(ref))["seq"] == start + 501


# ------------------------------------------------------------------ live


async def test_send_and_stream_live(
    clients: Any,
    validator: StubValidator,
    runner: TurnRunner,
    providers: ProviderRegistry,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    owner, viewer = await clients("Alice"), await clients("Vi")
    cid = await ready(owner, validator)
    await add_member(sessionmaker, cid, viewer, "viewer")
    providers.fake.script = FakeScript(chunks=["Hello ", "there, ", "**Alice**", "!"], delay_s=0.05)
    for c in (owner, viewer):
        await c.connect()
        await c.ack(await c.send("subscribe", chat_id=cid, after_seq=0))

    cmid = str(uuid.uuid4())
    ack = await owner.ack(
        await owner.send("message.send", chat_id=cid, client_msg_id=cmid, content="hi everyone")
    )
    assert ack["created"] is True
    assert ack["message"]["client_msg_id"] == cmid
    dup = await owner.ack(
        await owner.send("message.send", chat_id=cid, client_msg_id=cmid, content="hi everyone")
    )
    assert dup["created"] is False
    assert dup["message"]["id"] == ack["message"]["id"]

    created = await viewer.until(lambda f: f["type"] == "message.created")
    assert created["payload"]["message"]["content"] == "hi everyone"

    await runner.run_chat(uuid.UUID(cid))
    done = await viewer.until(lambda f: f["type"] == "turn.completed")
    started = next(f for f in viewer.frames if f["type"] == "turn.started")
    deltas = [f["payload"] for f in viewer.frames if f["type"] == "turn.delta"]
    assert started["seq"] < done["seq"]
    assert started["payload"]["author_ids"] == [owner.id]
    assert [d["idx"] for d in deltas] == list(range(len(deltas)))
    assert "".join(d["text"] for d in deltas) == "Hello there, **Alice**!"
    msg = done["payload"]["message"]
    assert msg["content"] == "Hello there, **Alice**!"
    assert msg["anchor_seq"] == started["seq"]

    # viewers can watch but not send
    ref = await viewer.send(
        "message.send", chat_id=cid, client_msg_id=str(uuid.uuid4()), content="me too"
    )
    assert (await viewer.until(lambda f: f.get("ref") == ref))["code"] == "forbidden"

    # I4: the key never travels over the socket
    await owner.until(lambda f: f["type"] == "turn.completed")
    for c in (owner, viewer):
        assert all(KEY not in r for r in c.raw)


async def test_join_mid_stream_gets_snapshot_then_live_without_gaps(
    clients: Any,
    validator: StubValidator,
    runner: TurnRunner,
    providers: ProviderRegistry,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    owner, late = await clients("Alice"), await clients("Bob")
    cid = await ready(owner, validator)
    await add_member(sessionmaker, cid, late, "editor")
    words = [f"w{i} " for i in range(12)]
    providers.fake.script = FakeScript(chunks=words, delay_s=0.08)
    await owner.connect()
    ack = await owner.ack(await owner.send("subscribe", chat_id=cid, after_seq=0))
    await owner.ack(
        await owner.send("message.send", chat_id=cid, client_msg_id=str(uuid.uuid4()), content="go")
    )
    task = asyncio.create_task(runner.run_chat(uuid.UUID(cid)))
    for _ in range(3):
        await owner.until(lambda f: f["type"] == "turn.delta")

    # Bob joins mid-stream, resuming from before the turn started
    await late.connect()
    await late.ack(await late.send("subscribe", chat_id=cid, after_seq=ack["seq"]))
    done = await late.until(lambda f: f["type"] == "turn.completed", timeout=10)
    await task

    snap = next(f for f in late.frames if f["type"] == "turn.snapshot")
    assert any(f["type"] == "turn.started" for f in late.frames)  # replayed
    assert snap["payload"]["chunks"], "joined mid-stream, so some chunks already existed"
    by_idx: dict[int, str] = {}
    for ch in snap["payload"]["chunks"]:
        by_idx[ch["idx"]] = ch["text"]
    for f in late.frames:
        if f["type"] == "turn.delta":
            p = f["payload"]
            assert by_idx.get(p["idx"], p["text"]) == p["text"]  # overlap is identical
            by_idx[p["idx"]] = p["text"]
    assert sorted(by_idx) == list(range(len(by_idx)))  # no gaps
    assert "".join(by_idx[i] for i in sorted(by_idx)) == done["payload"]["message"]["content"]


async def test_chat_deleted_is_pushed(clients: Any, validator: StubValidator) -> None:
    owner = await clients("Alice")
    cid = await ready(owner, validator)
    await owner.connect()
    await owner.ack(await owner.send("subscribe", chat_id=cid, after_seq=0))
    assert (await owner.rest("DELETE", f"/chats/{cid}")).status_code == 204
    await owner.until(lambda f: f["type"] == "chat.deleted")


# ------------------------------------------------------------------ collaboration (M4)


async def join_via_invite(owner: Client, guest: Client, cid: str, role: str = "editor") -> None:
    token = (await owner.rest("POST", f"/chats/{cid}/invites", json={"role": role})).json()["token"]
    r = await guest.rest("POST", f"/invites/{token}/accept")
    assert r.status_code == 200, r.text


async def test_join_is_broadcast_live(clients: Any, validator: StubValidator) -> None:
    owner, bob = await clients("Alice"), await clients("Bob")
    cid = await ready(owner, validator)
    await owner.connect()
    await owner.ack(await owner.send("subscribe", chat_id=cid, after_seq=0))
    await join_via_invite(owner, bob, cid)
    sysmsg = await owner.until(
        lambda f: (
            f["type"] == "message.created" and f["payload"]["message"]["kind"] == "system_event"
        )
    )
    assert sysmsg["payload"]["message"]["content"] == "Bob joined as editor"
    upd = await owner.until(lambda f: f["type"] == "member.updated")
    assert upd["payload"]["member"]["name"] == "Bob"
    assert upd["payload"]["member"]["role"] == "editor"


async def test_removed_member_is_kicked_from_live_socket(
    clients: Any, validator: StubValidator
) -> None:
    owner, bob = await clients("Alice"), await clients("Bob")
    cid = await ready(owner, validator)
    await join_via_invite(owner, bob, cid)
    await bob.connect()
    await bob.ack(await bob.send("subscribe", chat_id=cid, after_seq=0))

    assert (await owner.rest("DELETE", f"/chats/{cid}/members/{bob.id}")).status_code == 204
    kicked = await bob.until(lambda f: f["type"] == "kicked")
    assert kicked["chat_id"] == cid

    # nothing more flows to Bob, and he can't come back in
    await owner.rest("PATCH", f"/chats/{cid}", json={"title": "secret new title"})
    ref = await bob.send("subscribe", chat_id=cid, after_seq=0)
    err = await bob.until(lambda f: f.get("ref") == ref)
    assert err["code"] == "not_found"
    assert not any("secret new title" in r for r in bob.raw)
    ref = await bob.send(
        "message.send", chat_id=cid, client_msg_id=str(uuid.uuid4()), content="let me back"
    )
    assert (await bob.until(lambda f: f.get("ref") == ref))["code"] == "not_found"


async def test_presence_tracks_connections(clients: Any, validator: StubValidator) -> None:
    owner, bob = await clients("Alice"), await clients("Bob")
    cid = await ready(owner, validator)
    await join_via_invite(owner, bob, cid)
    await owner.connect()
    await owner.ack(await owner.send("subscribe", chat_id=cid, after_seq=0))
    await owner.until(
        lambda f: f["type"] == "presence.update" and f["payload"]["online"] == [owner.id]
    )

    await bob.connect()
    await bob.ack(await bob.send("subscribe", chat_id=cid, after_seq=0))
    both = await owner.until(
        lambda f: f["type"] == "presence.update" and len(f["payload"]["online"]) == 2
    )
    assert set(both["payload"]["online"]) == {owner.id, bob.id}
    members = (await owner.rest("GET", f"/chats/{cid}/members")).json()
    assert all(m["online"] for m in members)

    assert bob.ws is not None
    await bob.ws.close()
    after = await owner.until(
        lambda f: f["type"] == "presence.update" and f["payload"]["online"] == [owner.id]
    )
    assert after["payload"]["online"] == [owner.id]


async def test_typing_indicator_and_permissions(clients: Any, validator: StubValidator) -> None:
    owner, bob, vi = await clients("Alice"), await clients("Bob"), await clients("Vi")
    cid = await ready(owner, validator)
    await join_via_invite(owner, bob, cid, "editor")
    await join_via_invite(owner, vi, cid, "viewer")
    for c in (owner, bob, vi):
        await c.connect()

    ref = await bob.send("typing", chat_id=cid)  # not subscribed yet
    assert (await bob.until(lambda f: f.get("ref") == ref))["code"] == "not_subscribed"
    for c in (owner, bob, vi):
        await c.ack(await c.send("subscribe", chat_id=cid, after_seq=0))

    await bob.ack(await bob.send("typing", chat_id=cid))
    t = await owner.until(lambda f: f["type"] == "typing")
    assert t["payload"]["user_id"] == bob.id
    assert t["payload"]["name"] == "Bob"

    ref = await vi.send("typing", chat_id=cid)
    assert (await vi.until(lambda f: f.get("ref") == ref))["code"] == "forbidden"

    # demote Bob live: his socket learns the new role from member.updated
    await owner.rest("PATCH", f"/chats/{cid}/members/{bob.id}", json={"role": "viewer"})
    await bob.until(lambda f: f["type"] == "member.updated")
    ref = await bob.send("typing", chat_id=cid)
    assert (await bob.until(lambda f: f.get("ref") == ref))["code"] == "forbidden"
