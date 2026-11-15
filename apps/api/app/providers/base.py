"""Provider-agnostic request/response types and the LLMProvider protocol (§9.1)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class Msg:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    """Everything a provider call needs except the key, which is passed
    separately as a function argument so it's never stored on an object (§11.3)."""

    provider: str
    model: str
    system: str
    messages: Sequence[Msg]
    max_tokens: int
    base_url: str | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class Usage:
    tokens_in: int
    tokens_out: int


FinishReason = Literal["stop", "length", "blocked", "other"]


@dataclass(frozen=True, slots=True)
class Finish:
    reason: FinishReason


Chunk = TextDelta | Usage | Finish


@dataclass(frozen=True, slots=True)
class KeyCheck:
    ok: bool
    error_code: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str
    context_window: int | None = None
    extra: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class LLMProvider(Protocol):
    def stream_chat(self, req: ProviderRequest, api_key: str) -> AsyncIterator[Chunk]: ...

    async def validate_key(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> KeyCheck: ...

    async def list_models(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> list[ModelInfo]: ...

    def count_tokens(self, model: str, messages: Sequence[Msg], system: str = "") -> int: ...


def estimate_tokens(text: str) -> int:
    """len/4 heuristic, used as a fallback when a real tokenizer isn't available."""
    return (len(text) + 3) // 4
