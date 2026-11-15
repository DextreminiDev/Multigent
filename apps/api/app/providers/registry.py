"""Pick the LLMProvider for a chat's provider id. `fake` = FakeProvider (tests +
keyless demo mode); everything else goes through LiteLLM."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.providers.base import LLMProvider
from app.providers.fake import FakeProvider


@dataclass
class ProviderRegistry:
    real: LLMProvider | None = None
    fake: FakeProvider = field(default_factory=FakeProvider)

    def get(self, provider: str) -> LLMProvider:
        if provider == "fake":
            return self.fake
        if self.real is None:
            from app.providers.litellm_provider import LiteLLMProvider  # heavy import, lazy

            self.real = LiteLLMProvider()
        return self.real
