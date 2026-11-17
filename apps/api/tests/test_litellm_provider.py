"""LiteLLMProvider: streaming normalization and error mapping (no network)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import litellm
import pytest
from litellm import exceptions as lx

import app.providers.litellm_provider as lp
from app.providers.base import Finish, Msg, ProviderRequest, TextDelta, Usage
from app.providers.errors import (
    AuthError,
    ContentBlocked,
    ContextTooLong,
    ProviderError,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimitError,
    Unknown,
)

KEY = "AIzaSy" + "K" * 33
REQ = ProviderRequest(
    provider="gemini", model="gemini-x", system="sys", messages=[Msg("user", "hi")], max_tokens=64
)


def chunk(text: str | None = None, finish: str | None = None, usage: Any = None) -> Any:
    choices = (
        []
        if text is None and finish is None
        else [SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish)]
    )
    return SimpleNamespace(choices=choices, usage=usage)


def fake_acompletion(
    chunks: list[Any],
    seen: list[dict[str, Any]],
    raise_at: int | None = None,
    exc: Exception | None = None,
) -> Callable[..., Awaitable[AsyncIterator[Any]]]:
    async def acompletion(**kw: Any) -> AsyncIterator[Any]:
        seen.append(kw)

        async def gen() -> AsyncIterator[Any]:
            for i, c in enumerate(chunks):
                if raise_at is not None and i == raise_at:
                    assert exc is not None
                    raise exc
                yield c

        if raise_at == -1:
            assert exc is not None
            raise exc
        return gen()

    return acompletion


async def collect(p: lp.LiteLLMProvider, req: ProviderRequest = REQ) -> list[Any]:
    return [c async for c in p.stream_chat(req, KEY)]


@pytest.mark.parametrize(
    ("provider", "model", "expected"),
    [
        ("gemini", "gemini-2.5-flash", "gemini/gemini-2.5-flash"),
        ("anthropic", "claude-x", "anthropic/claude-x"),
        ("openai", "gpt-x", "openai/gpt-x"),
        ("openrouter", "meta/llama", "openrouter/meta/llama"),
        ("openai_compatible", "local", "openai/local"),
    ],
)
def test_model_mapping(provider: str, model: str, expected: str) -> None:
    assert lp.litellm_model(provider, model) == expected


async def test_stream_normalization(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(
        litellm,
        "acompletion",
        fake_acompletion(
            [
                chunk("Hel"),
                chunk("lo"),
                chunk(None, "stop"),
                chunk(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3)),
            ],
            seen,
        ),
    )
    out = await collect(lp.LiteLLMProvider())
    assert out == [TextDelta("Hel"), TextDelta("lo"), Usage(12, 3), Finish("stop")]
    kw = seen[0]
    assert kw["model"] == "gemini/gemini-x"
    assert kw["messages"][0] == {"role": "system", "content": "sys"}
    assert kw["stream"] is True
    assert kw["num_retries"] == 0
    assert kw["api_key"] == KEY
    assert "api_base" not in kw


async def test_base_url_passed_for_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion([chunk("x", "stop")], seen))
    req = ProviderRequest(
        provider="openai_compatible",
        model="m",
        system="",
        base_url="https://x.test/v1",
        messages=[Msg("user", "hi")],
        max_tokens=5,
    )
    await collect(lp.LiteLLMProvider(), req)
    assert seen[0]["api_base"] == "https://x.test/v1"


@pytest.mark.parametrize("chunks", [[chunk(None, "content_filter")], [], [chunk(None, "SAFETY")]])
async def test_blocked_or_empty_is_content_blocked(
    monkeypatch: pytest.MonkeyPatch, chunks: list[Any]
) -> None:
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion(chunks, []))
    with pytest.raises(ContentBlocked):
        await collect(lp.LiteLLMProvider())


async def test_partial_then_safety_is_a_blocked_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        fake_acompletion([chunk("partial"), chunk(None, "content_filter")], []),
    )
    out = await collect(lp.LiteLLMProvider())
    assert out[-1] == Finish("blocked")


def _exc(cls: type[Exception], msg: str = f"boom key={KEY}") -> Exception:
    return cls(message=msg, llm_provider="gemini", model="gemini-x")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (lambda: _exc(lx.AuthenticationError), AuthError),
        (lambda: _exc(lx.RateLimitError), RateLimitError),
        (lambda: _exc(lx.RateLimitError, "insufficient_quota for key"), QuotaExceeded),
        (lambda: _exc(lx.ContextWindowExceededError), ContextTooLong),
        (lambda: _exc(lx.ContentPolicyViolationError), ContentBlocked),
        (lambda: _exc(lx.ServiceUnavailableError), ProviderUnavailable),
        (lambda: _exc(lx.InternalServerError), ProviderUnavailable),
        (lambda: _exc(lx.BadRequestError), Unknown),
        (lambda: RuntimeError(f"weird {KEY}"), Unknown),
    ],
)
async def test_error_mapping_redacts_key(
    monkeypatch: pytest.MonkeyPatch, exc: Any, expected: type[ProviderError]
) -> None:
    e = exc()
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion([], [], raise_at=-1, exc=e))
    with pytest.raises(expected) as ei:
        await collect(lp.LiteLLMProvider())
    assert KEY not in str(ei.value)
    assert KEY not in ei.value.message


async def test_error_mid_stream_is_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        litellm,
        "acompletion",
        fake_acompletion(
            [chunk("a"), chunk("b")], [], raise_at=1, exc=_exc(lx.ServiceUnavailableError)
        ),
    )
    got: list[Any] = []

    async def consume() -> None:
        async for c in lp.LiteLLMProvider().stream_chat(REQ, KEY):
            got.append(c)

    with pytest.raises(ProviderUnavailable):
        await consume()
    assert got == [TextDelta("a")]


def test_token_count_and_context_window() -> None:
    p = lp.LiteLLMProvider()
    assert p.count_tokens("gemini/gemini-2.5-flash", [Msg("user", "hello world")], "sys") > 0
    assert lp.context_window("gemini", "gemini-2.5-flash") >= 32_000
    assert lp.context_window("gemini", "no-such-model-xyz") == lp.DEFAULT_CONTEXT_WINDOW


async def test_model_not_found_surfaces_provider_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.providers.errors import ModelUnavailable

    body = (
        'GeminiException - {"error": {"code": 404, "message": "This model models/old-x is no '
        'longer available to new users. Please use models/new-y.", "status": "NOT_FOUND"}} key='
        + KEY
    )
    e = _exc(lx.NotFoundError, body)
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion([], [], raise_at=-1, exc=e))
    with pytest.raises(ModelUnavailable) as ei:
        await collect(lp.LiteLLMProvider())
    assert ei.value.code == "model"
    assert "no longer available to new users" in ei.value.message
    assert "models/new-y" in ei.value.message
    assert KEY not in ei.value.message


def test_provider_message_extraction() -> None:
    assert lp.provider_message('{"message": "a \\"quoted\\" thing"}') == 'a "quoted" thing'
    assert lp.provider_message("no json here") is None


async def test_midstream_fallback_unwraps_original(monkeypatch: pytest.MonkeyPatch) -> None:
    inner = _exc(lx.ServiceUnavailableError, '{"error": {"code": 503, "message": "high demand"}}')
    outer = RuntimeError("MidStreamFallbackError: wrapped")
    outer.original_exception = inner  # type: ignore[attr-defined]
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion([], [], raise_at=-1, exc=outer))
    with pytest.raises(ProviderUnavailable):
        await collect(lp.LiteLLMProvider())


@pytest.mark.parametrize("hang_after", [0, 1])
async def test_stalled_stream_fails_fast(monkeypatch: pytest.MonkeyPatch, hang_after: int) -> None:
    import asyncio

    async def acompletion(**kw: Any) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            if hang_after:
                yield chunk("partial")
            await asyncio.Event().wait()  # provider opens the stream and goes silent
            yield chunk("never")

        return gen()

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr(lp, "FIRST_CHUNK_TIMEOUT_S", 0.2)
    monkeypatch.setattr(lp, "IDLE_TIMEOUT_S", 0.2)
    got: list[Any] = []

    async def consume() -> None:
        async for c in lp.LiteLLMProvider().stream_chat(REQ, KEY):
            got.append(c)

    with pytest.raises(ProviderUnavailable, match="stopped responding"):
        await asyncio.wait_for(consume(), 5)
    assert got == ([TextDelta("partial")] if hang_after else [])
