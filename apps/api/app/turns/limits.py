"""Per-model limits (ARCHITECTURE.md §10.1).

Limits differ per *model*, not per provider, and no single source knows them all.
Resolution order, best first:
  1. the provider's own model list (Gemini input/outputTokenLimit, Anthropic
     max_input_tokens/max_tokens, OpenRouter context_length/max_completion_tokens)
  2. LiteLLM's bundled model map
  3. a conservative default (flagged in the UI as a guess)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.providers.base import ModelInfo

DEFAULT_CONTEXT_WINDOW = 32_000
# Proactive requests/minute when the owner hasn't set one. Free Gemini keys allow
# only a handful of RPM, so queueing beats collecting 429s. Others: no ceiling.
DEFAULT_RPM: dict[str, int] = {"gemini": 10}

Source = Literal["provider", "litellm", "default"]


@dataclass(frozen=True)
class ModelLimits:
    context_window: int
    max_output: int | None
    source: Source


def _litellm(provider: str, model: str) -> tuple[int | None, int | None]:
    if provider == "fake":
        return None, None
    try:
        import litellm  # heavy; only needed here

        from app.providers.litellm_provider import litellm_model

        info = litellm.get_model_info(litellm_model(provider, model))
        win = info.get("max_input_tokens") or info.get("max_tokens")
        out = info.get("max_output_tokens")
        return (int(win) if win else None), (int(out) if out else None)
    except Exception:
        return None, None


def resolve_limits(provider: str, model: str, listed: ModelInfo | None = None) -> ModelLimits:
    lw, lo = _litellm(provider, model)
    if listed is not None and listed.context_window:
        return ModelLimits(listed.context_window, listed.max_output_tokens or lo, "provider")
    if lw:
        return ModelLimits(lw, lo, "litellm")
    return ModelLimits(DEFAULT_CONTEXT_WINDOW, None, "default")


def effective_rpm(provider: str | None, rpm_limit: int | None) -> int | None:
    if rpm_limit is not None:
        return rpm_limit
    return DEFAULT_RPM.get(provider or "")


def effective_max_output(requested: int, max_output_limit: int | None) -> int:
    return min(requested, max_output_limit) if max_output_limit else requested
