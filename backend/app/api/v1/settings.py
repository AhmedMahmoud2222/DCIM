"""Persisted monitoring-policy settings; edits never trigger immediate cleanup."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.rbac import require_permission
from app.domain.telemetry.models import MonitoringPolicy

router = APIRouter(prefix="/settings", tags=["settings"])
ALLOWED_INTERVALS = {60, 180, 300, 600, 900, 1800}

class MonitoringPolicyOut(BaseModel):
    default_poll_interval_seconds: int
    raw_retention_days: int
    daily_aggregate_retention_days: int | None
    alarm_history_retention_days: int | None

@router.get("/monitoring", response_model=MonitoringPolicyOut)
async def get_monitoring_policy(
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("telemetry:read"))
) -> MonitoringPolicy:
    policy = await db.get(MonitoringPolicy, 1)
    assert policy is not None
    return policy

@router.put("/monitoring", response_model=MonitoringPolicyOut)
async def update_monitoring_policy(
    body: MonitoringPolicyOut,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:manage")),
) -> MonitoringPolicy:
    if body.default_poll_interval_seconds not in ALLOWED_INTERVALS or body.raw_retention_days < 1:
        from app.core.errors import ApiError
        raise ApiError(status_code=422, title="Invalid monitoring policy", detail="Unsupported interval or retention.")
    policy = await db.get(MonitoringPolicy, 1)
    assert policy is not None
    for key, value in body.model_dump().items():
        setattr(policy, key, value)
    await db.commit()
    return policy
