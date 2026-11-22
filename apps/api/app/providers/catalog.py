"""Provider catalog + key validation / model listing over plain HTTPS.

Validation lists models, which is free on every supported provider (no tokens
consumed, important for free-tier Gemini keys). Keys go in headers, never in
query strings. Responses and errors are never logged; every error string is
built by us (no provider text echoed) or passed through redaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from app.providers.base import KeyCheck, ModelInfo
from app.providers.errors import (
    AuthError,
    ProviderError,
    ProviderUnavailable,
    RateLimitError,
    Unknown,
)
from app.providers.fake import FakeProvider

ProviderId = Literal["gemini", "anthropic", "openai", "openrouter", "openai_compatible", "fake"]
REAL_PROVIDERS: tuple[ProviderId, ...] = (
    "gemini", "anthropic", "openai", "openrouter", "openai_compatible",
)  # fmt: skip


@dataclass(frozen=True)
class ProviderMeta:
    id: ProviderId
    label: str
    needs_base_url: bool = False
    key_help_url: str | None = None


PROVIDERS: dict[str, ProviderMeta] = {
    "gemini": ProviderMeta("gemini", "Google Gemini", key_help_url="https://aistudio.google.com/app/apikey"),
    "anthropic": ProviderMeta("anthropic", "Anthropic", key_help_url="https://console.anthropic.com/settings/keys"),
    "openai": ProviderMeta("openai", "OpenAI", key_help_url="https://platform.openai.com/api-keys"),
    "openrouter": ProviderMeta("openrouter", "OpenRouter", key_help_url="https://openrouter.ai/keys"),
    "openai_compatible": ProviderMeta("openai_compatible", "OpenAI-compatible", needs_base_url=True),
    "fake": ProviderMeta("fake", "Demo (no key)"),
}  # fmt: skip

_OPENAI_NON_CHAT = ("embedding", "whisper", "tts", "dall-e", "moderation", "davinci", "babbage",
                    "audio", "realtime", "transcribe", "image", "search")  # fmt: skip


# Gemini models that list generateContent but aren't text chat models.
_GEMINI_NON_CHAT = ("tts", "image", "lyria", "banana", "research", "customtools", "embedding",
                    "aqa", "robotics", "computer-use", "veo", "imagen", "audio", "live")  # fmt: skip


class KeyValidator(Protocol):
    async def list_models(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> list[ModelInfo]: ...

    async def validate_key(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> KeyCheck: ...


def _raise_for(resp: httpx.Response, api_key: str) -> None:
    s = resp.status_code
    if s < 400:
        return
    body = resp.text[:2000]
    if s in (401, 403) or (s == 400 and ("API_KEY_INVALID" in body or "API key" in body)):
        raise AuthError("The provider rejected this API key", secret=api_key)
    if s == 429:
        ra = resp.headers.get("retry-after")
        raise RateLimitError(
            "Provider rate limit hit", retry_after=float(ra) if ra and ra.isdigit() else None
        )
    if s >= 500:
        raise ProviderUnavailable(f"Provider returned HTTP {s}")
    raise Unknown(f"Provider returned HTTP {s}")


class HttpKeyValidator:
    def __init__(self, client: httpx.AsyncClient | None = None, timeout_s: float = 10.0) -> None:
        self._client = client or httpx.AsyncClient(timeout=timeout_s, follow_redirects=False)
        self._fake = FakeProvider()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(
        self, url: str, api_key: str, headers: dict[str, str], params: dict[str, Any] | None = None
    ) -> Any:
        try:
            resp = await self._client.get(url, headers=headers, params=params)
        except httpx.HTTPError:
            raise ProviderUnavailable("Could not reach the provider") from None
        _raise_for(resp, api_key)
        try:
            return resp.json()
        except ValueError:
            raise Unknown("Provider returned an unexpected response") from None

    async def list_models(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> list[ModelInfo]:
        bearer = {"Authorization": f"Bearer {api_key}"}
        if provider == "fake":
            return await self._fake.list_models(provider, api_key)
        if provider == "gemini":
            data = await self._get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                api_key,
                {"x-goog-api-key": api_key},
                {"pageSize": 1000},
            )
            models = [
                ModelInfo(
                    id=m["name"].removeprefix("models/"),
                    context_window=m.get("inputTokenLimit"),
                    max_output_tokens=m.get("outputTokenLimit"),
                    extra={"label": m.get("displayName", "")},
                )
                for m in data.get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])
                and not any(t in m["name"] for t in _GEMINI_NON_CHAT)
            ]
            # "-latest" aliases first: they always point at a model the key can use
            return sorted(models, key=lambda m: (not m.id.endswith("-latest"),))
        if provider == "anthropic":
            data = await self._get(
                "https://api.anthropic.com/v1/models",
                api_key,
                {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                {"limit": 1000},
            )
            return [
                ModelInfo(
                    id=m["id"],
                    context_window=m.get("max_input_tokens"),
                    max_output_tokens=m.get("max_tokens"),
                    extra={"label": m.get("display_name", "")},
                )
                for m in data.get("data", [])
            ]
        if provider == "openai":
            data = await self._get("https://api.openai.com/v1/models", api_key, bearer)
            return sorted(
                (
                    ModelInfo(id=m["id"])
                    for m in data.get("data", [])
                    if not any(t in m["id"] for t in _OPENAI_NON_CHAT)
                ),
                key=lambda m: m.id,
            )
        if provider == "openrouter":
            # /models is public, so check the key explicitly first.
            await self._get("https://openrouter.ai/api/v1/key", api_key, bearer)
            data = await self._get("https://openrouter.ai/api/v1/models", api_key, bearer)
            return [
                ModelInfo(
                    id=m["id"],
                    context_window=m.get("context_length"),
                    max_output_tokens=(m.get("top_provider") or {}).get("max_completion_tokens"),
                    extra={"label": m.get("name", "")},
                )
                for m in data.get("data", [])
            ]
        if provider == "openai_compatible":
            if not base_url:
                raise Unknown("A base URL is required")
            data = await self._get(f"{base_url}/models", api_key, bearer)
            return [ModelInfo(id=m["id"]) for m in data.get("data", []) if "id" in m]
        raise Unknown("Unsupported provider")

    async def validate_key(
        self, provider: str, api_key: str, base_url: str | None = None
    ) -> KeyCheck:
        if provider == "fake":
            return await self._fake.validate_key(provider, api_key)
        try:
            await self.list_models(provider, api_key, base_url)
        except RateLimitError:
            return KeyCheck(ok=True, message="Key accepted (provider is rate limiting)")
        except ProviderError as e:
            return KeyCheck(ok=False, error_code=e.code, message=e.message)
        return KeyCheck(ok=True)
