"""Turn runner: lock → claim batch → build prompt → stream → fenced finish.

See ARCHITECTURE.md §6.3-§6.4. Every write after the claim is fenced on
`turns.status='running' AND lock_token=:mine`, so a worker that lost its lock
(GC pause, partition, crash-restart) can never double-write (I2, I3).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import random
import time
import uuid
from collections.abc import Callable, Sequence
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
    ChatSummary,
    Message,
    Turn,
    TurnMessage,
    User,
)
from app.events import Outbox, allocate_seq, chat_channel
from app.ids import uuid7
from app.logging import get_logger
from app.messages.service import message_payload
from app.providers.base import Finish, Msg, ProviderRequest, TextDelta, Usage, estimate_tokens
from app.providers.errors import (
    AuthError,
    ContentBlocked,
    ContextTooLong,
    KeyUnavailable,
    ProviderError,
    ProviderUnavailable,
    RateLimitError,
    Unknown,
)
from app.providers.registry import ProviderRegistry
from app.security.keyvault import EncryptedKey, KeyVault, KeyVaultError
from app.security.redaction import redact, registered_secret
from app.turns.context import (
    KEEP_FRACTION,
    Budget,
    measure,
    position,
    split_for_compaction,
    summary_budget,
    summary_request,
)
from app.turns.limits import effective_max_output, effective_rpm, resolve_limits
from app.turns.lock import ChatLock
from app.turns.prompt import ChatContext, HistoryItem, Participant, build_request
from app.turns.schedule import wake, wake_at_least
from app.usage.service import bump_counters, tokens_used, usage_rows

log = get_logger("turns")

HISTORY_LIMIT = 1000  # older context lives in the rolling summary (§10.2)
MAX_PROVIDER_RETRIES = 3
MAX_BACKOFF_S = 60.0
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


def context_key(chat_id: uuid.UUID | str) -> str:
    """Latest context-budget snapshot for the meter (written by the worker)."""
    return f"chat:{chat_id}:context"


def compact_key(chat_id: uuid.UUID | str) -> str:
    """Owner asked for "Compact now": consumed by the next claim."""
    return f"chat:{chat_id}:compact"


def rpm_key(chat_id: uuid.UUID | str) -> str:
    return f"rpm:{chat_id}"


def cancel_key(turn_id: uuid.UUID | str) -> str:
    """Set by the cancel endpoint (value = who asked); polled by the running turn."""
    return f"turn:{turn_id}:cancel"


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
    context_window: int = 32_000
    limits_source: str = "default"
    rpm: int | None = None
    summary: str | None = None
    summary_through: int | None = None
    force_compact: bool = False


@dataclass
class StreamResult:
    text: str = ""
    usage: Usage | None = None
    finish: str = "stop"
    error: ProviderError | None = None
    lost_lock: bool = False
    cancelled: bool = False
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
    cancel_poll_s: float = 0.2

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

    async def _over_cap(self, db: AsyncSession, chat: Chat) -> bool:
        """Pre-check (§12): paused if today's usage plus a rough estimate of this
        turn's input would exceed the chat's daily cap."""
        assert chat.daily_token_cap is not None
        used = await tokens_used(self.redis, db, chat.id)
        if used >= chat.daily_token_cap:
            return True
        chars = await db.scalar(
            select(func.coalesce(func.sum(func.length(Message.content)), 0)).where(
                Message.chat_id == chat.id,
                Message.status.not_in(("deleted", "superseded")),
                Message.intent.is_distinct_from("off"),
            )
        )
        window = chat.context_window or 32_000
        est_in = min(int(chars or 0) // 4, int(window * 0.7))
        return used + est_in > chat.daily_token_cap

    async def _post_cap_check(self, chat_id: uuid.UUID) -> None:
        """After a turn: pause the chat if it just reached its daily cap."""
        outbox = Outbox()
        async with self.sessionmaker() as db:
            chat = await db.scalar(select(Chat).where(Chat.id == chat_id).with_for_update())
            if chat is None or not chat.daily_token_cap or chat.status != "active":
                return
            if await tokens_used(self.redis, db, chat_id) < chat.daily_token_cap:
                return
            chat.status = "paused_cap"
            await outbox.append(
                db,
                chat_id,
                "chat.updated",
                {"changed": ["status"], "status": "paused_cap", "reason": "daily_cap"},
            )
            await db.commit()
        await outbox.publish(self.redis)

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
            if chat.daily_token_cap and await self._over_cap(db, chat):
                chat.status = "paused_cap"
                await outbox.append(
                    db,
                    chat_id,
                    "chat.updated",
                    {"changed": ["status"], "status": "paused_cap", "reason": "daily_cap"},
                )
                await db.commit()
                await outbox.publish(self.redis)
                log.info("turn.paused_cap", chat_id=str(chat_id))
                return None

            # Messages put back by Retry/Regenerate still point at their previous turn.
            prior = {m.turn_id for m in pending if m.turn_id is not None}
            trigger = "batch"
            if prior:
                statuses = set(
                    (await db.scalars(select(Turn.status).where(Turn.id.in_(prior)))).all()
                )
                trigger = "regenerate" if "completed" in statuses else "retry"
            turn = Turn(
                id=uuid7(),
                chat_id=chat_id,
                status="running",
                trigger=trigger,
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
                    "trigger": trigger,
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
                        .order_by(ChatMember.joined_at)  # stable namesake labels
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
                        Message.intent.is_distinct_from("off"),  # humans-only
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
                    to_ai=m.intent != "context",
                )
                for m, anchor in reversed(rows)
            ]
            latest = await db.scalar(
                select(ChatSummary)
                .where(ChatSummary.chat_id == chat_id)
                .order_by(ChatSummary.created_at.desc())
                .limit(1)
            )
            if latest is not None:  # covered history now lives in the summary
                history = [h for h in history if position(h) > latest.covers_through_seq]
            if chat.context_window:
                window, source = chat.context_window, chat.limits_source or "provider"
            else:  # model chosen before limits were tracked
                lim = resolve_limits(chat.provider, chat.model)
                window, source = lim.context_window, lim.source
            ctx = ChatContext(
                title=chat.title,
                provider=chat.provider,
                model=chat.model,
                max_output_tokens=effective_max_output(
                    chat.max_output_tokens, chat.max_output_limit
                ),
                custom_instructions=chat.custom_instructions,
                base_url=chat.base_url,
            )
            blob = EncryptedKey(key.ciphertext, key.nonce, key.key_id)
            key_provider = key.provider
            rpm = effective_rpm(chat.provider, chat.rpm_limit)
            force_compact = bool(await self.redis.getdel(compact_key(chat_id)))
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
            context_window=window,
            limits_source=source,
            rpm=rpm,
            summary=latest.content if latest is not None else None,
            summary_through=latest.covers_through_seq if latest is not None else None,
            force_compact=force_compact,
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
                await self._run_with_context(claim, api_key, lock, result)
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
        if result.cancelled:
            await self.finish_cancelled(claim, result)
        elif result.error is None:
            await self.finish_completed(claim, result)
        else:
            await self.finish_failed(claim, result)

    # ------------------------------------------------------------------ context

    def _counter(self, claim: Claim) -> Callable[[Sequence[Msg], str], int]:
        provider = self.providers.get(claim.ctx.provider)
        if claim.ctx.provider == "fake":
            model = claim.ctx.model
        else:
            from app.providers.litellm_provider import litellm_model

            model = litellm_model(claim.ctx.provider, claim.ctx.model)

        def count(msgs: Sequence[Msg], system: str) -> int:
            return provider.count_tokens(model, msgs, system)

        return count

    async def _prepare(
        self, claim: Claim, api_key: str, *, force: bool = False, shrink: float = 1.0
    ) -> ProviderRequest:
        """Build the request; if it doesn't fit the budget (or a compaction was
        requested), fold the oldest exchanges into the rolling summary first."""
        count = self._counter(claim)
        req = build_request(claim.ctx, claim.participants, claim.history, claim.summary)
        b = measure(
            claim.context_window,
            claim.ctx.max_output_tokens,
            count([], req.system),
            count(req.messages, ""),
        )
        if b.fits and not force:
            await self._publish_context(claim, b)
            return req
        keep_tokens = max(0, int(b.available * KEEP_FRACTION * shrink))
        if force:
            # Forced (owner's "Compact now", or the provider said "too long" although
            # our estimate fit): always fold, keeping at most half the history
            # (a quarter after a "too long").
            keep_tokens = min(keep_tokens, int(b.needed * 0.5 * shrink))
        fold, keep = split_for_compaction(
            claim.history, claim.participants, keep_tokens, count, claim.started_seq
        )
        if not fold:
            await self._publish_context(claim, b)
            return req
        await self._status(claim, "compacting")
        sreq = summary_request(
            claim.ctx.provider,
            claim.ctx.model,
            claim.ctx.base_url,
            claim.summary,
            fold,
            claim.participants,
            max_tokens=summary_budget(b.available),
        )
        text, usage = await self._complete(claim, sreq, api_key)
        through = max(position(f) for f in fold)
        await self._store_summary(claim, text, through, usage)
        claim.history, claim.summary, claim.summary_through = keep, text, through
        req = build_request(claim.ctx, claim.participants, claim.history, claim.summary)
        b = measure(
            claim.context_window,
            claim.ctx.max_output_tokens,
            count([], req.system),
            count(req.messages, ""),
        )
        await self._publish_context(claim, b)
        return req

    async def _complete(
        self, claim: Claim, req: ProviderRequest, api_key: str
    ) -> tuple[str, Usage | None]:
        """Non-visible provider call (summaries): collect the whole text."""
        await self._acquire_rpm(claim, None)
        provider = self.providers.get(claim.ctx.provider)
        text, usage = "", None
        async for chunk in provider.stream_chat(req, api_key):
            if isinstance(chunk, TextDelta):
                text += chunk.text
            elif isinstance(chunk, Usage):
                usage = chunk
        if not text.strip():
            raise ContentBlocked("The model returned an empty summary")
        return text.strip(), usage

    async def _store_summary(
        self, claim: Claim, text: str, through: int, usage: Usage | None
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        async with self.sessionmaker() as db:
            db.add(
                ChatSummary(
                    chat_id=claim.chat_id,
                    covers_through_seq=through,
                    content=text,
                    tokens=estimate_tokens(text),
                    created_at=now,
                )
            )
            rows = usage_rows(
                chat_id=claim.chat_id,
                turn_id=claim.turn_id,
                kind="summary",
                authors=[],
                tokens_in=usage.tokens_in if usage else 0,
                tokens_out=usage.tokens_out if usage else estimate_tokens(text),
                provider=claim.ctx.provider,
                model=claim.ctx.model,
                day=now.date(),
            )
            db.add_all(rows)
            await db.commit()
        await bump_counters(self.redis, rows)
        log.info("turn.compacted", chat_id=str(claim.chat_id), through=through)

    async def _publish_context(self, claim: Claim, b: Budget) -> None:
        used = max(0, b.budget - b.available - claim.ctx.max_output_tokens + b.needed)
        info = {
            "window": b.window,
            "budget": b.budget,
            "used": used,
            "percent": round(100 * used / b.budget, 1) if b.budget > 0 else 100.0,
            "source": claim.limits_source,
            "summary_through": claim.summary_through,
            "max_output": claim.ctx.max_output_tokens,
        }
        await self.redis.set(context_key(claim.chat_id), json.dumps(info), ex=30 * 24 * 3600)
        await self.redis.publish(
            chat_channel(claim.chat_id),
            json.dumps({"type": "context.update", "chat_id": str(claim.chat_id), "payload": info}),
        )

    async def _status(self, claim: Claim, status: str, **extra: Any) -> None:
        """Ephemeral sub-state of a running turn (turn.status)."""
        await self.redis.publish(
            chat_channel(claim.chat_id),
            json.dumps(
                {
                    "type": "turn.status",
                    "chat_id": str(claim.chat_id),
                    "payload": {"turn_id": str(claim.turn_id), "status": status, **extra},
                }
            ),
        )

    # ------------------------------------------------------------------ retries / RPM

    async def _wait(
        self,
        claim: Claim,
        lock: ChatLock | None,
        seconds: float,
        result: StreamResult | None,
    ) -> bool:
        """Sleep, but wake early (returning True) on Stop or a lost lock."""
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if lock is not None and lock.lost.is_set():
                if result is not None:
                    result.lost_lock = True
                return True
            if await self.redis.exists(cancel_key(claim.turn_id)):
                if result is not None:
                    result.cancelled = True
                return True
            await asyncio.sleep(min(self.cancel_poll_s, max(0.0, end - time.monotonic())))
        return False

    async def _acquire_rpm(
        self, claim: Claim, lock: ChatLock | None, result: StreamResult | None = None
    ) -> bool:
        """Proactive ceiling on provider calls per minute for this chat's key, so a
        free-tier key queues instead of collecting 429s. True if interrupted."""
        if not claim.rpm:
            return False
        key = rpm_key(claim.chat_id)
        while True:
            now = time.time()
            async with self.redis.pipeline(transaction=True) as p:
                p.zremrangebyscore(key, "-inf", now - 60)
                p.zcard(key)
                _, n = await p.execute()
            if int(n) < claim.rpm:
                await self.redis.zadd(key, {uuid.uuid4().hex: now})
                await self.redis.expire(key, 120)
                return False
            oldest = await self.redis.zrange(key, 0, 0, withscores=True)
            wait = max(0.5, (float(oldest[0][1]) + 60 - now) if oldest else 1.0)
            await self._status(claim, "waiting_rate", retry_in=round(wait, 1))
            if await self._wait(claim, lock, wait, result):
                return True

    async def _run_with_context(
        self, claim: Claim, api_key: str, lock: ChatLock, result: StreamResult
    ) -> None:
        req = await self._prepare(claim, api_key, force=claim.force_compact)
        try:
            await self._stream_with_retries(claim, req, api_key, lock, result)
        except ContextTooLong:
            if result.text:
                raise
            # our estimate was off for this provider: fold harder and try once more
            req = await self._prepare(claim, api_key, force=True, shrink=0.5)
            await self._stream_with_retries(claim, req, api_key, lock, result)

    async def _stream_with_retries(
        self,
        claim: Claim,
        req: ProviderRequest,
        api_key: str,
        lock: ChatLock,
        result: StreamResult,
    ) -> None:
        for attempt in range(MAX_PROVIDER_RETRIES + 1):
            if await self._acquire_rpm(claim, lock, result):
                return
            try:
                await self._stream(claim, req, api_key, lock, result)
                return
            except (RateLimitError, ProviderUnavailable) as e:
                # Never retry after visible output: it would duplicate text.
                if result.text or attempt == MAX_PROVIDER_RETRIES:
                    raise
                retry_after = e.retry_after if isinstance(e, RateLimitError) else None
                delay = min(MAX_BACKOFF_S, retry_after or float(2 ** (attempt + 1)))
                delay += random.uniform(0, 1)  # noqa: S311 (jitter, not crypto)
                status = "rate_limited" if isinstance(e, RateLimitError) else "unavailable"
                await self._status(
                    claim,
                    status,
                    retry_in=round(delay, 1),
                    attempt=attempt + 1,
                    max_attempts=MAX_PROVIDER_RETRIES,
                    detail=e.message[:200],
                )
                log.info(
                    "turn.backoff", turn_id=str(claim.turn_id), status=status, delay=round(delay, 1)
                )
                if await self._wait(claim, lock, delay, result):
                    return
                await self._status(claim, "retrying", attempt=attempt + 1)

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

        async def consume() -> None:
            stream = provider.stream_chat(req, api_key)
            try:
                async for chunk in stream:
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

        async def watch() -> str:
            """Interrupt the stream on user cancel or lost lock, even while the
            provider is silent (polling a Redis key is cheap at ~5/s per turn)."""
            while True:
                if lock.lost.is_set():
                    return "lost"
                if await self.redis.exists(cancel_key(claim.turn_id)):
                    return "cancel"
                await asyncio.sleep(self.cancel_poll_s)

        streamer = asyncio.create_task(consume())
        watcher = asyncio.create_task(watch())
        try:
            done, _ = await asyncio.wait({streamer, watcher}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (streamer, watcher):
                if not t.done():
                    t.cancel()
            await asyncio.gather(streamer, watcher, return_exceptions=True)
        if streamer in done:
            streamer.result()  # re-raise provider errors
            return
        if watcher.result() == "lost":
            result.lost_lock = True
        else:
            result.cancelled = True

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
            rows = usage_rows(
                chat_id=claim.chat_id,
                turn_id=claim.turn_id,
                kind="turn",
                authors=claim.author_ids,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                provider=claim.ctx.provider,
                model=claim.ctx.model,
                day=now.date(),
            )
            db.add_all(rows)
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
        await bump_counters(self.redis, rows)
        await self._post_cap_check(claim.chat_id)
        log.info(
            "turn.completed",
            chat_id=str(claim.chat_id),
            turn_id=str(claim.turn_id),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    async def finish_cancelled(self, claim: Claim, result: StreamResult) -> None:
        """User pressed Stop: keep the partial text on the turn, mark the batch as
        answered-as-attempted (`done`); anyone allowed can hit Retry (§6.4)."""
        now = dt.datetime.now(dt.UTC)
        by = await self.redis.get(cancel_key(claim.turn_id))
        tokens_out = estimate_tokens(result.text) if result.text else 0
        outbox = Outbox()
        async with self.sessionmaker() as db:
            await db.scalar(select(Chat.id).where(Chat.id == claim.chat_id).with_for_update())
            if not await self._fence(
                db,
                claim,
                status="cancelled",
                finished_at=now,
                partial_text=result.text or None,
                tokens_out=tokens_out,
            ):
                await db.rollback()
                return
            await db.execute(
                update(Message)
                .where(Message.id.in_(claim.message_ids), Message.status == "batched")
                .values(status="done")
            )
            rows = []
            if tokens_out:  # partial output still cost the owner tokens
                rows = usage_rows(
                    chat_id=claim.chat_id,
                    turn_id=claim.turn_id,
                    kind="turn",
                    authors=claim.author_ids,
                    tokens_in=result.usage.tokens_in if result.usage else 0,
                    tokens_out=tokens_out,
                    provider=claim.ctx.provider,
                    model=claim.ctx.model,
                    day=now.date(),
                )
                db.add_all(rows)
            await outbox.append(
                db,
                claim.chat_id,
                "turn.cancelled",
                {
                    "turn_id": str(claim.turn_id),
                    "message_ids": [str(i) for i in claim.message_ids],
                    "partial_text": result.text or None,
                    "cancelled_by": str(by) if by else None,
                },
            )
            await db.commit()
        await outbox.publish(self.redis)
        await self.redis.delete(buffer_key(claim.turn_id), cancel_key(claim.turn_id))
        await bump_counters(self.redis, rows)
        log.info("turn.cancelled", chat_id=str(claim.chat_id), turn_id=str(claim.turn_id))

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
