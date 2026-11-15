"""Normalized provider error taxonomy (ARCHITECTURE.md §9.2).

Messages are always passed through `redact()` at construction, because some
providers echo the request URL (Gemini: `?key=...`) or headers in their errors.
"""

from __future__ import annotations

from app.security.redaction import redact


class ProviderError(Exception):
    code: str = "unknown"
    retryable: bool = False

    def __init__(self, message: str = "", *, secret: str | None = None) -> None:
        clean = redact(message, *([secret] if secret else []))
        super().__init__(clean)
        self.message = clean


class AuthError(ProviderError):
    code = "auth"


class RateLimitError(ProviderError):
    code = "rate_limit"
    retryable = True

    def __init__(
        self, message: str = "", *, retry_after: float | None = None, secret: str | None = None
    ) -> None:
        super().__init__(message, secret=secret)
        self.retry_after = retry_after


class QuotaExceeded(ProviderError):
    code = "quota"


class ContextTooLong(ProviderError):
    code = "context"


class ProviderUnavailable(ProviderError):
    code = "unavailable"
    retryable = True


class ContentBlocked(ProviderError):
    code = "blocked"


class Unknown(ProviderError):
    code = "unknown"


class KeyUnavailable(ProviderError):
    """The stored key couldn't be decrypted (or the vault isn't configured)."""

    code = "key"


class ModelUnavailable(ProviderError):
    """The model id doesn't exist or isn't available to this key (retired, gated...)."""

    code = "model"
