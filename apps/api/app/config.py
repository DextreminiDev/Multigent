"""Application settings (env-driven). See docs/ARCHITECTURE.md §15."""

from __future__ import annotations

import base64
import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Public dev-only master key used by docker-compose defaults. Never valid in production.
DEV_MASTER_KEY = b"dev-only-key-do-not-use-in-prod!"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["development", "test", "production"] = "development"

    database_url: str = "postgresql+asyncpg://multigent:multigent@localhost:15432/multigent"
    redis_url: str = "redis://localhost:16379/0"

    # KeyVault master keys: JSON object {key_id: base64(32 bytes)}.
    key_encryption_keys: str = Field(default="{}", repr=False)
    key_encryption_active_id: str = ""

    session_secret: str = Field(default="", repr=False)
    # Comma-separated in env (NoDecode stops pydantic-settings JSON-parsing it first).
    allowed_origins: Annotated[list[str], NoDecode] = ["http://localhost:3000"]
    cookie_domain: str | None = None
    # None = Secure only in production. Plain-http test clients can't send Secure cookies.
    cookie_secure: bool | None = None
    session_ttl_s: int = 60 * 60 * 24 * 30  # sliding

    # Providers allowed when saving a key. "fake" (FakeProvider demo mode) is never
    # allowed in production. "openai_compatible" requires a public https base_url.
    allow_private_base_urls: bool = False  # SSRF guard for openai_compatible

    debounce_ms: int = 800
    message_rate_per_min: int = 6  # AI-calling messages, per user per chat (token bucket)
    chat_rate_per_min: int = 30  # messages that don't call the AI (@skip/@off/tags mode)
    worker_max_concurrent_turns: int = 20
    shutdown_grace_s: int = 30

    dev_login: bool = False

    # OAuth (ARCHITECTURE.md §11.1). A provider is enabled when both values are set.
    public_api_url: str = "http://localhost:8000"  # builds the OAuth redirect_uri
    web_url: str = "http://localhost:3000"  # where users land after signing in
    oauth_google_client_id: str = ""
    oauth_google_client_secret: str = Field(default="", repr=False)
    oauth_github_client_id: str = ""
    oauth_github_client_secret: str = Field(default="", repr=False)
    log_level: str = "INFO"

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _production_guards(self) -> Settings:
        if self.env == "production":
            if self.dev_login:
                raise ValueError("DEV_LOGIN must not be enabled when ENV=production")
            if len(self.session_secret) < 32:
                raise ValueError("SESSION_SECRET must be at least 32 chars in production")
            keys = self.encryption_keys()
            if not keys:
                raise ValueError("KEY_ENCRYPTION_KEYS must be set in production")
            if DEV_MASTER_KEY in keys.values():
                raise ValueError("KEY_ENCRYPTION_KEYS contains the public dev key")
        return self

    @property
    def secure_cookies(self) -> bool:
        return self.env == "production" if self.cookie_secure is None else self.cookie_secure

    def encryption_keys(self) -> dict[str, bytes]:
        """Decode master keys. Each must be exactly 32 bytes (AES-256)."""
        raw = json.loads(self.key_encryption_keys or "{}")
        if not isinstance(raw, dict):
            raise ValueError("KEY_ENCRYPTION_KEYS must be a JSON object")
        keys: dict[str, bytes] = {}
        for key_id, b64 in raw.items():
            material = base64.b64decode(b64)
            if len(material) != 32:
                raise ValueError(f"encryption key {key_id!r} must be 32 bytes")
            keys[str(key_id)] = material
        if keys and self.key_encryption_active_id not in keys:
            raise ValueError("KEY_ENCRYPTION_ACTIVE_ID must name a key in KEY_ENCRYPTION_KEYS")
        return keys


@lru_cache
def get_settings() -> Settings:
    return Settings()
