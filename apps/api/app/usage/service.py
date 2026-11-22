"""Usage accounting, caps and the per-user message rate limit (ARCHITECTURE.md §12, §11.5).

- Every provider call writes `usage_events` (provider-reported tokens when given,
  else estimated). A turn's tokens are split evenly across the authors of its
  batch (they asked together); summaries are chat-level (user_id NULL).
- Fast cap checks read Redis counters `usage:{chat}:{day}[:{user}]`, rebuilt from
  `usage_events` when missing (Redis is never the source of truth, I6).
- Day boundary: UTC.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UsageEvent

COUNTER_TTL_S = 3 * 24 * 3600


def today() -> dt.date:
    return dt.datetime.now(dt.UTC).date()


def _key(chat_id: uuid.UUID, day: dt.date, user_id: uuid.UUID | None = None) -> str:
    base = f"usage:{chat_id}:{day.isoformat()}"
    return f"{base}:{user_id}" if user_id else base


def split_tokens(total: int, authors: list[uuid.UUID]) -> dict[uuid.UUID | None, int]:
    """Even split with the remainder going to the first authors; no authors -> chat."""
    if not authors:
        return {None: total}
    share, rest = divmod(total, len(authors))
    return {a: share + (1 if i < rest else 0) for i, a in enumerate(authors)}


def estimate_cost(provider: str, model: str, tokens_in: int, tokens_out: int) -> Decimal | None:
    """USD estimate from LiteLLM's price map; None when pricing is unknown or free."""
    if provider == "fake" or (tokens_in == 0 and tokens_out == 0):
        return None
    try:
        import litellm

        from app.providers.litellm_provider import litellm_model

        cin, cout = litellm.cost_per_token(
            model=litellm_model(provider, model),
            prompt_tokens=tokens_in,
            completion_tokens=tokens_out,
        )
        total = Decimal(str(cin)) + Decimal(str(cout))
        return total.quantize(Decimal("0.000001")) if total > 0 else None
    except Exception:
        return None


def usage_rows(
    *,
    chat_id: uuid.UUID,
    turn_id: uuid.UUID | None,
    kind: str,
    authors: list[uuid.UUID],
    tokens_in: int,
    tokens_out: int,
    provider: str,
    model: str,
    day: dt.date,
) -> list[UsageEvent]:
    ins = split_tokens(tokens_in, authors)
    outs = split_tokens(tokens_out, authors)
    rows = []
    for user in ins:
        tin, tout = ins[user], outs[user]
        rows.append(
            UsageEvent(
                chat_id=chat_id,
                user_id=user,
                turn_id=turn_id,
                kind=kind,
                tokens_in=tin,
                tokens_out=tout,
                est_cost_usd=estimate_cost(provider, model, tin, tout),
                day=day,
            )
        )
    return rows


async def bump_counters(redis: Redis, rows: list[UsageEvent]) -> None:
    """Call after the rows are committed."""
    for r in rows:
        n = r.tokens_in + r.tokens_out
        keys = [_key(r.chat_id, r.day)]
        if r.user_id:
            keys.append(_key(r.chat_id, r.day, r.user_id))
        for k in keys:
            # Only bump counters that exist; a missing one is rebuilt from the DB on
            # next read (which already includes these committed rows).
            if await redis.exists(k):
                await redis.incrby(k, n)
                await redis.expire(k, COUNTER_TTL_S)


async def tokens_used(
    redis: Redis,
    db: AsyncSession,
    chat_id: uuid.UUID,
    day: dt.date | None = None,
    user_id: uuid.UUID | None = None,
) -> int:
    day = day or today()
    key = _key(chat_id, day, user_id)
    cached = await redis.get(key)
    if cached is not None:
        return int(cached)
    stmt = select(func.coalesce(func.sum(UsageEvent.tokens_in + UsageEvent.tokens_out), 0)).where(
        UsageEvent.chat_id == chat_id, UsageEvent.day == day
    )
    if user_id is not None:
        stmt = stmt.where(UsageEvent.user_id == user_id)
    total = int(await db.scalar(stmt) or 0)
    await redis.set(key, total, ex=COUNTER_TTL_S, nx=True)
    return total


# ------------------------------------------------------------------ message rate limit

_BUCKET = """
local key, cap, rate, now = KEYS[1], tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
local s = redis.call('hmget', key, 't', 'ts')
local tokens, ts = tonumber(s[1]) or cap, tonumber(s[2]) or now
tokens = math.min(cap, tokens + (now - ts) * rate)
local ok, wait = 0, 0
if tokens >= 1 then tokens = tokens - 1; ok = 1 else wait = (1 - tokens) / rate end
redis.call('hset', key, 't', tokens, 'ts', now)
redis.call('expire', key, math.ceil(cap / rate) + 60)
return {ok, tostring(wait)}
"""


async def take_message_token(
    redis: Redis,
    chat_id: uuid.UUID,
    user_id: uuid.UUID,
    per_minute: int,
    *,
    bucket: str = "msgrate",
) -> float | None:
    """Token bucket per user per chat (burst = per_minute). None if allowed, else
    seconds until the next message is allowed."""
    import time

    ok, wait = await redis.eval(
        _BUCKET, 1, f"{bucket}:{chat_id}:{user_id}", per_minute, per_minute / 60.0, time.time()
    )
    return None if int(ok) == 1 else float(wait)
