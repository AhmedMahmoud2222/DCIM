"""Minimal human alarm API; collectors only ever submit telemetry."""

import base64
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime, BaseModel, Field, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.alarm_service import acknowledge_alarm, disable_rule, resolve_rule_unit, rule_unit_status
from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.core.errors import NotFoundError
from app.domain.alarm.models import ALARM_STATUSES, RULE_TYPES, Alarm, AlarmRule
from app.domain.telemetry.models import TelemetryReading
from app.domain.telemetry.registry import METRIC_REGISTRY, REGISTRY_VERSION, convert_to_presentation, registry_snapshot, validate_metric_rule

router = APIRouter(prefix="/alarms", tags=["alarms"])


class AlarmRuleIn(BaseModel):
    registry_version: str = REGISTRY_VERSION
    integration_id: uuid.UUID
    metric: str = Field(max_length=64)
    rule_type: str
    threshold: float | None = None
    name: str = Field(max_length=128)


class AlarmRuleUpdateIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2048)
    expected_updated_at: AwareDatetime

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A nonblank reason is required.")
        return value.strip()


class AlarmRuleResolveIn(AlarmRuleUpdateIn):
    unit: str = Field(min_length=1, max_length=32)


class AlarmRuleOut(BaseModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    metric: str
    rule_type: str
    threshold: float | None
    unit: str | None
    registry_version: str | None
    enabled: bool
    name: str
    updated_at: datetime
    unit_status: str
    needs_attention: bool
    blocked_notified_at: datetime | None
    unit_resolved_by: uuid.UUID | None
    unit_resolved_at: datetime | None
    unit_resolution_reason: str | None


class AlarmRuleResolutionOut(AlarmRuleOut):
    retained_blocked_window_readings: int
    blocked_window_count_complete: bool = False
    retroactive_evaluation: bool = False


class AlarmOut(BaseModel):
    id: uuid.UUID
    rule_id: uuid.UUID
    integration_id: uuid.UUID
    managed_asset_id: uuid.UUID | None
    subject_key: str
    status: str
    opened_at: datetime
    acknowledged_at: datetime | None
    cleared_at: datetime | None
    last_value: float
    unit: str | None = None
    presentation_value: float
    presentation_unit: str | None = None


class AlarmHistoryPage(BaseModel):
    items: list[AlarmOut]
    next_cursor: str | None = None


@router.post("/rules", status_code=201)
async def create_alarm_rule(
    body: AlarmRuleIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("alarm:manage"))
) -> dict:
    if body.rule_type not in RULE_TYPES or ((body.rule_type == "availability_unavailable") != (body.threshold is None)):
        from app.core.errors import ApiError

        raise ApiError(status_code=422, title="Invalid alarm rule", detail="Rule type and threshold are inconsistent.")
    try:
        definition = registry_snapshot(body.registry_version).metrics.get(body.metric)
        if definition is not None:
            validate_metric_rule(body.metric, body.rule_type,
                                 Decimal(str(body.threshold)) if body.threshold is not None else None,
                                 definition.canonical_unit, registry_version=body.registry_version)
    except ValueError as error:
        from app.core.errors import ApiError
        raise ApiError(status_code=422, title="Invalid alarm semantics", detail=str(error)) from error
    if definition is None:
        from app.core.errors import ApiError
        raise ApiError(status_code=422, title="Invalid alarm metric", detail="Metric is not in the active registry.")
    rule = AlarmRule(id=uuid.uuid4(), unit=definition.canonical_unit,
                     **body.model_dump())
    db.add(rule)
    await db.flush()
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="alarm.rule.create",
        entity_type="alarm_rule",
        entity_id=rule.id,
        request_id=None,
        correlation_id=None,
        after={"integration_id": str(rule.integration_id), "metric": rule.metric},
    )
    await write_outbox_event(
        db,
        event_type="AlarmRuleCreated",
        aggregate_type="alarm_rule",
        aggregate_id=rule.id,
        payload={"rule_id": str(rule.id), "integration_id": str(rule.integration_id)},
    )
    await db.commit()
    return {"id": rule.id}


@router.get("/rules", response_model=list[AlarmRuleOut])
async def list_alarm_rules(
    needs_attention: bool = False, integration_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=500), offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("alarm:read")),
) -> list[AlarmRuleOut]:
    stmt = select(AlarmRule).order_by(AlarmRule.id).limit(limit).offset(offset)
    if needs_attention:
        stmt = stmt.where(AlarmRule.enabled.is_(True), AlarmRule.unit.is_(None))
    if integration_id is not None:
        stmt = stmt.where(AlarmRule.integration_id == integration_id)
    return [_rule_out(row) for row in (await db.execute(stmt)).scalars().all()]


async def _locked_rule(db: AsyncSession, rule_id: uuid.UUID) -> AlarmRule:
    rule = (await db.execute(select(AlarmRule).where(AlarmRule.id == rule_id).with_for_update()
                            .execution_options(populate_existing=True))).scalar_one_or_none()
    if rule is None:
        raise NotFoundError("Alarm rule not found.")
    return rule


@router.post("/rules/{rule_id}/resolve-unit", response_model=AlarmRuleResolutionOut)
async def resolve_alarm_rule_unit(
    rule_id: uuid.UUID, body: AlarmRuleResolveIn,
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("alarm:manage")),
) -> AlarmRuleResolutionOut:
    rule = await _locked_rule(db, rule_id)
    await resolve_rule_unit(db, rule=rule, unit=body.unit, reason=body.reason,
                           expected_updated_at=body.expected_updated_at, actor_user_id=ctx.user.id)
    count = 0
    if rule.blocked_notified_at is not None:
        # Count only retained raw readings received during the block window. Raw
        # expiry/late compaction can remove rows, so never present this as complete.
        end = rule.unit_resolved_at or datetime.now(UTC)
        count = (await db.execute(select(func.count()).select_from(TelemetryReading).where(
            TelemetryReading.integration_id == rule.integration_id,
            TelemetryReading.metric == rule.metric,
            TelemetryReading.received_at >= rule.blocked_notified_at,
            TelemetryReading.received_at <= end,
        ))).scalar_one()
    result = AlarmRuleResolutionOut(**_rule_out(rule).model_dump(), retained_blocked_window_readings=count)
    await db.commit()
    return result


@router.post("/rules/{rule_id}/disable", response_model=AlarmRuleOut)
async def disable_alarm_rule(
    rule_id: uuid.UUID, body: AlarmRuleUpdateIn,
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("alarm:manage")),
) -> AlarmRuleOut:
    rule = await _locked_rule(db, rule_id)
    await disable_rule(db, rule=rule, reason=body.reason, expected_updated_at=body.expected_updated_at,
                       actor_user_id=ctx.user.id)
    result = _rule_out(rule)
    await db.commit()
    return result


def _rule_out(rule: AlarmRule) -> AlarmRuleOut:
    return AlarmRuleOut(
        id=rule.id, integration_id=rule.integration_id, metric=rule.metric, rule_type=rule.rule_type,
        threshold=float(rule.threshold) if rule.threshold is not None else None,
        unit=rule.unit, registry_version=rule.registry_version, enabled=rule.enabled, name=rule.name,
        updated_at=rule.updated_at, unit_status=rule_unit_status(rule),
        needs_attention=rule.enabled and rule.unit is None, blocked_notified_at=rule.blocked_notified_at,
        unit_resolved_by=rule.unit_resolved_by, unit_resolved_at=rule.unit_resolved_at,
        unit_resolution_reason=rule.unit_resolution_reason,
    )


@router.get("", response_model=list[AlarmOut])
async def list_alarms(
    status: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("alarm:read")),
) -> list[AlarmOut]:
    if status is not None and status not in ALARM_STATUSES:
        from app.core.errors import ApiError

        raise ApiError(status_code=422, title="Invalid alarm status", detail="Unknown alarm status.")
    stmt = select(Alarm).order_by(Alarm.opened_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Alarm.status == status)
    return [_out(row) for row in (await db.execute(stmt)).scalars().all()]


@router.get("/history", response_model=AlarmHistoryPage)
async def alarm_history(
    status: str | None = Query(default=None),
    integration_id: uuid.UUID | None = None,
    managed_asset_id: uuid.UUID | None = None,
    rule_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("alarm:read")),
) -> AlarmHistoryPage:
    """Keyset history for presets or arbitrary retained ranges; no alarm downsampling."""
    stmt = select(Alarm).order_by(Alarm.opened_at.desc(), Alarm.id.desc()).limit(limit + 1)
    if status is not None:
        if status not in ALARM_STATUSES:
            from app.core.errors import ApiError

            raise ApiError(status_code=422, title="Invalid alarm status", detail="Unknown alarm status.")
        stmt = stmt.where(Alarm.status == status)
    for column, value in (
        (Alarm.integration_id, integration_id),
        (Alarm.managed_asset_id, managed_asset_id),
        (Alarm.rule_id, rule_id),
    ):
        if value is not None:
            stmt = stmt.where(column == value)
    if start is not None:
        stmt = stmt.where(Alarm.opened_at >= start)
    if end is not None:
        stmt = stmt.where(Alarm.opened_at <= end)
    if cursor is not None:
        cursor_opened_at, cursor_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                Alarm.opened_at < cursor_opened_at,
                and_(Alarm.opened_at == cursor_opened_at, Alarm.id < cursor_id),
            )
        )
    rows = (await db.execute(stmt)).scalars().all()
    page, has_more = rows[:limit], len(rows) > limit
    next_cursor = _encode_cursor(page[-1]) if has_more and page else None
    return AlarmHistoryPage(items=[_out(row) for row in page], next_cursor=next_cursor)


@router.post("/{alarm_id}/acknowledge", response_model=AlarmOut)
async def acknowledge(
    alarm_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("alarm:manage"))
) -> AlarmOut:
    alarm = (await db.execute(select(Alarm).where(Alarm.id == alarm_id).with_for_update())).scalar_one_or_none()
    if alarm is None:
        raise NotFoundError("Alarm not found.")
    await acknowledge_alarm(db, alarm=alarm, actor_user_id=ctx.user.id)
    await db.commit()
    return _out(alarm)


def _out(row: Alarm) -> AlarmOut:
    unit = row.details.get("unit")
    version = row.details.get("registry_version")
    presentation = None
    metric = row.details.get("metric")
    if metric and unit and version:
        presentation = convert_to_presentation(metric, row.last_value, unit, registry_version=version)
    return AlarmOut(
        id=row.id,
        rule_id=row.rule_id,
        integration_id=row.integration_id,
        managed_asset_id=row.managed_asset_id,
        subject_key=row.subject_key,
        status=row.status,
        opened_at=row.opened_at,
        acknowledged_at=row.acknowledged_at,
        cleared_at=row.cleared_at,
        last_value=float(row.last_value),
        unit=unit,
        presentation_value=float(presentation.value) if presentation is not None else float(row.last_value),
        presentation_unit=presentation.unit if presentation is not None else unit,
    )


def _encode_cursor(alarm: Alarm) -> str:
    payload = json.dumps({"opened_at": alarm.opened_at.isoformat(), "id": str(alarm.id)}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return datetime.fromisoformat(payload["opened_at"]), uuid.UUID(payload["id"])
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        from app.core.errors import ApiError

        raise ApiError(status_code=422, title="Invalid history cursor", detail="Cursor is malformed.") from error
