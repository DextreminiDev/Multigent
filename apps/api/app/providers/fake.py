"""Deterministic FakeProvider for tests and keyless demo mode (§9.1).

Scriptable behaviors:
- `chunks`: explicit list of text chunks; otherwise an echo reply is generated
- `delay_s`: sleep before each chunk
- `fail_at_chunk` + `fail_with`: raise after emitting k chunks
- `rate_limit_first_n`: raise RateLimitError on the first n calls
- `blocked`: emit nothing, raise ContentBlocked
- `hang`: emit `hang_after` chunks then block forever (until cancelled)
- `valid_keys`: keys accepted by validate_key (None = any non-empty key)
"""

from __future__ import annotations

import asyncio
import html
import re
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

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
from app.providers.errors import AuthError, ContentBlocked, ProviderError, RateLimitError

_TAG = re.compile(r'<message from="([^"]*)"[^>]*>\n(.*?)\n</message>', re.S)


@dataclass
class FakeScript:
    chunks: list[str] | None = None
    delay_s: float = 0.0
    fail_at_chunk: int | None = None
    fail_with: ProviderError | None = None
    rate_limit_first_n: int = 0
    fail_first_n: int = 0  # raise `fail_first_with` on the first n calls
    fail_first_with: ProviderError | None = None
    retry_after: float | None = None
    blocked: bool = False
    hang: bool = False
    hang_after: int = 0
    valid_keys: set[str] | None = None


@dataclass
class FakeProvider:
    script: FakeScript = field(default_factory=FakeScript)
    calls: list[ProviderRequest] = field(default_factory=list)

    def _check_key(self, api_key: str) -> None:
        if not api_key or (
            self.script.valid_keys is not None and api_key not in self.script.valid_keys
        ):
            raise AuthError("invalid API key", secret=api_key)

    @staticmethod
    def echo_reply(req: ProviderRequest) -> list[str]:
        """Deterministic demo reply. If the last user turn carries speaker tags, answer
        each person in their own section (shows batching without a real model)."""
        last = req.messages[-1].content if req.messages else ""
        tagged = _TAG.findall(last)
        if tagged:
            parts = [f"*Demo model ({req.model}): I echo instead of thinking.*\n"]
            for name, body in tagged:
                text = html.unescape(body).strip()
                quoted = "\n".join(f"> {line}" for line in text.splitlines() or [""])
                parts.append(f"**{html.unescape(name)}**, you said:\n\n{quoted}\n")
            reply = "\n".join(parts)
        else:
            reply = f"[fake:{req.model}] You said: {last}"
        words = reply.split(" ")
        return [w + (" " if i < len(words) - 1 else "") for i, w in enumerate(words)]

    async def stream_chat(self, req: ProviderRequest, api_key: str) -> AsyncIterator[Chunk]:
        s = self.script
        self.calls.append(req)
        self._check_key(api_key)
        if len(self.calls) <= s.fail_first_n:
            raise s.fail_first_with or ProviderError("injected failure")
        if len(self.calls) <= s.rate_limit_first_n:
            raise RateLimitError("429 Too Many Requests", retry_after=s.retry_after)
        if s.blocked:
            raise ContentBlocked("response blocked by safety filters")

        chunks = s.chunks if s.chunks is not None else self.echo_reply(req)
        out = ""
        finish: FinishReason = "stop"
        for i, text in enumerate(chunks):
            if estimate_tokens(out + text) > req.max_tokens:  # like a real model
                finish = "length"
                break
            if s.hang and i >= s.hang_after:
                await asyncio.Event().wait()  # until cancelled
            if s.fail_at_chunk is not None and i >= s.fail_at_chunk:
                raise s.fail_with or ProviderError("injected failure")
            if s.delay_s:
                await asyncio.sleep(s.delay_s)
            out += text
            yield TextDelta(text)
        if s.hang:
            await asyncio.Event().wait()

        yield Usage(
            tokens_in=self.count_tokens(req.model, req.messages, req.system),
            tokens_out=estimate_tokens(out),
        )
        yield Finish(finish)

    async def validate_key(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> KeyCheck:
        try:
            self._check_key(api_key)
        except AuthError as e:
            return KeyCheck(ok=False, error_code=e.code, message=e.message)
        return KeyCheck(ok=True)

    async def list_models(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> list[ModelInfo]:
        return [
            ModelInfo(id="fake-small", context_window=8_000),
            ModelInfo(id="fake-large", context_window=128_000),
        ]

    def count_tokens(self, model: str, messages: Sequence[Msg], system: str = "") -> int:
        return estimate_tokens(system) + sum(estimate_tokens(m.content) for m in messages)
