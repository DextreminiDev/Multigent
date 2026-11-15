"""Server-side sessions in Redis (ARCHITECTURE.md §11.1).

- Cookie `sid` holds 256 random bits. Redis stores only sha256(sid), so a Redis
  dump can't be replayed as cookies.
- Sliding TTL; per-user index allows "log out everywhere" (account deletion etc).
- CSRF token = HMAC(SESSION_SECRET, sid): stateless double-submit token, handed
  to the SPA via GET /me and required as X-CSRF-Token on state-changing routes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid

from redis.asyncio import Redis

SESSION_COOKIE = "sid"


def _hash(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()


def _key(sid: str) -> str:
    return f"sess:{_hash(sid)}"


def _user_index(user_id: uuid.UUID) -> str:
    return f"user_sessions:{user_id}"


def csrf_token(secret: str, sid: str) -> str:
    return hmac.new(secret.encode(), f"csrf:{sid}".encode(), hashlib.sha256).hexdigest()


def csrf_valid(secret: str, sid: str, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(csrf_token(secret, sid), token or "")


class SessionStore:
    def __init__(self, redis: Redis, ttl_s: int) -> None:
        self.redis = redis
        self.ttl_s = ttl_s

    async def create(self, user_id: uuid.UUID) -> str:
        sid = secrets.token_urlsafe(32)
        now = int(time.time())
        async with self.redis.pipeline(transaction=True) as p:
            p.hset(_key(sid), mapping={"user_id": str(user_id), "created": now, "seen": now})
            p.expire(_key(sid), self.ttl_s)
            p.sadd(_user_index(user_id), _hash(sid))
            p.expire(_user_index(user_id), self.ttl_s)
            await p.execute()
        return sid

    async def get(self, sid: str) -> uuid.UUID | None:
        raw = await self.redis.hget(_key(sid), "user_id")
        if not raw:
            return None
        async with self.redis.pipeline(transaction=False) as p:
            p.hset(_key(sid), "seen", int(time.time()))
            p.expire(_key(sid), self.ttl_s)
            await p.execute()
        return uuid.UUID(str(raw))

    async def delete(self, sid: str) -> None:
        uid = await self.redis.hget(_key(sid), "user_id")
        async with self.redis.pipeline(transaction=True) as p:
            p.delete(_key(sid))
            if uid:
                p.srem(_user_index(uuid.UUID(str(uid))), _hash(sid))
            await p.execute()

    async def delete_all_for_user(self, user_id: uuid.UUID) -> int:
        hashes = await self.redis.smembers(_user_index(user_id))
        async with self.redis.pipeline(transaction=True) as p:
            for h in hashes:
                p.delete(f"sess:{h!s}")
            p.delete(_user_index(user_id))
            await p.execute()
        return len(hashes)
