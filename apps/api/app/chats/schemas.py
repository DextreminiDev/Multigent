"""Request/response models for chats and keys. No model here ever carries a
plaintext key outward: KeyIn is input-only and uses SecretStr (masked repr)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.providers.catalog import ProviderId

MAX_TITLE = 120
MAX_INSTRUCTIONS = 8000


class UserRef(BaseModel):
    id: uuid.UUID
    name: str
    avatar_color: str


class KeyView(BaseModel):
    provider: str
    status: Literal["ok", "invalid", "unvalidated"]
    last4: str | None = None  # owner only
    validated_at: dt.datetime | None = None


class ChatView(BaseModel):
    id: uuid.UUID
    title: str
    role: Literal["owner", "editor", "viewer"]
    owner: UserRef
    status: str
    provider: str | None
    model: str | None
    max_output_tokens: int
    member_count: int
    key: KeyView | None
    last_seq: int
    created_at: dt.datetime
    updated_at: dt.datetime
    # owner-only (null for collaborators)
    custom_instructions: str | None = None
    daily_token_cap: int | None = None
    base_url: str | None = None
    rpm_limit: int | None = None  # owner-only; None = provider default
    # model limits (public): what the meter and composer reason about
    context_window: int | None = None
    max_output_limit: int | None = None
    limits_source: Literal["provider", "litellm", "default"] | None = None
    # "always": every untagged message gets a reply; "tags": only @ai messages do
    reply_mode: Literal["always", "tags"] = "always"


class ChatCreate(BaseModel):
    title: str | None = Field(default=None, max_length=MAX_TITLE)


class ChatPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=MAX_TITLE)
    custom_instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS)
    model: str | None = Field(default=None, min_length=1, max_length=200)
    max_output_tokens: int | None = Field(default=None, ge=16, le=65536)
    daily_token_cap: int | None = Field(default=None, ge=1)
    rpm_limit: int | None = Field(default=None, ge=1, le=600)
    reply_mode: Literal["always", "tags"] | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, v: str | None) -> str | None:
        if v is None:
            return v
        v = " ".join(v.split())
        if not v:
            raise ValueError("Title cannot be blank")
        return v


class KeyIn(BaseModel):
    provider: ProviderId
    api_key: SecretStr
    base_url: str | None = Field(default=None, max_length=500)

    @field_validator("api_key")
    @classmethod
    def _key_shape(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value().strip()
        # Messages are fixed strings: they must never include the value.
        if not 8 <= len(raw) <= 512:
            raise ValueError("API key length looks wrong")
        if any(ch.isspace() for ch in raw) or not raw.isprintable():
            raise ValueError("API key contains invalid characters")
        return SecretStr(raw)


class MessageView(BaseModel):
    id: uuid.UUID
    seq: int
    anchor_seq: int
    kind: Literal["human", "ai", "system_event"]
    author_id: uuid.UUID | None
    author_name: str | None
    author_color: str | None = None
    content: str
    status: str
    # human messages: "reply" (asks the AI), "context" (@skip / tags mode), "off" (@off)
    intent: Literal["reply", "context", "off"] | None = None
    turn_id: uuid.UUID | None
    created_at: dt.datetime
    client_msg_id: uuid.UUID | None = None


class ModelList(BaseModel):
    models: list[ModelOption]


class ModelOption(BaseModel):
    id: str
    label: str | None = None
    context_window: int | None = None


class ProviderView(BaseModel):
    id: str
    label: str
    needs_base_url: bool
    key_help_url: str | None


ModelList.model_rebuild()
