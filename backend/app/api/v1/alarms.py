"""Minimal human alarm API; collectors only ever submit telemetry."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.alarm_service import acknowledge_alarm
from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.domain.alarm.models import ALARM_STATUSES, RULE_TYPES, Alarm, AlarmRule

router = APIRouter(prefix="/alarms", tags=["alarms"])


class AlarmRuleIn(BaseModel):
    integration_id: uuid.UUID
    metric: str = Field(max_length=64)
    rule_type: str
    threshold: float | None = None
    name: str = Field(max_length=128)


class AlarmOut(BaseModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    integration_id: uuid.UUID
    subject_key: str
    status: str
    opened_at: datetime
    acknowledged_at: datetime | None
    cleared_at: datetime | None
    last_value: float


@router.post("/rules", status_code=201)
async def create_alarm_rule(body: AlarmRuleIn, db: AsyncSession = Depends(get_db),
                            ctx=Depends(require_permission("alarm:manage"))) -> dict:
    if body.rule_type not in RULE_TYPES or ((body.rule_type == "availability_unavailable") != (body.threshold is None)):
        from app.core.errors import ApiError
        raise ApiError(status_code=422, title="Invalid alarm rule", detail="Rule type and threshold are inconsistent.")
    rule = AlarmRule(id=uuid.uuid4(), **body.model_dump())
    db.add(rule)
    await db.flush()
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="alarm.rule.create", entity_type="alarm_rule", entity_id=rule.id,
        request_id=None, correlation_id=None, after={"integration_id": str(rule.integration_id), "metric": rule.metric},
    )
    await write_outbox_event(
        db, event_type="AlarmRuleCreated", aggregate_type="alarm_rule", aggregate_id=rule.id,
        payload={"rule_id": str(rule.id), "integration_id": str(rule.integration_id)},
    )
    await db.commit()
    return {"id": rule.id}


@router.get("", response_model=list[AlarmOut])
async def list_alarms(status: str | None = Query(default=None), limit: int = Query(default=100, ge=1, le=1000),
                      db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("alarm:read"))) -> list[AlarmOut]:
    if status is not None and status not in ALARM_STATUSES:
        from app.core.errors import ApiError
        raise ApiError(status_code=422, title="Invalid alarm status", detail="Unknown alarm status.")
    stmt = select(Alarm).order_by(Alarm.opened_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Alarm.status == status)
    return [_out(row) for row in (await db.execute(stmt)).scalars().all()]


@router.post("/{alarm_id}/acknowledge", response_model=AlarmOut)
async def acknowledge(alarm_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                      ctx=Depends(require_permission("alarm:manage"))) -> AlarmOut:
    alarm = (await db.execute(select(Alarm).where(Alarm.id == alarm_id).with_for_update())).scalar_one_or_none()
    if alarm is None:
        raise NotFoundError("Alarm not found.")
    await acknowledge_alarm(db, alarm=alarm, actor_user_id=ctx.user.id)
    await db.commit()
    return _out(alarm)


def _out(row: Alarm) -> AlarmOut:
    return AlarmOut(id=row.id, rule_id=row.rule_id, integration_id=row.integration_id, subject_key=row.subject_key,
                    status=row.status, opened_at=row.opened_at, acknowledged_at=row.acknowledged_at,
                    cleared_at=row.cleared_at, last_value=float(row.last_value))
