"""Secret redaction for logs, errors and anything persisted. See ARCHITECTURE.md §11.4.

Two layers:
1. Pattern-based: key-shaped strings for known providers.
2. Literal-based: exact key values registered for the current task (contextvar),
   so a key with an unknown shape is still scrubbed while a turn is using it.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

REDACTED = "[REDACTED]"

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),  # Google / Gemini
    re.compile(r"sk-ant-[0-9A-Za-z_\-]{16,}"),  # Anthropic
    re.compile(r"sk-or-[0-9A-Za-z_\-]{16,}"),  # OpenRouter
    re.compile(r"sk-(?:proj-|svcacct-)?[0-9A-Za-z_\-]{20,}"),  # OpenAI
    re.compile(r"(?i)([?&](?:key|api_key|apikey)=)[^&\s\"']+"),  # key in query strings
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)(x-api-key:\s*)[^\s\"']+"),
    re.compile(r"(?i)(x-goog-api-key:\s*)[^\s\"']+"),
    re.compile(r"(/invites/)[A-Za-z0-9_\-]{20,}"),  # invite tokens in URL paths
)

_registered: ContextVar[tuple[str, ...]] = ContextVar("_registered_secrets", default=())


@contextmanager
def registered_secret(secret: str) -> Iterator[None]:
    """Register a literal secret so every redact() call in this context scrubs it."""
    token = _registered.set((*_registered.get(), secret)) if secret else None
    try:
        yield
    finally:
        if token is not None:
            _registered.reset(token)


def redact(text: str, *extra: str) -> str:
    """Scrub key-shaped strings plus any registered/extra literal secrets."""
    for secret in (*_registered.get(), *extra):
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    for pat in _PATTERNS:
        if pat.groups:
            text = pat.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pat.sub(REDACTED, text)
    return text


def redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(redact_value(v) for v in value)
    if isinstance(value, BaseException):
        return redact(f"{type(value).__name__}: {value}")
    return value


def structlog_redact_processor(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> dict[str, Any]:
    """structlog processor: scrub every value in the event dict."""
    return {k: redact_value(v) for k, v in event_dict.items()}
