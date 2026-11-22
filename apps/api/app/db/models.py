"""SQLAlchemy models. Mirrors docs/ARCHITECTURE.md §4 exactly; change both together."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.ids import uuid7

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid7)


def _ts(nullable: bool = False) -> Mapped[Any]:
    if nullable:
        return mapped_column(DateTime(timezone=True), nullable=True)
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Mapped[Any]:
    return mapped_column(
        UUID(as_uuid=True), ForeignKey(target, ondelete=ondelete), nullable=nullable
    )


# ---------------------------------------------------------------- users / auth


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    avatar_color: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = _ts()
    deleted_at: Mapped[dt.datetime | None] = _ts(nullable=True)


class AuthIdentity(Base):
    __tablename__ = "auth_identities"
    __table_args__ = (UniqueConstraint("provider", "provider_user_id"),)
    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    provider_user_id: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------- chats


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (
        CheckConstraint("status IN ('active','paused_key','paused_cap','archived')", name="status"),
    )
    id: Mapped[uuid.UUID] = _pk()
    owner_id: Mapped[uuid.UUID] = _fk("users.id")
    title: Mapped[str] = mapped_column(Text, nullable=False, server_default="New chat")
    custom_instructions: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text)
    max_output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2048")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    daily_token_cap: Mapped[int | None] = mapped_column(Integer)
    # Model limits resolved when the model is chosen (§10.1): provider list > LiteLLM
    # map > default. `limits_source` says which, so the UI can flag guesses.
    context_window: Mapped[int | None] = mapped_column(Integer)
    max_output_limit: Mapped[int | None] = mapped_column(Integer)
    limits_source: Mapped[str | None] = mapped_column(Text)
    # Proactive requests/minute ceiling for the owner's key (None = provider default).
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[dt.datetime] = _ts()
    updated_at: Mapped[dt.datetime] = _ts()


class ChatKey(Base):
    """0 or 1 row per chat. Plaintext is never stored; see KeyVault (§11.3)."""

    __tablename__ = "chat_keys"
    __table_args__ = (CheckConstraint("status IN ('ok','invalid','unvalidated')", name="status"),)
    chat_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_id: Mapped[str] = mapped_column(Text, nullable=False)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    last4: Mapped[str] = mapped_column(Text, nullable=False)
    validated_at: Mapped[dt.datetime | None] = _ts(nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="unvalidated")


class ChatMember(Base):
    __tablename__ = "chat_members"
    __table_args__ = (
        PrimaryKeyConstraint("chat_id", "user_id"),
        CheckConstraint("role IN ('owner','editor','viewer')", name="role"),
    )
    chat_id: Mapped[uuid.UUID] = _fk("chats.id")
    user_id: Mapped[uuid.UUID] = _fk("users.id")
    role: Mapped[str] = mapped_column(Text, nullable=False)
    member_token_cap: Mapped[int | None] = mapped_column(Integer)
    joined_at: Mapped[dt.datetime] = _ts()
    removed_at: Mapped[dt.datetime | None] = _ts(nullable=True)


class ChatInvite(Base):
    __tablename__ = "chat_invites"
    __table_args__ = (CheckConstraint("role IN ('editor','viewer')", name="role"),)
    id: Mapped[uuid.UUID] = _pk()
    chat_id: Mapped[uuid.UUID] = _fk("chats.id")
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[dt.datetime | None] = _ts(nullable=True)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    uses: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    revoked_at: Mapped[dt.datetime | None] = _ts(nullable=True)
    created_by: Mapped[uuid.UUID] = _fk("users.id")
    created_at: Mapped[dt.datetime] = _ts()


# ---------------------------------------------------------------- events / messages / turns


class ChatEvent(Base):
    """Append-only replay log; one shared seq counter per chat (§5)."""

    __tablename__ = "chat_events"
    __table_args__ = (PrimaryKeyConstraint("chat_id", "seq"),)
    chat_id: Mapped[uuid.UUID] = _fk("chats.id")
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = _ts()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("chat_id", "client_msg_id"),
        UniqueConstraint("chat_id", "seq"),
        CheckConstraint("kind IN ('human','ai','system_event')", name="kind"),
        CheckConstraint(
            "status IN ('pending','batched','done','failed','deleted','superseded')",
            name="status",
        ),
        Index(
            "messages_pending_idx",
            "chat_id",
            "seq",
            postgresql_where=text("status = 'pending' AND kind = 'human'"),
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    chat_id: Mapped[uuid.UUID] = _fk("chats.id")
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    author_name_snapshot: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    client_msg_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(Text, nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("turns.id", ondelete="SET NULL", use_alter=True),
    )
    created_at: Mapped[dt.datetime] = _ts()


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (
        CheckConstraint("status IN ('running','completed','failed','cancelled')", name="status"),
        CheckConstraint("trigger IN ('batch','retry','regenerate')", name="trigger"),
        Index(
            "one_running_turn_per_chat",
            "chat_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )
    id: Mapped[uuid.UUID] = _pk()
    chat_id: Mapped[uuid.UUID] = _fk("chats.id")
    status: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False, server_default="batch")
    lock_token: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # seq of this turn's `turn.started` event: the logical position of the turn. The
    # AI reply is inserted at completion (higher seq), but belongs right after its
    # batch; messages that arrived mid-turn come after it (prompt + UI ordering).
    started_seq: Mapped[int | None] = mapped_column(BigInteger)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[dt.datetime] = _ts()
    finished_at: Mapped[dt.datetime | None] = _ts(nullable=True)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(Text)
    error_detail: Mapped[str | None] = mapped_column(Text)  # always redacted
    partial_text: Mapped[str | None] = mapped_column(Text)
    ai_message_id: Mapped[uuid.UUID | None] = _fk("messages.id", nullable=True, ondelete="SET NULL")


class TurnMessage(Base):
    __tablename__ = "turn_messages"
    __table_args__ = (PrimaryKeyConstraint("turn_id", "message_id"),)
    turn_id: Mapped[uuid.UUID] = _fk("turns.id")
    message_id: Mapped[uuid.UUID] = _fk("messages.id")


class ChatSummary(Base):
    """Each summary subsumes all earlier ones; only the latest is used."""

    __tablename__ = "chat_summaries"
    id: Mapped[uuid.UUID] = _pk()
    chat_id: Mapped[uuid.UUID] = _fk("chats.id")
    covers_through_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[dt.datetime] = _ts()


class UsageEvent(Base):
    __tablename__ = "usage_events"
    __table_args__ = (
        CheckConstraint("kind IN ('turn','summary')", name="kind"),
        Index("usage_events_chat_day_idx", "chat_id", "day"),
    )
    id: Mapped[uuid.UUID] = _pk()
    chat_id: Mapped[uuid.UUID] = _fk("chats.id")
    user_id: Mapped[uuid.UUID | None] = _fk("users.id", nullable=True, ondelete="SET NULL")
    turn_id: Mapped[uuid.UUID | None] = _fk("turns.id", nullable=True, ondelete="SET NULL")
    kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="turn")
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False)
    est_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    day: Mapped[dt.date] = mapped_column(Date, nullable=False)
    created_at: Mapped[dt.datetime] = _ts()
