from __future__ import annotations

import asyncio

import pytest

from app.providers.base import Finish, LLMProvider, Msg, ProviderRequest, TextDelta, Usage
from app.providers.errors import AuthError, ContentBlocked, ProviderUnavailable, RateLimitError
from app.providers.fake import FakeProvider, FakeScript

REQ = ProviderRequest(
    provider="fake",
    model="fake-small",
    system="sys",
    messages=[Msg("user", "hello there")],
    max_tokens=100,
)


async def collect(p: FakeProvider, key: str = "k") -> list[object]:
    return [c async for c in p.stream_chat(REQ, key)]


def text_of(chunks: list[object]) -> str:
    return "".join(c.text for c in chunks if isinstance(c, TextDelta))


def test_satisfies_protocol() -> None:
    assert isinstance(FakeProvider(), LLMProvider)


async def test_echo_stream_ends_with_usage_and_finish() -> None:
    chunks = await collect(FakeProvider())
    assert text_of(chunks) == "[fake:fake-small] You said: hello there"
    assert isinstance(chunks[-2], Usage)
    assert chunks[-1] == Finish("stop")


async def test_scripted_chunks() -> None:
    chunks = await collect(FakeProvider(FakeScript(chunks=["a", "b", "c"])))
    assert text_of(chunks) == "abc"


async def test_fail_at_chunk_after_partial_output() -> None:
    p = FakeProvider(
        FakeScript(chunks=["a", "b", "c"], fail_at_chunk=2, fail_with=ProviderUnavailable("503"))
    )
    seen: list[str] = []

    async def consume() -> None:
        async for c in p.stream_chat(REQ, "k"):
            if isinstance(c, TextDelta):
                seen.append(c.text)

    with pytest.raises(ProviderUnavailable):
        await consume()
    assert seen == ["a", "b"]


async def test_rate_limit_first_n_calls() -> None:
    p = FakeProvider(FakeScript(rate_limit_first_n=2, retry_after=1.0))
    for _ in range(2):
        with pytest.raises(RateLimitError) as ei:
            await collect(p)
        assert ei.value.retry_after == 1.0
    assert text_of(await collect(p))
    assert len(p.calls) == 3


async def test_blocked() -> None:
    with pytest.raises(ContentBlocked):
        await collect(FakeProvider(FakeScript(blocked=True)))


async def test_hang_is_cancellable() -> None:
    p = FakeProvider(FakeScript(chunks=["x", "y"], hang=True, hang_after=1))
    got: list[str] = []

    async def run() -> None:
        async for c in p.stream_chat(REQ, "k"):
            if isinstance(c, TextDelta):
                got.append(c.text)

    task = asyncio.create_task(run())
    await asyncio.sleep(0.05)
    assert got == ["x"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_key_validation_and_auth_error_redacts_key() -> None:
    p = FakeProvider(FakeScript(valid_keys={"good-key-1234"}))
    assert (await p.validate_key("fake", "good-key-1234")).ok
    bad = await p.validate_key("fake", "bad-key-5678")
    assert not bad.ok
    assert bad.error_code == "auth"
    with pytest.raises(AuthError) as ei:
        await collect(p, "bad-key-5678")
    assert "bad-key-5678" not in str(ei.value)


async def test_list_models_and_count_tokens() -> None:
    p = FakeProvider()
    assert [m.id for m in await p.list_models("fake", "k")] == ["fake-small", "fake-large"]
    assert p.count_tokens("m", [Msg("user", "abcd" * 10)]) == 10


async def test_respects_max_tokens_like_a_real_model() -> None:
    req = ProviderRequest(
        provider="fake", model="m", system="", max_tokens=5, messages=[Msg("user", "word " * 200)]
    )
    chunks = [c async for c in FakeProvider().stream_chat(req, "k")]
    text = "".join(c.text for c in chunks if isinstance(c, TextDelta))
    assert len(text) // 4 <= 5
    assert chunks[-1] == Finish("length")
