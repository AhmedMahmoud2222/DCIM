"""Bounded read APIs and collector-authenticated telemetry ingestion."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.application.telemetry_service import MetricMappingNotFound, ingest_reading
from app.core.errors import ApiError
from app.domain.integration.models import Collector
from app.domain.telemetry.models import CANONICAL_METRICS, IntegrationMetricMapping, TelemetryReading

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
MAX_HISTORY_POINTS = 1000
MAX_BATCH_RECORDS = 500


class TelemetryRecordIn(BaseModel):
    dedup_key: str = Field(max_length=255)
    integration_id: uuid.UUID
    external_identifier: str = Field(max_length=255)
    source_identifier: str = Field(max_length=255)
    occurred_at: datetime
    value: float
    attributes: dict = Field(default_factory=dict)


class TelemetryBatchIn(BaseModel):
    records: list[TelemetryRecordIn] = Field(max_length=MAX_BATCH_RECORDS)


class TelemetryAck(BaseModel):
    dedup_key: str
    status: str
    error: str | None = None


class TelemetryBatchOut(BaseModel):
    results: list[TelemetryAck]


class TelemetryOut(BaseModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    external_identifier: str
    metric: str
    unit: str
    value: float
    occurred_at: datetime
    received_at: datetime


class MetricMappingIn(BaseModel):
    integration_id: uuid.UUID
    source_identifier: str = Field(max_length=255)
    canonical_metric: str
    unit: str = Field(max_length=32)
    scale: float = 1
    label: str | None = Field(default=None, max_length=128)


class MetricMappingOut(MetricMappingIn):
    id: uuid.UUID


@router.post("/mappings", response_model=MetricMappingOut, status_code=201)
async def create_metric_mapping(
    body: MetricMappingIn, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("telemetry:manage")),
) -> MetricMappingOut:
    if body.canonical_metric not in CANONICAL_METRICS:
        raise ApiError(status_code=422, title="Invalid canonical metric", detail="Metric is not supported by the MVP catalog.")
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), **body.model_dump())
    db.add(mapping)
    await db.flush()
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="telemetry.mapping.create", entity_type="integration_metric_mapping",
        entity_id=mapping.id, request_id=None, correlation_id=None,
        after={"integration_id": str(mapping.integration_id), "canonical_metric": mapping.canonical_metric},
    )
    await write_outbox_event(
        db, event_type="MetricMappingCreated", aggregate_type="integration_metric_mapping", aggregate_id=mapping.id,
        payload={"integration_id": str(mapping.integration_id), "canonical_metric": mapping.canonical_metric},
    )
    await db.commit()
    return MetricMappingOut(id=mapping.id, **body.model_dump())


@router.get("/mappings", response_model=list[MetricMappingOut])
async def list_metric_mappings(
    integration_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:read")),
) -> list[MetricMappingOut]:
    stmt = select(IntegrationMetricMapping).order_by(IntegrationMetricMapping.created_at)
    if integration_id is not None:
        stmt = stmt.where(IntegrationMetricMapping.integration_id == integration_id)
    return [MetricMappingOut(id=row.id, integration_id=row.integration_id, source_identifier=row.source_identifier,
        canonical_metric=row.canonical_metric, unit=row.unit, scale=float(row.scale), label=row.label)
        for row in (await db.execute(stmt)).scalars().all()]


async def ingest_collector_telemetry(
    db: AsyncSession, *, collector: Collector, body: TelemetryBatchIn,
) -> TelemetryBatchOut:
    """Per-record ACKs retain unacknowledged edge-queue records for retry."""
    from app.application.collector_service import current_assignment

    results: list[TelemetryAck] = []
    for record in body.records:
        assignment = await current_assignment(db, record.integration_id)
        if assignment is None or assignment.collector_id != collector.id:
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="NOT_ASSIGNED"))
            continue
        try:
            outcome = await ingest_reading(
                db, collector_id=collector.id, integration_id=record.integration_id,
                dedup_key=record.dedup_key, external_identifier=record.external_identifier,
                source_identifier=record.source_identifier, occurred_at=record.occurred_at,
                value=record.value, attributes=record.attributes,
            )
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="duplicate" if outcome.duplicate else "accepted"))
        except MetricMappingNotFound:
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="UNKNOWN_METRIC_MAPPING"))
    await db.commit()
    return TelemetryBatchOut(results=results)


@router.get("/latest", response_model=list[TelemetryOut])
async def latest_readings(
    integration_id: uuid.UUID | None = None, metric: str | None = None,
    limit: int = Query(default=100, ge=1, le=MAX_HISTORY_POINTS),
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("telemetry:read")),
) -> list[TelemetryOut]:
    stmt = select(TelemetryReading).order_by(TelemetryReading.occurred_at.desc()).limit(limit)
    if integration_id is not None:
        stmt = stmt.where(TelemetryReading.integration_id == integration_id)
    if metric is not None:
        stmt = stmt.where(TelemetryReading.metric == metric)
    rows = (await db.execute(stmt)).scalars().all()
    return [_out(row) for row in rows]


@router.get("/history", response_model=list[TelemetryOut])
async def metric_history(
    integration_id: uuid.UUID, metric: str, start: datetime, end: datetime,
    limit: int = Query(default=500, ge=1, le=MAX_HISTORY_POINTS),
    db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("telemetry:read")),
) -> list[TelemetryOut]:
    if start >= end:
        raise ApiError(status_code=422, title="Invalid time range", detail="start must be before end.")
    stmt = select(TelemetryReading).where(
        TelemetryReading.integration_id == integration_id,
        TelemetryReading.metric == metric,
        TelemetryReading.occurred_at >= start,
        TelemetryReading.occurred_at <= end,
    ).order_by(TelemetryReading.occurred_at.asc()).limit(limit)
    return [_out(row) for row in (await db.execute(stmt)).scalars().all()]


def _out(row: TelemetryReading) -> TelemetryOut:
    return TelemetryOut(id=row.id, integration_id=row.integration_id, external_identifier=row.external_identifier,
        metric=row.metric, unit=row.unit, value=float(row.value), occurred_at=row.occurred_at, received_at=row.received_at)
