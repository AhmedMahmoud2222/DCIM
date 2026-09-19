"""Persisted monitoring-policy settings; edits never trigger immediate cleanup."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.audit_service import write_audit_log
from app.application.rbac import require_permission
from app.domain.telemetry.models import MonitoringPolicy

router = APIRouter(prefix="/settings", tags=["settings"])
ALLOWED_INTERVALS = {60, 180, 300, 600, 900, 1800}

class MonitoringPolicyOut(BaseModel):
    default_poll_interval_seconds: int
    raw_retention_days: int = Field(ge=1)
    daily_aggregate_retention_days: int | None = Field(default=None, ge=1)
    alarm_history_retention_days: int | None = Field(default=None, ge=1)

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
    if body.default_poll_interval_seconds not in ALLOWED_INTERVALS:
        from app.core.errors import ApiError
        raise ApiError(status_code=422, title="Invalid monitoring policy", detail="Unsupported interval or retention.")
    policy = await db.get(MonitoringPolicy, 1)
    assert policy is not None
    before = {
        "default_poll_interval_seconds": policy.default_poll_interval_seconds,
        "raw_retention_days": policy.raw_retention_days,
        "daily_aggregate_retention_days": policy.daily_aggregate_retention_days,
        "alarm_history_retention_days": policy.alarm_history_retention_days,
    }
    for key, value in body.model_dump().items():
        setattr(policy, key, value)
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="monitoring.policy.update",
        entity_type="monitoring_policy",
        entity_id=None,
        request_id=None,
        correlation_id=None,
        before=before,
        after=body.model_dump(),
    )
    await db.commit()
    return policy
