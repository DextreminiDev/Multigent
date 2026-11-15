"""Liveness / readiness probes (§15)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

router = APIRouter(tags=["meta"])


class Health(BaseModel):
    status: str


class Readiness(BaseModel):
    status: str
    postgres: bool
    redis: bool


@router.get("/healthz", response_model=Health)
async def healthz() -> Health:
    return Health(status="ok")


@router.get("/readyz", response_model=Readiness)
async def readyz(request: Request, response: Response) -> Readiness:
    async def check_pg() -> bool:
        try:
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def check_redis() -> bool:
        try:
            return bool(await request.app.state.redis.ping())
        except Exception:
            return False

    pg_ok, redis_ok = await asyncio.gather(
        asyncio.wait_for(check_pg(), 3),
        asyncio.wait_for(check_redis(), 3),
        return_exceptions=True,
    )
    pg = pg_ok is True
    rd = redis_ok is True
    ok = pg and rd
    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return Readiness(status="ok" if ok else "unavailable", postgres=pg, redis=rd)
