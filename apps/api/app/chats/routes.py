"""Chat CRUD, message history, and the write-only key slot."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from app.chats.permissions import Action
from app.chats.schemas import (
    ChatCreate,
    ChatPatch,
    ChatView,
    KeyIn,
    KeyView,
    MessageView,
    ModelList,
    ModelOption,
    ProviderView,
)
from app.chats.service import build_chat_view, chat_view, key_view, member_counts, require
from app.db.models import Chat, ChatKey, ChatMember, Message, Turn, User
from app.deps import AuthDep, CsrfAuthDep, DbDep, RedisDep, SettingsDep
from app.errors import ApiError
from app.events import Outbox
from app.ids import uuid7
from app.messages.service import payloads_with_anchors
from app.providers.catalog import PROVIDERS, KeyValidator
from app.providers.errors import ProviderError
from app.security.keyvault import EncryptedKey, KeyVault, KeyVaultError
from app.security.redaction import registered_secret
from app.security.urls import UnsafeUrlError, assert_public_url
from app.turns.limits import ModelLimits, resolve_limits
from app.turns.runner import cancel_key
from app.turns.schedule import wake

router = APIRouter(tags=["chats"])

ChatId = uuid.UUID


def _vault(request: Request) -> KeyVault:
    vault: KeyVault | None = request.app.state.vault
    if vault is None:
        raise ApiError(503, "vault_unconfigured", "Server key encryption is not configured")
    return vault


def _validator(request: Request) -> KeyValidator:
    return request.app.state.key_validator  # type: ignore[no-any-return]


# --------------------------------------------------------------------- providers


@router.get("/providers", response_model=list[ProviderView])
async def list_providers(_: AuthDep, settings: SettingsDep) -> list[ProviderView]:
    return [
        ProviderView(
            id=p.id, label=p.label, needs_base_url=p.needs_base_url, key_help_url=p.key_help_url
        )
        for p in PROVIDERS.values()
        if not (p.id == "fake" and settings.env == "production")
    ]


# --------------------------------------------------------------------- chats


@router.post("/chats", response_model=ChatView, status_code=201)
async def create_chat(body: ChatCreate, auth: CsrfAuthDep, db: DbDep, redis: RedisDep) -> ChatView:
    title = " ".join((body.title or "").split()) or "New chat"
    chat = Chat(id=uuid7(), owner_id=auth.user.id, title=title)
    db.add(chat)
    member = ChatMember(chat_id=chat.id, user_id=auth.user.id, role="owner")
    db.add(member)
    await db.flush()
    outbox = Outbox()
    await outbox.append(
        db, chat.id, "chat.created", {"title": title, "owner_id": str(auth.user.id)}
    )
    await db.commit()
    await outbox.publish(redis)
    await db.refresh(chat)
    return chat_view(chat, "owner", auth.user, None, 1)


@router.get("/chats", response_model=list[ChatView])
async def list_chats(auth: AuthDep, db: DbDep) -> list[ChatView]:
    rows = (
        await db.execute(
            select(Chat, ChatMember.role, User, ChatKey)
            .join(ChatMember, ChatMember.chat_id == Chat.id)
            .join(User, User.id == Chat.owner_id)
            .outerjoin(ChatKey, ChatKey.chat_id == Chat.id)
            .where(ChatMember.user_id == auth.user.id, ChatMember.removed_at.is_(None))
            .order_by(Chat.updated_at.desc())
        )
    ).all()
    counts = await member_counts(db, [r[0].id for r in rows])
    return [
        chat_view(c, role, owner, key, counts.get(c.id, 0))  # type: ignore[arg-type]
        for c, role, owner, key in rows
    ]


@router.get("/chats/{chat_id}", response_model=ChatView)
async def get_chat(chat_id: ChatId, auth: AuthDep, db: DbDep) -> ChatView:
    m = await require(db, chat_id, auth.user.id, Action.READ)
    return await build_chat_view(db, m)


@router.patch("/chats/{chat_id}", response_model=ChatView)
async def patch_chat(
    chat_id: ChatId,
    body: ChatPatch,
    request: Request,
    auth: CsrfAuthDep,
    db: DbDep,
    redis: RedisDep,
) -> ChatView:
    m = await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT)
    limits: ModelLimits | None = None
    if body.model is not None and body.model != m.chat.model:
        # resolve before taking the row lock: it may call the provider
        limits = await _resolve_model_limits(request, db, m.chat, body.model)
    m = await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT, for_update=True)
    changed: list[str] = []
    clearable = {"daily_token_cap", "rpm_limit"}
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value is None and field not in clearable:
            continue
        if getattr(m.chat, field) != value:
            setattr(m.chat, field, value)
            changed.append(field)
    if limits is not None and "model" in changed:
        m.chat.context_window = limits.context_window
        m.chat.max_output_limit = limits.max_output
        m.chat.limits_source = limits.source
        changed.append("limits")
    if m.chat.max_output_limit and m.chat.max_output_tokens > m.chat.max_output_limit:
        m.chat.max_output_tokens = (
            m.chat.max_output_limit
        )  # never ask for more than the model can give
        if "max_output_tokens" not in changed:
            changed.append("max_output_tokens")
    if changed:
        if m.chat.status == "paused_cap" and "daily_token_cap" in changed:
            m.chat.status = "active"  # the cap check re-pauses if still exceeded
        public: dict[str, object] = {
            f: getattr(m.chat, f)
            for f in ("title", "model", "max_output_tokens", "reply_mode")
            if f in changed
        }
        if "limits" in changed:
            public.update(context_window=m.chat.context_window,
                          max_output_limit=m.chat.max_output_limit,
                          limits_source=m.chat.limits_source)  # fmt: skip
        outbox = Outbox()
        await outbox.append(
            db,
            chat_id,
            "chat.updated",
            {"changed": sorted(changed), **public, "status": m.chat.status},
        )
        await db.commit()
        await outbox.publish(redis)
        await db.refresh(m.chat)
        if {"model", "daily_token_cap", "rpm_limit"} & set(changed):
            await wake(redis, chat_id)  # queued messages may now be runnable
    return await build_chat_view(db, m)


async def _resolve_model_limits(request: Request, db: DbDep, chat: Chat, model: str) -> ModelLimits:
    """Look the model up in the provider's own list (best source), falling back to
    LiteLLM's map and then a default. Never fails the PATCH."""
    listed = None
    if chat.provider:
        try:
            key, plaintext = await _decrypt_stored(request, db, chat)
            with registered_secret(plaintext):
                models = await _validator(request).list_models(
                    key.provider, plaintext, chat.base_url
                )
            del plaintext
            listed = next((x for x in models if x.id == model), None)
        except (ApiError, ProviderError):
            listed = None
    return resolve_limits(chat.provider or "", model, listed)


@router.delete("/chats/{chat_id}", status_code=204)
async def delete_chat(chat_id: ChatId, auth: CsrfAuthDep, db: DbDep, redis: RedisDep) -> Response:
    """Hard delete: key, messages, events, everything, in one transaction."""
    m = await require(db, chat_id, auth.user.id, Action.DELETE_CHAT, for_update=True)
    await db.delete(m.chat)
    await db.commit()
    outbox = Outbox()
    outbox.push_ephemeral(chat_id, "chat.deleted", {})
    await outbox.publish(redis)
    return Response(status_code=204)


# --------------------------------------------------------------------- messages


@router.get("/chats/{chat_id}/messages", response_model=list[MessageView])
async def list_messages(
    chat_id: ChatId,
    auth: AuthDep,
    db: DbDep,
    before_seq: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[MessageView]:
    await require(db, chat_id, auth.user.id, Action.READ)
    stmt = select(Message).where(Message.chat_id == chat_id)
    if before_seq is not None:
        stmt = stmt.where(Message.seq < before_seq)
    msgs = (await db.scalars(stmt.order_by(Message.seq.desc()).limit(limit))).all()
    payloads = await payloads_with_anchors(db, list(reversed(msgs)))
    return [MessageView.model_validate(p) for p in payloads]


# --------------------------------------------------------------------- key slot


@router.put("/chats/{chat_id}/key", response_model=KeyView)
async def put_key(
    chat_id: ChatId,
    body: KeyIn,
    request: Request,
    auth: CsrfAuthDep,
    db: DbDep,
    redis: RedisDep,
    settings: SettingsDep,
) -> KeyView:
    """Write-only. Validates against the provider, encrypts, stores. The plaintext
    lives only in this request's scope and is never returned or logged."""
    api_key = body.api_key.get_secret_value()
    with registered_secret(api_key):
        if body.provider == "fake" and settings.env == "production":
            raise ApiError(422, "provider_unavailable", "Demo provider is disabled")
        meta = PROVIDERS[body.provider]
        base_url: str | None = None
        if meta.needs_base_url:
            if not body.base_url:
                raise ApiError(422, "base_url_required", "This provider needs a base URL")
            try:
                base_url = await assert_public_url(
                    body.base_url, allow_private=settings.allow_private_base_urls
                )
            except UnsafeUrlError as e:
                raise ApiError(422, "unsafe_base_url", str(e)) from None

        m = await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT)
        vault = _vault(request)
        check = await _validator(request).validate_key(body.provider, api_key, base_url)
        if not check.ok and check.error_code == "auth":
            raise ApiError(422, "key_invalid", "The provider rejected this API key")
        status = "ok" if check.ok else "unvalidated"

        blob = vault.encrypt(api_key, chat_id=chat_id, provider=body.provider)
        last4 = api_key[-4:]
        del api_key

        # re-lock the chat row for the write
        m = await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT, for_update=True)
        key = await db.get(ChatKey, chat_id)
        if key is None:
            key = ChatKey(chat_id=chat_id)
            db.add(key)
        key.ciphertext, key.nonce, key.key_id = blob.ciphertext, blob.nonce, blob.key_id
        key.provider = body.provider
        key.last4 = last4
        key.status = status
        key.validated_at = dt.datetime.now(dt.UTC) if check.ok else None

        outbox = Outbox()
        if m.chat.provider != body.provider or m.chat.base_url != base_url:
            if m.chat.provider != body.provider:
                m.chat.model = None  # old model id belongs to the old provider
            m.chat.provider, m.chat.base_url = body.provider, base_url
            await outbox.append(
                db,
                chat_id,
                "chat.updated",
                {
                    "changed": ["provider"],
                    "provider": body.provider,
                    "model": m.chat.model,
                    "status": m.chat.status,
                },
            )
        if m.chat.status == "paused_key":
            m.chat.status = "active"
        await outbox.append(
            db,
            chat_id,
            "key.status",
            {"status": status, "provider": body.provider, "chat_status": m.chat.status},
        )
        await db.commit()
        await outbox.publish(redis)
        await wake(redis, chat_id)  # resume a chat paused on its key
        view = key_view(key, owner=True)
        assert view is not None
        return view


@router.delete("/chats/{chat_id}/key", status_code=204)
async def delete_key(chat_id: ChatId, auth: CsrfAuthDep, db: DbDep, redis: RedisDep) -> Response:
    m = await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT, for_update=True)
    key = await db.get(ChatKey, chat_id)
    if key is not None:
        await db.delete(key)
        outbox = Outbox()
        await outbox.append(
            db,
            chat_id,
            "key.status",
            {"status": "missing", "provider": key.provider, "chat_status": m.chat.status},
        )
        await db.commit()
        await outbox.publish(redis)
        # Stop any in-flight reply (spec §2.6): it's using the key being removed.
        running = await db.scalar(
            select(Turn.id).where(Turn.chat_id == chat_id, Turn.status == "running")
        )
        if running is not None:
            await redis.set(cancel_key(running), str(auth.user.id), ex=600)
    return Response(status_code=204)


async def _decrypt_stored(request: Request, db: DbDep, chat: Chat) -> tuple[ChatKey, str]:
    key = await db.get(ChatKey, chat.id)
    if key is None:
        raise ApiError(409, "no_key", "This chat has no API key yet")
    try:
        plaintext = _vault(request).decrypt(
            EncryptedKey(key.ciphertext, key.nonce, key.key_id),
            chat_id=chat.id,
            provider=key.provider,
        )
    except KeyVaultError:
        raise ApiError(
            409, "key_unreadable", "Stored key can't be decrypted; please re-enter it"
        ) from None
    return key, plaintext


@router.post("/chats/{chat_id}/key/validate", response_model=KeyView)
async def validate_key(
    chat_id: ChatId, request: Request, auth: CsrfAuthDep, db: DbDep, redis: RedisDep
) -> KeyView:
    m = await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT)
    key, plaintext = await _decrypt_stored(request, db, m.chat)
    with registered_secret(plaintext):
        check = await _validator(request).validate_key(key.provider, plaintext, m.chat.base_url)
    del plaintext
    new_status = "ok" if check.ok else ("invalid" if check.error_code == "auth" else key.status)
    m = await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT, for_update=True)
    key.status = new_status
    if check.ok:
        key.validated_at = dt.datetime.now(dt.UTC)
        if m.chat.status == "paused_key":
            m.chat.status = "active"
    outbox = Outbox()
    await outbox.append(
        db,
        chat_id,
        "key.status",
        {"status": new_status, "provider": key.provider, "chat_status": m.chat.status},
    )
    await db.commit()
    await outbox.publish(redis)
    if check.ok:
        await wake(redis, chat_id)
    view = key_view(key, owner=True)
    assert view is not None
    return view


@router.get("/chats/{chat_id}/models", response_model=ModelList)
async def list_models(chat_id: ChatId, request: Request, auth: AuthDep, db: DbDep) -> ModelList:
    m = await require(db, chat_id, auth.user.id, Action.MANAGE_CHAT)
    key, plaintext = await _decrypt_stored(request, db, m.chat)
    with registered_secret(plaintext):
        try:
            models = await _validator(request).list_models(key.provider, plaintext, m.chat.base_url)
        except ProviderError as e:
            raise ApiError(502, f"provider_{e.code}", e.message) from None
    del plaintext
    return ModelList(
        models=[
            ModelOption(
                id=x.id, label=x.extra.get("label") or None, context_window=x.context_window
            )
            for x in models
        ]
    )
