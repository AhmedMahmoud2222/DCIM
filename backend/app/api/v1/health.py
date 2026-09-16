"""Liveness/readiness split (§22 of the Phase 1 prompt): liveness never depends on the
database or Redis (a dependency outage must not make the process look crash-worthy to an
orchestrator); readiness reports each dependency's actual state rather than collapsing
everything into one boolean."""

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.db.health import check_database, check_redis

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict:
    return {"status": "alive"}


@router.get("/ready")
async def readiness(response: Response, db: AsyncSession = Depends(get_db)) -> dict:
    db_ok = await check_database(db)
    redis_ok = await check_redis()
    overall = "ready" if (db_ok and redis_ok) else "degraded"
    # Finding L3 (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md / PHASE1_CORRECTION_REPORT.md):
    # a degraded response must not be HTTP 200 — orchestrator/load-balancer readiness
    # probes conventionally act on status code, not response body, so a 200 here would
    # keep routing traffic to an instance that just reported itself not ready.
    if overall != "ready":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": overall,
        "dependencies": {
            "database": "up" if db_ok else "down",
            "redis": "up" if redis_ok else "down",
        },
    }
