"""Liveness/readiness split (§22 of the Phase 1 prompt): liveness never depends on the
database or Redis (a dependency outage must not make the process look crash-worthy to an
orchestrator); readiness reports each dependency's actual state rather than collapsing
everything into one boolean."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.health import check_database, check_redis

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict:
    return {"status": "alive"}


@router.get("/ready")
async def readiness(db: AsyncSession = Depends(get_db)) -> dict:
    db_ok = await check_database(db)
    redis_ok = await check_redis()
    overall = "ready" if (db_ok and redis_ok) else "degraded"
    return {
        "status": overall,
        "dependencies": {
            "database": "up" if db_ok else "down",
            "redis": "up" if redis_ok else "down",
        },
    }
