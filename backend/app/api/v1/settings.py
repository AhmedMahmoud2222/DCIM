"""Persisted monitoring-policy settings; edits never trigger immediate cleanup."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.audit_service import write_audit_log
from app.application.lifecycle_settings import (
    LifecycleSettingsRead,
    LifecycleSettingsUpdate,
    build_lifecycle_settings_read,
    effective_states_from_row,
    get_effective_lifecycle_states,
    lock_lifecycle_settings_row,
    validate_lifecycle_states_update,
)
from app.application.rbac import require_catalog_administrator, require_permission
from app.domain.identity.models import default_lifecycle_states_config
from app.domain.telemetry.models import MonitoringPolicy

router = APIRouter(prefix="/settings", tags=["settings"])
ALLOWED_INTERVALS = {60, 180, 300, 600, 900, 1800}

class MonitoringPolicyOut(BaseModel):
    default_poll_interval_seconds: int
    raw_retention_days: int = Field(ge=1)
    daily_aggregate_retention_days: int | None = Field(default=None, ge=1)
    alarm_history_retention_days: int | None = Field(default=None, ge=1)
    # Issue #128 / G4-R. NULL = no age-based rejection of late data and no ledger pruning (the ledger then grows without
    # bound). A finite value is an owner decision made from representative delay profiling. An omitted field on PUT
    # leaves the stored value unchanged, so an older client cannot clear it by accident.
    late_replay_horizon_days: int | None = Field(default=None, ge=1)
    # Issue #128 / G6. NULL = no hold-expiry policy (resolved lineage evidence is kept indefinitely); DB default is
    # 365. Same omitted-field-preserves-current-value contract as late_replay_horizon_days above, for the same
    # reason: this field is new, so an older client's PUT body must not silently reset an owner's explicit policy.
    resolved_hold_retention_days: int | None = Field(default=None, ge=1)

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
        "late_replay_horizon_days": policy.late_replay_horizon_days,
        "resolved_hold_retention_days": policy.resolved_hold_retention_days,
    }
    changes = body.model_dump(exclude_unset=False)
    if "late_replay_horizon_days" not in body.model_fields_set:
        changes.pop("late_replay_horizon_days")
    if "resolved_hold_retention_days" not in body.model_fields_set:
        changes.pop("resolved_hold_retention_days")
    if "late_replay_horizon_days" in changes and changes["late_replay_horizon_days"] is None:
        if policy.late_replay_horizon_days is not None:
            from app.application.telemetry_ledger import UnsafeHorizonClear, guard_horizon_clear
            from app.core.errors import ApiError
            try:
                await guard_horizon_clear(db, now=datetime.now(UTC))
            except UnsafeHorizonClear as error:
                raise ApiError(status_code=409, title="Replay horizon cannot be cleared", detail=str(error)) from error
    for key, value in changes.items():
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
        after={**before, **changes},
    )
    await db.commit()
    return policy


@router.get("/lifecycle", response_model=LifecycleSettingsRead)
async def get_lifecycle_settings(
    db: AsyncSession = Depends(get_db),
    _ctx=Depends(require_permission("managed_asset:read")),
) -> LifecycleSettingsRead:
    states = await get_effective_lifecycle_states(db)
    return build_lifecycle_settings_read(states)


@router.put("/lifecycle", response_model=LifecycleSettingsRead)
async def update_lifecycle_settings(
    body: LifecycleSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_catalog_administrator("managed_asset:manage")),
) -> LifecycleSettingsRead:
    validated_states = validate_lifecycle_states_update(body.states)
    # Lock the singleton row so concurrent updates serialize and the audit "before" is the true prior state.
    row = await lock_lifecycle_settings_row(db)
    before_states = effective_states_from_row(row)
    row.states_config = validated_states
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="settings.lifecycle.update",
        entity_type="lifecycle_settings",
        entity_id=None,
        request_id=None,
        correlation_id=None,
        before={"states": before_states},
        after={"states": validated_states},
    )
    await db.commit()
    return build_lifecycle_settings_read(validated_states)


@router.post("/lifecycle/restore-defaults", response_model=LifecycleSettingsRead)
async def restore_default_lifecycle_settings(
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_catalog_administrator("managed_asset:manage")),
) -> LifecycleSettingsRead:
    row = await lock_lifecycle_settings_row(db)
    before_states = effective_states_from_row(row)
    default_states = default_lifecycle_states_config()
    row.states_config = default_states
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="settings.lifecycle.restore_defaults",
        entity_type="lifecycle_settings",
        entity_id=None,
        request_id=None,
        correlation_id=None,
        before={"states": before_states},
        after={"states": default_states},
    )
    await db.commit()
    return build_lifecycle_settings_read(default_states)
