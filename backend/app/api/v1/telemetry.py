"""Bounded read APIs and collector-authenticated telemetry ingestion."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.application.telemetry_service import MetricMappingNotFound, ingest_reading
from app.core.errors import ApiError
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import CANONICAL_METRICS, DailyTelemetryAggregate, IntegrationMetricMapping, TelemetryReading

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
    managed_asset_id: uuid.UUID | None
    external_identifier: str
    metric: str
    unit: str
    value: float
    occurred_at: datetime
    received_at: datetime
    expected_poll_interval_seconds: int | None = None


class TelemetryHistoryOut(TelemetryOut):
    resolution: str = "raw"
    minimum_value: float | None = None
    maximum_value: float | None = None
    sample_count: int | None = None


class MetricMappingIn(BaseModel):
    integration_id: uuid.UUID
    managed_asset_id: uuid.UUID | None = None
    source_identifier: str = Field(max_length=255)
    canonical_metric: str
    unit: str = Field(max_length=32)
    scale: float = 1
    label: str | None = Field(default=None, max_length=128)


class MetricMappingOut(MetricMappingIn):
    id: uuid.UUID


@router.post("/mappings", response_model=MetricMappingOut, status_code=201)
async def create_metric_mapping(
    body: MetricMappingIn,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:manage")),
) -> MetricMappingOut:
    if body.canonical_metric not in CANONICAL_METRICS:
        raise ApiError(status_code=422, title="Invalid canonical metric", detail="Metric is not supported by the MVP catalog.")
    if body.managed_asset_id is not None and await db.get(ManagedAsset, body.managed_asset_id) is None:
        raise ApiError(status_code=422, title="Invalid managed asset", detail="managed_asset_id does not exist.")
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), **body.model_dump())
    db.add(mapping)
    await db.flush()
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="telemetry.mapping.create",
        entity_type="integration_metric_mapping",
        entity_id=mapping.id,
        request_id=None,
        correlation_id=None,
        after={"integration_id": str(mapping.integration_id), "canonical_metric": mapping.canonical_metric},
    )
    await write_outbox_event(
        db,
        event_type="MetricMappingCreated",
        aggregate_type="integration_metric_mapping",
        aggregate_id=mapping.id,
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
    return [
        MetricMappingOut(
            id=row.id,
            integration_id=row.integration_id,
            managed_asset_id=row.managed_asset_id,
            source_identifier=row.source_identifier,
            canonical_metric=row.canonical_metric,
            unit=row.unit,
            scale=float(row.scale),
            label=row.label,
        )
        for row in (await db.execute(stmt)).scalars().all()
    ]


async def ingest_collector_telemetry(
    db: AsyncSession,
    *,
    collector: Collector,
    body: TelemetryBatchIn,
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
                db,
                collector_id=collector.id,
                integration_id=record.integration_id,
                dedup_key=record.dedup_key,
                external_identifier=record.external_identifier,
                source_identifier=record.source_identifier,
                occurred_at=record.occurred_at,
                value=record.value,
                attributes=record.attributes,
            )
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="duplicate" if outcome.duplicate else "accepted"))
        except MetricMappingNotFound:
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="UNKNOWN_METRIC_MAPPING"))
    await db.commit()
    return TelemetryBatchOut(results=results)


@router.get("/latest", response_model=list[TelemetryOut])
async def latest_readings(
    integration_id: uuid.UUID | None = None,
    managed_asset_id: uuid.UUID | None = None,
    metric: str | None = None,
    limit: int = Query(default=100, ge=1, le=MAX_HISTORY_POINTS),
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:read")),
) -> list[TelemetryOut]:
    # Rank *after* filtering, so limit means "latest series returned", never
    # "raw rows inspected".  `series_key` is the canonical durable telemetry
    # identity shared with retention.  UUID closes otherwise exact timestamp ties.
    ranked = select(
        TelemetryReading.id.label("reading_id"),
        func.row_number().over(
            partition_by=TelemetryReading.series_key,
            order_by=(
                TelemetryReading.occurred_at.desc(),
                TelemetryReading.received_at.desc(),
                TelemetryReading.id.desc(),
            ),
        ).label("series_rank"),
    )
    if integration_id is not None:
        ranked = ranked.where(TelemetryReading.integration_id == integration_id)
    if managed_asset_id is not None:
        ranked = ranked.where(TelemetryReading.managed_asset_id == managed_asset_id)
    if metric is not None:
        ranked = ranked.where(TelemetryReading.metric == metric)
    ranked_subquery = ranked.subquery()

    # Poll cadence comes from the authoritative integration configuration in the
    # same bounded query; the UI must not guess a global interval or issue N+1 reads.
    stmt = (
        select(TelemetryReading, Integration.poll_interval_seconds)
        .join(Integration, Integration.id == TelemetryReading.integration_id)
        .join(ranked_subquery, ranked_subquery.c.reading_id == TelemetryReading.id)
        .where(ranked_subquery.c.series_rank == 1)
        .order_by(TelemetryReading.occurred_at.desc(), TelemetryReading.received_at.desc(), TelemetryReading.id.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [_out(row, poll_interval_seconds=interval) for row, interval in rows]


@router.get("/history", response_model=list[TelemetryHistoryOut])
async def metric_history(
    metric: str,
    start: datetime,
    end: datetime,
    integration_id: uuid.UUID | None = None,
    managed_asset_id: uuid.UUID | None = None,
    limit: int = Query(default=500, ge=1, le=MAX_HISTORY_POINTS),
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:read")),
) -> list[TelemetryHistoryOut]:
    if integration_id is None and managed_asset_id is None:
        raise ApiError(
            status_code=422, title="Telemetry series required", detail="integration_id or managed_asset_id is required."
        )
    if start >= end:
        raise ApiError(status_code=422, title="Invalid time range", detail="start must be before end.")
    cutoff = datetime.now(UTC) - timedelta(days=365)
    raw_stmt = (
        select(TelemetryReading)
        .where(
            TelemetryReading.metric == metric,
            TelemetryReading.occurred_at >= start,
            TelemetryReading.occurred_at <= end,
            TelemetryReading.occurred_at >= cutoff,
        )
        .order_by(TelemetryReading.occurred_at.asc())
        .limit(limit)
    )
    if integration_id is not None:
        raw_stmt = raw_stmt.where(TelemetryReading.integration_id == integration_id)
    if managed_asset_id is not None:
        raw_stmt = raw_stmt.where(TelemetryReading.managed_asset_id == managed_asset_id)
    daily_stmt = (
        select(DailyTelemetryAggregate)
        .where(
            DailyTelemetryAggregate.metric == metric,
            DailyTelemetryAggregate.day >= start.date(),
            DailyTelemetryAggregate.day <= end.date(),
            DailyTelemetryAggregate.day < cutoff.date(),
        )
        .order_by(DailyTelemetryAggregate.day.asc())
        .limit(limit)
    )
    if integration_id is not None:
        daily_stmt = daily_stmt.where(DailyTelemetryAggregate.integration_id == integration_id)
    if managed_asset_id is not None:
        daily_stmt = daily_stmt.where(DailyTelemetryAggregate.managed_asset_id == managed_asset_id)
    raw = [_history_raw(row) for row in (await db.execute(raw_stmt)).scalars().all()]
    daily = [_history_daily(row) for row in (await db.execute(daily_stmt)).scalars().all()]
    return sorted([*daily, *raw], key=lambda item: item.occurred_at)[:limit]


def _out(row: TelemetryReading, *, poll_interval_seconds: int | None = None) -> TelemetryOut:
    return TelemetryOut(
        id=row.id,
        integration_id=row.integration_id,
        managed_asset_id=row.managed_asset_id,
        external_identifier=row.external_identifier,
        metric=row.metric,
        unit=row.unit,
        value=float(row.value),
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        expected_poll_interval_seconds=poll_interval_seconds,
    )


def _history_raw(row: TelemetryReading) -> TelemetryHistoryOut:
    return TelemetryHistoryOut(**_out(row).model_dump(), resolution="raw")


def _history_daily(row: DailyTelemetryAggregate) -> TelemetryHistoryOut:
    occurred_at = datetime.combine(row.day, datetime.min.time(), tzinfo=UTC)
    return TelemetryHistoryOut(
        id=row.id,
        integration_id=row.integration_id,
        managed_asset_id=row.managed_asset_id,
        external_identifier=row.external_identifier,
        metric=row.metric,
        unit=row.unit,
        value=float(row.average_value),
        occurred_at=occurred_at,
        received_at=occurred_at,
        resolution="daily",
        minimum_value=float(row.minimum_value),
        maximum_value=float(row.maximum_value),
        sample_count=row.sample_count,
    )
