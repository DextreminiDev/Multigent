"""LiteLLM-backed provider: Gemini, Anthropic, OpenAI, OpenRouter, OpenAI-compatible.

Provider quirks (Gemini `model` role + system_instruction, Anthropic required
max_tokens, usage-only final chunks) are absorbed by LiteLLM; we normalize its
exceptions into our taxonomy (errors.py) with the key redacted, and map safety
blocks to ContentBlocked.
"""

from __future__ import annotations

import asyncio
import os
import re

# Must be set before litellm is imported: no network fetch of the model cost map
# at import time, and no verbose logging (which could include request details).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
os.environ.setdefault("LITELLM_LOG", "ERROR")

from collections.abc import AsyncIterator, Sequence
from typing import Any

import litellm
from litellm import exceptions as lx

from app.providers.base import (
    Chunk,
    Finish,
    FinishReason,
    KeyCheck,
    ModelInfo,
    Msg,
    ProviderRequest,
    TextDelta,
    Usage,
    estimate_tokens,
)
from app.providers.catalog import HttpKeyValidator
from app.providers.errors import (
    AuthError,
    ContentBlocked,
    ContextTooLong,
    ModelUnavailable,
    ProviderError,
    ProviderUnavailable,
    QuotaExceeded,
    RateLimitError,
    Unknown,
)

litellm.telemetry = False
litellm.suppress_debug_info = True
litellm.drop_params = True  # silently drop params a provider doesn't support
litellm.callbacks = []
litellm.success_callback = []
litellm.failure_callback = []

DEFAULT_CONTEXT_WINDOW = 32_000
REQUEST_TIMEOUT_S = 120
FIRST_CHUNK_TIMEOUT_S = 90.0  # max wait for the first token (thinking models start slowly)
IDLE_TIMEOUT_S = 45.0  # max gap between chunks once streaming

_PREFIX = {
    "gemini": "gemini/",
    "anthropic": "anthropic/",
    "openai": "openai/",
    "openrouter": "openrouter/",
    "openai_compatible": "openai/",
}


def litellm_model(provider: str, model: str) -> str:
    try:
        prefix = _PREFIX[provider]
    except KeyError:
        raise Unknown(f"Unsupported provider {provider!r}") from None
    return model if model.startswith(prefix) and provider != "openai_compatible" else prefix + model


def _retry_after(e: Exception) -> float | None:
    resp = getattr(e, "response", None)
    headers = getattr(resp, "headers", None) or {}
    try:
        ra = headers.get("retry-after")
        return float(ra) if ra is not None else None
    except (TypeError, ValueError):
        return None


_MESSAGE = re.compile(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"')


def provider_message(raw: str) -> str | None:
    """Pull the human-readable `message` out of a provider's JSON error body."""
    m = _MESSAGE.search(raw)
    if not m:
        return None
    return m.group(1).encode().decode("unicode_escape", errors="ignore")[:500]


def map_exception(e: Exception, api_key: str) -> ProviderError:
    """Normalize a LiteLLM exception. Order matters: specific subclasses first."""
    msg = str(e)
    if isinstance(e, ProviderError):
        return e
    # LiteLLM wraps some failures (e.g. a 503 on a streaming call) in a fallback
    # error that carries the real cause.
    inner = getattr(e, "original_exception", None)
    if isinstance(inner, Exception) and inner is not e:
        return map_exception(inner, api_key)
    if isinstance(e, lx.ContextWindowExceededError):
        return ContextTooLong("The conversation is too long for this model", secret=api_key)
    if isinstance(e, lx.ContentPolicyViolationError):
        return ContentBlocked("The provider blocked this request (safety filter)", secret=api_key)
    if isinstance(e, lx.AuthenticationError | lx.PermissionDeniedError):
        return AuthError("The provider rejected the API key", secret=api_key)
    if isinstance(e, lx.RateLimitError):
        low = msg.lower()
        if "billing" in low or "insufficient_quota" in low or "credit" in low:
            return QuotaExceeded("The API key is out of quota or credit", secret=api_key)
        return RateLimitError(
            "Provider rate limit hit", retry_after=_retry_after(e), secret=api_key
        )
    if isinstance(e, lx.PaymentRequiredError | lx.BudgetExceededError):
        return QuotaExceeded("The API key is out of quota or credit", secret=api_key)
    if isinstance(e, lx.NotFoundError):
        detail = provider_message(msg) or "The selected model wasn't found for this key"
        return ModelUnavailable(detail, secret=api_key)
    if isinstance(
        e,
        lx.APIConnectionError
        | lx.ServiceUnavailableError
        | lx.InternalServerError
        | lx.BadGatewayError
        | lx.Timeout,
    ):
        detail = provider_message(msg) or "The provider is unavailable right now"
        return ProviderUnavailable(detail, secret=api_key)
    if isinstance(e, lx.BadRequestError):
        detail = provider_message(msg) or msg[:300]
        return Unknown(f"The provider rejected the request: {detail}", secret=api_key)
    return Unknown(f"Unexpected provider error ({type(e).__name__})", secret=api_key)


def _finish(reason: str | None) -> FinishReason:
    if reason in ("stop", "end_turn", "STOP"):
        return "stop"
    if reason in ("length", "max_tokens", "MAX_TOKENS"):
        return "length"
    if reason in ("content_filter", "SAFETY", "safety", "RECITATION", "blocked"):
        return "blocked"
    return "stop" if reason is None else "other"


class LiteLLMProvider:
    def __init__(self, validator: HttpKeyValidator | None = None) -> None:
        self._validator = validator or HttpKeyValidator()

    async def aclose(self) -> None:
        await self._validator.aclose()

    @staticmethod
    def _messages(req: ProviderRequest) -> list[dict[str, str]]:
        out = [{"role": "system", "content": req.system}] if req.system else []
        out.extend({"role": m.role, "content": m.content} for m in req.messages)
        return out

    async def stream_chat(self, req: ProviderRequest, api_key: str) -> AsyncIterator[Chunk]:
        kwargs: dict[str, Any] = {
            "model": litellm_model(req.provider, req.model),
            "messages": self._messages(req),
            "max_tokens": req.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
            "api_key": api_key,
            "timeout": REQUEST_TIMEOUT_S,
            "num_retries": 0,  # retries/backoff are ours (turn engine)
        }
        if req.base_url:
            kwargs["api_base"] = req.base_url

        emitted = False
        finish: FinishReason | None = None
        usage: Usage | None = None
        try:
            stream = await asyncio.wait_for(litellm.acompletion(**kwargs), FIRST_CHUNK_TIMEOUT_S)
            it = stream.__aiter__()
            while True:
                # Stall guard: overloaded providers sometimes open the stream and then
                # send nothing. Fail fast instead of waiting for the full HTTP timeout.
                # (Thinking models can be slow to start, so the first chunk gets longer.)
                wait = IDLE_TIMEOUT_S if emitted else FIRST_CHUNK_TIMEOUT_S
                try:
                    chunk = await asyncio.wait_for(it.__anext__(), wait)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    raise ProviderUnavailable(
                        f"The provider stopped responding (no data for {int(wait)}s)"
                    ) from None
                u = getattr(chunk, "usage", None)
                if u is not None and getattr(u, "prompt_tokens", None) is not None:
                    usage = Usage(int(u.prompt_tokens or 0), int(u.completion_tokens or 0))
                choices = getattr(chunk, "choices", None) or []
                if not choices:
                    continue
                choice = choices[0]
                text = getattr(getattr(choice, "delta", None), "content", None)
                if text:
                    emitted = True
                    yield TextDelta(text)
                if getattr(choice, "finish_reason", None):
                    finish = _finish(choice.finish_reason)
        except Exception as e:
            raise map_exception(e, api_key) from None

        if finish == "blocked" and not emitted:
            raise ContentBlocked("The model's safety filter blocked this response")
        if not emitted and finish is None:
            raise ContentBlocked("The model returned an empty response")
        if usage is not None:
            yield usage
        yield Finish(finish or "stop")

    async def validate_key(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> KeyCheck:
        return await self._validator.validate_key(provider, api_key, base_url)

    async def list_models(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> list[ModelInfo]:
        return await self._validator.list_models(provider, api_key, base_url)

    def count_tokens(self, model: str, messages: Sequence[Msg], system: str = "") -> int:
        payload = ([{"role": "system", "content": system}] if system else []) + [
            {"role": m.role, "content": m.content} for m in messages
        ]
        try:
            return int(litellm.token_counter(model=model, messages=payload))
        except Exception:
            return estimate_tokens(system) + sum(estimate_tokens(m.content) for m in messages)


def context_window(provider: str, model: str) -> int:
    try:
        info = litellm.get_model_info(litellm_model(provider, model))
        return int(info.get("max_input_tokens") or DEFAULT_CONTEXT_WINDOW)
    except Exception:
        return DEFAULT_CONTEXT_WINDOW
