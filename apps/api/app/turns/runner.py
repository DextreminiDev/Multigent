"""Turn runner: lock → claim batch → build prompt → stream → fenced finish.

See ARCHITECTURE.md §6.3-§6.4. Every write after the claim is fenced on
`turns.status='running' AND lock_token=:mine`, so a worker that lost its lock
(GC pause, partition, crash-restart) can never double-write (I2, I3).
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import (
    Chat,
    ChatKey,
    ChatMember,
    Message,
    Turn,
    TurnMessage,
    UsageEvent,
    User,
)
from app.events import Outbox, allocate_seq, chat_channel
from app.ids import uuid7
from app.logging import get_logger
from app.messages.service import message_payload
from app.providers.base import Finish, TextDelta, Usage, estimate_tokens
from app.providers.errors import AuthError, KeyUnavailable, ProviderError, Unknown
from app.providers.registry import ProviderRegistry
from app.security.keyvault import EncryptedKey, KeyVault, KeyVaultError
from app.security.redaction import redact, registered_secret
from app.turns.lock import ChatLock
from app.turns.prompt import ChatContext, HistoryItem, Participant, build_request
from app.turns.schedule import wake, wake_at_least

log = get_logger("turns")

HISTORY_LIMIT = 200
DELTA_FLUSH_S = 0.04  # coalesce token chunks into ~25 updates/sec
BUFFER_TTL_S = 3600

FRIENDLY: dict[str, str] = {
    "auth": "The owner's API key was rejected by the provider. The owner needs to update it.",
    "rate_limit": "The model's rate limit was hit (free-tier keys have low limits). "
    "Try again in a minute.",
    "quota": "The owner's API key is out of quota or credit.",
    "context": "The conversation is too long for this model.",
    "unavailable": "The model provider is unavailable right now. Try again shortly.",
    "blocked": "The provider's safety filter blocked this response.",
    "key": "The stored API key can't be read. The owner needs to re-enter it.",
    "model": "The selected model isn't available for this API key. "
    "The owner should pick a different model in chat settings.",
    "unknown": "Something went wrong talking to the model.",
}


def buffer_key(turn_id: uuid.UUID | str) -> str:
    return f"turn:{turn_id}:buf"


@dataclass
class Claim:
    turn_id: uuid.UUID
    chat_id: uuid.UUID
    token: uuid.UUID
    ctx: ChatContext
    participants: list[Participant]
    history: list[HistoryItem]
    key: EncryptedKey
    key_provider: str
    message_ids: list[uuid.UUID]
    author_ids: list[uuid.UUID]
    started_seq: int


@dataclass
class StreamResult:
    text: str = ""
    usage: Usage | None = None
    finish: str = "stop"
    error: ProviderError | None = None
    lost_lock: bool = False
    chunks: int = 0


class DeltaPublisher:
    """Coalesces provider chunks and publishes `turn.delta` frames. Each frame has a
    per-turn chunk index (`idx`) so clients joining mid-stream dedupe exactly; the
    same frames are appended to `turn:{id}:buf` for late joiners (§8.3)."""

    def __init__(self, redis: Redis, chat_id: uuid.UUID, turn_id: uuid.UUID) -> None:
        self.redis = redis
        self.chat_id = chat_id
        self.turn_id = turn_id
        self.idx = 0
        self._pending = ""
        self._last = time.monotonic()

    async def add(self, text: str) -> None:
        self._pending += text
        if time.monotonic() - self._last >= DELTA_FLUSH_S:
            await self.flush()

    async def flush(self) -> None:
        if not self._pending:
            return
        frame = {"idx": self.idx, "text": self._pending}
        self.idx += 1
        self._pending = ""
        self._last = time.monotonic()
        event = {
            "type": "turn.delta",
            "chat_id": str(self.chat_id),
            "payload": {"turn_id": str(self.turn_id), **frame},
        }
        async with self.redis.pipeline(transaction=False) as p:
            p.rpush(buffer_key(self.turn_id), json.dumps(frame))
            p.expire(buffer_key(self.turn_id), BUFFER_TTL_S)
            p.publish(chat_channel(self.chat_id), json.dumps(event))
            await p.execute()


@dataclass
class TurnRunner:
    sessionmaker: async_sessionmaker[AsyncSession]
    redis: Redis
    vault: KeyVault | None
    providers: ProviderRegistry = field(default_factory=ProviderRegistry)
    lock_ttl_ms: int = 30_000
    heartbeat_s: float = 10.0

    # ------------------------------------------------------------------ entry

    async def run_chat(self, chat_id: uuid.UUID) -> None:
        token = uuid.uuid4()
        lock = ChatLock(self.redis, chat_id, token, self.lock_ttl_ms, self.heartbeat_s)
        if not await lock.acquire():
            # A turn is running; its finish re-checks pending messages. Re-queue
            # shortly anyway in case its check raced with our wake-up.
            await wake_at_least(self.redis, chat_id, 1000)
            return
        lock.start_heartbeat()
        try:
            claim = await self.claim(chat_id, token)
            if claim is not None:
                await self.execute(claim, lock)
        finally:
            await lock.release()
        if await self.runnable_pending(chat_id):
            await wake(self.redis, chat_id, 0)

    # ------------------------------------------------------------------ claim

    async def runnable_pending(self, chat_id: uuid.UUID) -> bool:
        async with self.sessionmaker() as db:
            row = (
                await db.execute(
                    select(Chat.status, Chat.model, ChatKey.status)
                    .outerjoin(ChatKey, ChatKey.chat_id == Chat.id)
                    .where(Chat.id == chat_id)
                )
            ).first()
            if row is None or row[0] != "active" or not row[1] or row[2] in (None, "invalid"):
                return False
            n = await db.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.chat_id == chat_id,
                    Message.status == "pending",
                    Message.kind == "human",
                )
            )
            return bool(n)

    async def _recover_orphan(self, db: AsyncSession, chat_id: uuid.UUID, outbox: Outbox) -> None:
        """We hold the chat lock, so any turn still 'running' belongs to a worker
        whose lock expired (crash). Fail it and return its batch to pending.
        (The periodic sweeper in milestone 5 does the same proactively.)"""
        orphan = await db.scalar(
            select(Turn).where(Turn.chat_id == chat_id, Turn.status == "running").with_for_update()
        )
        if orphan is None:
            return
        ids = await self._requeue_batch(db, orphan.id)
        orphan.status = "failed"
        orphan.error_code = "crash"
        orphan.error_detail = "Worker stopped during the turn; messages were re-queued."
        orphan.finished_at = dt.datetime.now(dt.UTC)
        await outbox.append(
            db,
            chat_id,
            "turn.failed",
            {
                "turn_id": str(orphan.id),
                "error_code": "crash",
                "error_message": "The previous reply was interrupted and will be retried.",
                "message_ids": [str(i) for i in ids],
                "requeued": True,
                "partial_text": None,
            },
        )
        await self.redis.delete(buffer_key(orphan.id))
        log.warning("turn.orphan_recovered", chat_id=str(chat_id), turn_id=str(orphan.id))

    async def _requeue_batch(self, db: AsyncSession, turn_id: uuid.UUID) -> list[uuid.UUID]:
        ids = list(
            (
                await db.scalars(
                    select(TurnMessage.message_id).where(TurnMessage.turn_id == turn_id)
                )
            ).all()
        )
        if ids:
            await db.execute(
                update(Message)
                .where(Message.id.in_(ids), Message.status == "batched")
                .values(status="pending", turn_id=None)
            )
        return ids

    async def claim(self, chat_id: uuid.UUID, token: uuid.UUID) -> Claim | None:
        outbox = Outbox()
        async with self.sessionmaker() as db:
            chat = await db.scalar(select(Chat).where(Chat.id == chat_id).with_for_update())
            if chat is None:
                return None
            await self._recover_orphan(db, chat_id, outbox)
            key = await db.get(ChatKey, chat_id)
            if (
                chat.status != "active"
                or not chat.model
                or not chat.provider
                or key is None
                or key.status == "invalid"
            ):
                await db.commit()
                await outbox.publish(self.redis)
                return None
            pending = list(
                (
                    await db.scalars(
                        select(Message)
                        .where(
                            Message.chat_id == chat_id,
                            Message.status == "pending",
                            Message.kind == "human",
                        )
                        .order_by(Message.seq)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            if not pending:
                await db.commit()
                await outbox.publish(self.redis)
                return None

            turn = Turn(
                id=uuid7(),
                chat_id=chat_id,
                status="running",
                trigger="batch",
                lock_token=token,
                provider=chat.provider,
                model=chat.model,
            )
            db.add(turn)
            try:
                await db.flush()  # partial unique index: backstop for I3
            except IntegrityError:
                await db.rollback()
                log.warning("turn.claim_conflict", chat_id=str(chat_id))
                return None
            for m in pending:
                m.status = "batched"
                m.turn_id = turn.id
                db.add(TurnMessage(turn_id=turn.id, message_id=m.id))
            message_ids = [m.id for m in pending]
            author_ids = list(dict.fromkeys(m.author_id for m in pending if m.author_id))
            started_seq = await outbox.append(
                db,
                chat_id,
                "turn.started",
                {
                    "turn_id": str(turn.id),
                    "trigger": "batch",
                    "message_ids": [str(i) for i in message_ids],
                    "author_ids": [str(a) for a in author_ids],
                    "provider": chat.provider,
                    "model": chat.model,
                },
            )
            turn.started_seq = started_seq

            participants = [
                Participant(user_id=u.id, name=u.name, role=role)  # type: ignore[arg-type]
                for u, role in (
                    await db.execute(
                        select(User, ChatMember.role)
                        .join(ChatMember, ChatMember.user_id == User.id)
                        .where(ChatMember.chat_id == chat_id, ChatMember.removed_at.is_(None))
                    )
                ).all()
            ]
            await db.flush()
            rows = (
                await db.execute(
                    select(Message, Turn.started_seq)
                    .outerjoin(Turn, Turn.id == Message.turn_id)
                    .where(
                        Message.chat_id == chat_id,
                        Message.status.not_in(("deleted", "superseded", "pending")),
                        ~((Message.kind == "ai") & (Message.status != "done")),
                    )
                    .order_by(Message.seq.desc())
                    .limit(HISTORY_LIMIT)
                )
            ).all()
            history = [
                HistoryItem(
                    seq=m.seq,
                    kind=m.kind,  # type: ignore[arg-type]
                    content=m.content,
                    created_at=m.created_at,
                    author_id=m.author_id,
                    author_name=m.author_name_snapshot,
                    anchor=anchor,
                )
                for m, anchor in reversed(rows)
            ]
            ctx = ChatContext(
                title=chat.title,
                provider=chat.provider,
                model=chat.model,
                max_output_tokens=chat.max_output_tokens,
                custom_instructions=chat.custom_instructions,
                base_url=chat.base_url,
            )
            blob = EncryptedKey(key.ciphertext, key.nonce, key.key_id)
            key_provider = key.provider
            await db.commit()
        await outbox.publish(self.redis)
        log.info("turn.started", chat_id=str(chat_id), turn_id=str(turn.id), batch=len(pending))
        return Claim(
            turn_id=turn.id,
            chat_id=chat_id,
            token=token,
            ctx=ctx,
            participants=participants,
            history=history,
            key=blob,
            key_provider=key_provider,
            message_ids=message_ids,
            author_ids=author_ids,
            started_seq=started_seq,
        )

    # ------------------------------------------------------------------ execute

    async def execute(self, claim: Claim, lock: ChatLock) -> None:
        result = StreamResult()
        try:
            if self.vault is None:
                raise KeyVaultError("vault unconfigured")
            api_key = self.vault.decrypt(
                claim.key, chat_id=claim.chat_id, provider=claim.key_provider
            )
        except KeyVaultError:
            result.error = KeyUnavailable("stored key unreadable")
            await self.finish_failed(claim, result)
            return

        with registered_secret(api_key):
            try:
                req = build_request(claim.ctx, claim.participants, claim.history)
                await self._stream(claim, req, api_key, lock, result)
            except ProviderError as e:
                e.message = redact(e.message)  # key still registered here
                result.error = e
            except Exception as e:  # never let an unexpected error kill the worker
                log.exception("turn.unexpected_error", turn_id=str(claim.turn_id))
                result.error = Unknown(f"{type(e).__name__}")
            del api_key

        if result.lost_lock:
            log.warning("turn.lock_lost", turn_id=str(claim.turn_id))
            return
        if result.error is None:
            await self.finish_completed(claim, result)
        else:
            await self.finish_failed(claim, result)

    async def _stream(
        self,
        claim: Claim,
        req: Any,
        api_key: str,
        lock: ChatLock,
        result: StreamResult,
    ) -> None:
        provider = self.providers.get(claim.ctx.provider)
        pub = DeltaPublisher(self.redis, claim.chat_id, claim.turn_id)
        stream = provider.stream_chat(req, api_key)
        try:
            async for chunk in stream:
                if lock.lost.is_set():
                    result.lost_lock = True
                    return
                if isinstance(chunk, TextDelta):
                    result.text += chunk.text
                    result.chunks += 1
                    await pub.add(chunk.text)
                elif isinstance(chunk, Usage):
                    result.usage = chunk
                elif isinstance(chunk, Finish):
                    result.finish = chunk.reason
        finally:
            await pub.flush()
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                await aclose()

    # ------------------------------------------------------------------ finish

    async def _fence(self, db: AsyncSession, claim: Claim, **values: Any) -> bool:
        res = await db.execute(
            update(Turn)
            .where(
                Turn.id == claim.turn_id,
                Turn.status == "running",
                Turn.lock_token == claim.token,
            )
            .values(**values)
            .returning(Turn.id)
        )
        return res.first() is not None

    async def finish_completed(self, claim: Claim, result: StreamResult) -> None:
        now = dt.datetime.now(dt.UTC)
        tokens_in = result.usage.tokens_in if result.usage else 0
        tokens_out = result.usage.tokens_out if result.usage else estimate_tokens(result.text)
        outbox = Outbox()
        async with self.sessionmaker() as db:
            await db.scalar(select(Chat.id).where(Chat.id == claim.chat_id).with_for_update())
            ai_id = uuid7()
            if not await self._fence(
                db,
                claim,
                status="completed",
                finished_at=now,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            ):
                await db.rollback()
                log.warning("turn.fenced_out", turn_id=str(claim.turn_id))
                return
            seq = await allocate_seq(db, claim.chat_id)
            ai = Message(
                id=ai_id,
                chat_id=claim.chat_id,
                seq=seq,
                kind="ai",
                content=result.text,
                status="done",
                turn_id=claim.turn_id,
                created_at=now,
            )
            db.add(ai)
            await db.flush()
            await db.execute(
                update(Turn).where(Turn.id == claim.turn_id).values(ai_message_id=ai_id)
            )
            await db.execute(
                update(Message)
                .where(Message.id.in_(claim.message_ids), Message.status == "batched")
                .values(status="done")
            )
            db.add(
                UsageEvent(
                    chat_id=claim.chat_id,
                    turn_id=claim.turn_id,
                    kind="turn",
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    day=now.date(),
                )
            )
            outbox.record(
                db,
                claim.chat_id,
                seq,
                "turn.completed",
                {
                    "turn_id": str(claim.turn_id),
                    "message": message_payload(ai, anchor_seq=claim.started_seq),
                    "message_ids": [str(i) for i in claim.message_ids],
                    "author_ids": [str(a) for a in claim.author_ids],
                    "finish_reason": result.finish,
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            )
            await db.commit()
        await outbox.publish(self.redis)
        await self.redis.delete(buffer_key(claim.turn_id))
        log.info(
            "turn.completed",
            chat_id=str(claim.chat_id),
            turn_id=str(claim.turn_id),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    async def finish_failed(self, claim: Claim, result: StreamResult) -> None:
        err = result.error or Unknown()
        code = err.code
        # Auth failures and unreadable keys aren't the messages' fault: put the batch
        # back in the queue and pause the chat until the owner fixes the key (§6.4).
        requeue = isinstance(err, AuthError | KeyUnavailable)
        now = dt.datetime.now(dt.UTC)
        outbox = Outbox()
        async with self.sessionmaker() as db:
            chat = await db.scalar(select(Chat).where(Chat.id == claim.chat_id).with_for_update())
            if not await self._fence(
                db,
                claim,
                status="failed",
                finished_at=now,
                error_code=code,
                error_detail=err.message[:1000],
                partial_text=result.text or None,
                tokens_out=estimate_tokens(result.text) if result.text else 0,
            ):
                await db.rollback()
                return
            ids = claim.message_ids
            if requeue:
                await self._requeue_batch(db, claim.turn_id)
            await outbox.append(
                db,
                claim.chat_id,
                "turn.failed",
                {
                    "turn_id": str(claim.turn_id),
                    "error_code": code,
                    "error_message": FRIENDLY.get(code, FRIENDLY["unknown"]),
                    # provider's own explanation, already redacted (useful to the owner)
                    "detail": err.message[:500]
                    if code in ("model", "unknown", "unavailable", "rate_limit", "quota")
                    else None,
                    "message_ids": [str(i) for i in ids],
                    "requeued": requeue,
                    "partial_text": result.text or None,
                },
            )
            if requeue and chat is not None:
                chat.status = "paused_key"
                key = await db.get(ChatKey, claim.chat_id)
                if key is not None:
                    key.status = "invalid"
                await outbox.append(
                    db,
                    claim.chat_id,
                    "key.status",
                    {
                        "status": "invalid" if key is not None else "missing",
                        "provider": key.provider if key is not None else None,
                        "chat_status": "paused_key",
                    },
                )
            await db.commit()
        await outbox.publish(self.redis)
        await self.redis.delete(buffer_key(claim.turn_id))
        log.info("turn.failed", chat_id=str(claim.chat_id), turn_id=str(claim.turn_id), code=code)
