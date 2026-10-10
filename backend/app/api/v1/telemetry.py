"""Bounded read APIs and collector-authenticated telemetry ingestion.

Phase 10C additions (below the MVP `/mappings`/`/latest`/`/history` routes): port/power-
inlet telemetry bindings and their cached latest status, under `/telemetry/bindings` and
`/telemetry/port-status/*`. Deliberately not layered onto the existing `/telemetry/latest`
path above — that route's `TelemetryOut` shape and `integration_id`/`managed_asset_id`
query contract are the MVP acquisition pipeline's own, unrelated to Phase 10B's
instantiated port/inlet identity, and overloading one path with two unrelated response
shapes would be worse than the small amount of namespacing added here (the same
"documented, explicit deviation" this codebase already prefers — see e.g.
power/models.py's `utility_intake` node-type addition)."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field, PrivateAttr, field_validator
from sqlalchemy import DateTime, and_, cast, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.alarm_service import AlarmUnitCompatibilityError
from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.application.telemetry_retention import as_utc, utc_day
from app.application.telemetry_service import (
    AmbiguousMappingContract,
    BindingNotFound,
    ConversionContractDrift,
    InvalidBindingTarget,
    LatestPortStatus,
    MappingRevisionMismatch,
    MetricMappingNotFound,
    UnknownMappingRevision,
    create_port_telemetry_binding,
    get_latest_status_for_equipment,
    get_latest_status_for_rack,
    ingest_reading,
    list_port_telemetry_bindings,
    mark_hold_resolved_if_any,
    record_latest_status,
    register_contract_hold,
)
from app.core.errors import ApiError
from app.domain.cooling.models import SENSOR_KIND_METRICS, EnvironmentalSensor
from app.domain.identity.models import ManagedAsset
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.mapping_models import TELEMETRY_PROTOCOLS, TELEMETRY_TARGET_TYPES, PortTelemetryBinding
from app.domain.telemetry.models import (
    CANONICAL_METRICS,
    DailyTelemetryAggregate,
    IntegrationMetricMapping,
    TelemetryContractHold,
    TelemetryReading,
)
from app.domain.telemetry.numeric import (
    MAX_ABS_ADJUSTED_EXPONENT,
    MAX_STORABLE,
    NUMERIC_SCALE,
    InvalidTelemetryValue,
)
from app.domain.telemetry.registry import (
    METRIC_REGISTRY,
    REGISTRY_VERSION,
    UnitDimensionMismatch,
    UnknownMetric,
    UnknownRegistryVersion,
    UnknownUnit,
    convert_to_presentation,
    validate_metric_unit,
)

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
MAX_HISTORY_POINTS = 1000
MAX_BATCH_RECORDS = 500


class TelemetryRecordIn(BaseModel):
    dedup_key: str = Field(max_length=255)
    integration_id: uuid.UUID
    external_identifier: str = Field(max_length=255)
    source_identifier: str = Field(max_length=255)
    occurred_at: datetime
    # Issue #128 / G3: an exact decimal. On the wire a JSON number or a decimal string is parsed without a float
    # intermediary (telemetry_wire); non-finite values are carried through so the service rejects that one record.
    value: Decimal = Field(allow_inf_nan=True)
    attributes: dict = Field(default_factory=dict)
    # Issue #128 / G1: the immutable conversion contract the collector acquired this value under (from its
    # authenticated telemetry-contracts plan). Optional for older collectors; an unpinned record is accepted
    # only where Central can prove the interpretation (see telemetry_service.resolve_contract). The unit and
    # scale are optional echoes verified against the revision, never used to select a conversion.
    mapping_revision_id: uuid.UUID | None = None
    source_unit: str | None = Field(default=None, max_length=32)
    source_scale: Decimal | None = None
    # The exact numeral of `value` as received. Set by the wire parser only; never read from client JSON.
    _value_lexeme: str | None = PrivateAttr(default=None)

    @field_validator("value", mode="before")
    @classmethod
    def _no_boolean(cls, raw: object) -> object:
        if isinstance(raw, bool):
            raise ValueError("value must be a number")
        return raw

    @property
    def value_text(self) -> str:
        """The exact source text of `value`: the received numeral, else the decimal text of the supplied number."""
        return self._value_lexeme if self._value_lexeme is not None else str(self.value)


class TelemetryBatchIn(BaseModel):
    records: list[TelemetryRecordIn] = Field(max_length=MAX_BATCH_RECORDS)


def parse_telemetry_batch(raw_body: bytes) -> TelemetryBatchIn:
    """Parse the (already authenticated and size-bounded) telemetry body with lossless numbers.

    Raises `ApiError(422)` with the same shape as every other collector body validation failure.
    """
    from pydantic import ValidationError

    from app.domain.telemetry.wire import WireNumberError, loads_exact, prepare_records

    try:
        document, lexemes = prepare_records(loads_exact(raw_body))
        batch = TelemetryBatchIn.model_validate(document)
    except (WireNumberError, ValidationError) as error:
        raise ApiError(
            status_code=422, title="Validation Error", detail="One or more fields failed validation.",
            type_="https://dcim.internal/errors/validation",
        ) from error
    for index, record in enumerate(batch.records):
        record._value_lexeme = lexemes.get(index)
    return batch


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
    presentation_unit: str
    presentation_value: float
    raw_value: float | None = None
    raw_unit: str | None = None
    registry_version: str | None = None
    source_scale: float | None = None
    # Issue #128 / G3: exact decimal strings of the stored NUMERIC values (the floats above can not carry 18
    # significant digits). `raw_value_text` is the source numeral as received; null for rows stored before G3.
    value_decimal: str | None = None
    raw_value_decimal: str | None = None
    source_scale_decimal: str | None = None
    raw_value_text: str | None = None
    occurred_at: datetime
    received_at: datetime
    expected_poll_interval_seconds: int | None = None


class TelemetryHistoryOut(TelemetryOut):
    resolution: str = "raw"
    minimum_value: float | None = None
    maximum_value: float | None = None
    presentation_minimum_value: float | None = None
    presentation_maximum_value: float | None = None
    sample_count: int | None = None


class MetricMappingIn(BaseModel):
    integration_id: uuid.UUID
    managed_asset_id: uuid.UUID | None = None
    source_identifier: str = Field(max_length=255)
    canonical_metric: str
    unit: str = Field(max_length=32)
    # Issue #128 / G3: an exact decimal (JSON number or decimal string, no float intermediary), at most 8 decimal
    # places, within the NUMERIC(18, 8) range. A number such as 0.1 is accepted exactly as written.
    scale: Decimal = Decimal(1)
    label: str | None = Field(default=None, max_length=128)

    @field_validator("scale", mode="before")
    @classmethod
    def _scale_is_a_number(cls, raw: object) -> object:
        if isinstance(raw, bool):
            raise ValueError("scale must be a number")
        return raw

    @field_validator("scale")
    @classmethod
    def _scale_fits_storage(cls, scale: Decimal) -> Decimal:
        if not scale.is_finite() or abs(scale.adjusted()) > MAX_ABS_ADJUSTED_EXPONENT:
            raise ValueError("scale must be a finite number")
        if abs(scale) > MAX_STORABLE:
            raise ValueError("scale is outside the storable range")
        quantum = Decimal(1).scaleb(-NUMERIC_SCALE)
        if scale != scale.quantize(quantum):
            raise ValueError("scale supports at most 8 decimal places")
        return scale.quantize(quantum)


class MetricMappingOut(BaseModel):
    id: uuid.UUID
    integration_id: uuid.UUID
    managed_asset_id: uuid.UUID | None = None
    source_identifier: str
    canonical_metric: str
    unit: str
    scale: float
    scale_decimal: str
    label: str | None = None
    registry_version: str | None = None
    current_revision_id: uuid.UUID | None = None


class MetricRegistryOut(BaseModel):
    version: str
    metrics: dict[str, dict[str, str]]


@router.get("/metric-registry", response_model=MetricRegistryOut)
async def get_metric_registry(ctx=Depends(require_permission("telemetry:read"))) -> MetricRegistryOut:
    return MetricRegistryOut(
        version=REGISTRY_VERSION,
        metrics={
            key: {
                "dimension": definition.dimension,
                "canonical_unit": definition.canonical_unit,
                "presentation_unit": definition.presentation_unit,
            }
            for key, definition in METRIC_REGISTRY.items()
        },
    )


async def exact_mapping_body(request: Request) -> MetricMappingIn:
    """The mapping request body with `scale` parsed without a float intermediary (Issue #128 / G3).

    Declared after the permission dependency on the route, so an unauthenticated or unauthorised caller is refused
    before any body is read. Same 422 shape as automatic body validation.
    """
    from pydantic import ValidationError

    from app.domain.telemetry.wire import WireNumberError, exact_number, loads_exact

    try:
        document = loads_exact(await request.body())
        if isinstance(document, dict) and document.get("scale") is not None:
            number, _text = exact_number(document["scale"])
            document["scale"] = number
        return MetricMappingIn.model_validate(document)
    except (WireNumberError, ValidationError) as error:
        raise ApiError(
            status_code=422, title="Validation Error", detail="One or more fields failed validation.",
            type_="https://dcim.internal/errors/validation",
        ) from error


@router.post(
    "/mappings", response_model=MetricMappingOut, status_code=201,
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {
        "schema": MetricMappingIn.model_json_schema()}}}},
)
async def create_metric_mapping(
    ctx=Depends(require_permission("telemetry:manage")),
    body: MetricMappingIn = Depends(exact_mapping_body),
    db: AsyncSession = Depends(get_db),
) -> MetricMappingOut:
    if body.canonical_metric not in CANONICAL_METRICS:
        raise ApiError(status_code=422, title="Invalid canonical metric", detail="Metric is not supported by the MVP catalog.")
    try:
        validate_metric_unit(body.canonical_metric, body.unit)
    except UnknownUnit as exc:
        raise ApiError(status_code=422, title="Invalid source unit", detail=str(exc)) from exc
    except (UnitDimensionMismatch, UnknownMetric) as exc:
        raise ApiError(status_code=422, title="Metric/unit mismatch", detail=str(exc)) from exc
    if body.managed_asset_id is not None and await db.get(ManagedAsset, body.managed_asset_id) is None:
        raise ApiError(status_code=422, title="Invalid managed asset", detail="managed_asset_id does not exist.")
    if body.managed_asset_id is not None:
        # Issue #105: an environmental sensor may only be mapped to the metrics its kind can measure.
        sensor = await db.get(EnvironmentalSensor, body.managed_asset_id)
        if sensor is not None and body.canonical_metric not in SENSOR_KIND_METRICS[sensor.sensor_kind]:
            raise ApiError(
                status_code=422, title="Metric not supported by sensor",
                detail=f"A {sensor.sensor_kind} sensor can be mapped to {list(SENSOR_KIND_METRICS[sensor.sensor_kind])}.",
            )
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), registry_version=REGISTRY_VERSION, **body.model_dump())
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
    return MetricMappingOut(
        id=mapping.id, registry_version=mapping.registry_version, current_revision_id=mapping.current_revision_id,
        **{**body.model_dump(), "scale": float(body.scale)}, scale_decimal=decimal_text(body.scale),
    )


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
            scale_decimal=decimal_text(row.scale),
            label=row.label,
            registry_version=row.registry_version,
            current_revision_id=row.current_revision_id,
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
    open_holds = set(
        (
            await db.execute(
                select(TelemetryContractHold.dedup_key).where(
                    TelemetryContractHold.collector_id == collector.id,
                    TelemetryContractHold.status != "resolved",
                    TelemetryContractHold.dedup_key.in_([record.dedup_key for record in body.records]),
                )
            )
        ).scalars()
    )
    for record in body.records:
        assignment = await current_assignment(db, record.integration_id)
        if assignment is None or assignment.collector_id != collector.id:
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="NOT_ASSIGNED"))
            continue
        try:
            # Alarm compatibility may fail after inserting a reading or updating
            # another rule. Reject this record atomically without losing valid peers.
            async with db.begin_nested():
                outcome = await ingest_reading(
                    db,
                    collector_id=collector.id,
                    integration_id=record.integration_id,
                    dedup_key=record.dedup_key,
                    external_identifier=record.external_identifier,
                    source_identifier=record.source_identifier,
                    occurred_at=record.occurred_at,
                    value=record.value,
                    value_text=record.value_text,
                    attributes=record.attributes,
                    mapping_revision_id=record.mapping_revision_id,
                    source_unit=record.source_unit,
                    source_scale=record.source_scale,
                )
            if record.dedup_key in open_holds:
                await mark_hold_resolved_if_any(
                    db, collector_id=collector.id, dedup_key=record.dedup_key, reading_id=outcome.reading_id,
                    revision_id=None,
                )
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="duplicate" if outcome.duplicate else "accepted"))
        except MetricMappingNotFound:
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="UNKNOWN_METRIC_MAPPING"))
        except AmbiguousMappingContract as ambiguous:
            # Never reinterpret with the latest mapping. Keep the record (bounded retry, then operator release).
            hold = await register_contract_hold(
                db, collector_id=collector.id, integration_id=record.integration_id,
                source_identifier=record.source_identifier, external_identifier=record.external_identifier,
                dedup_key=record.dedup_key, occurred_at=record.occurred_at, value=record.value,
                value_text=record.value_text, attributes=record.attributes, reason=ambiguous.reason,
            )
            results.append(TelemetryAck(
                dedup_key=record.dedup_key, status="rejected",
                error="CONTRACT_HOLD_EXPIRED" if hold.expired else "AMBIGUOUS_MAPPING_CONTRACT",
            ))
        except UnknownMappingRevision:
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="UNKNOWN_MAPPING_REVISION"))
        except MappingRevisionMismatch:
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="MAPPING_REVISION_MISMATCH"))
        except ConversionContractDrift:
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="CONVERSION_CONTRACT_DRIFT"))
        except InvalidTelemetryValue:
            # NaN/Infinity or a value (raw, scaled or canonical) outside NUMERIC(18, 8).
            # Deterministic per-record rejection: the savepoint already discarded this
            # record, valid peers persist, and a retry is rejected again (never "duplicate").
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="INVALID_TELEMETRY_VALUE"))
        except (AlarmUnitCompatibilityError, UnknownUnit, UnitDimensionMismatch, UnknownMetric, UnknownRegistryVersion):
            results.append(TelemetryAck(dedup_key=record.dedup_key, status="rejected", error="INCOMPATIBLE_TELEMETRY_UNITS"))
    await db.commit()
    return TelemetryBatchOut(results=results)


class ContractHoldOut(BaseModel):
    id: uuid.UUID
    collector_id: uuid.UUID
    integration_id: uuid.UUID
    source_identifier: str
    external_identifier: str
    dedup_key: str
    occurred_at: datetime
    value: str
    reason: str
    status: str
    attempts: int
    first_held_at: datetime
    last_held_at: datetime
    resolved_at: datetime | None = None
    resolved_revision_id: uuid.UUID | None = None
    resolved_reading_id: uuid.UUID | None = None


def _hold_out(hold: TelemetryContractHold) -> ContractHoldOut:
    return ContractHoldOut(
        id=hold.id, collector_id=hold.collector_id, integration_id=hold.integration_id,
        source_identifier=hold.source_identifier, external_identifier=hold.external_identifier,
        dedup_key=hold.dedup_key, occurred_at=hold.occurred_at, value=hold.value_text, reason=hold.reason,
        status=hold.status, attempts=hold.attempts, first_held_at=hold.first_held_at, last_held_at=hold.last_held_at,
        resolved_at=hold.resolved_at, resolved_revision_id=hold.resolved_revision_id,
        resolved_reading_id=hold.resolved_reading_id,
    )


@router.get("/contract-holds", response_model=list[ContractHoldOut])
async def list_contract_holds(
    status: str | None = Query(default=None, pattern="^(held|expired|resolved)$"),
    integration_id: uuid.UUID | None = None,
    limit: int = Query(default=100, ge=1, le=MAX_HISTORY_POINTS),
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:read")),
) -> list[ContractHoldOut]:
    """Records Central refused to interpret because their event-time conversion contract is ambiguous."""
    stmt = select(TelemetryContractHold).order_by(TelemetryContractHold.last_held_at.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(TelemetryContractHold.status == status)
    if integration_id is not None:
        stmt = stmt.where(TelemetryContractHold.integration_id == integration_id)
    return [_hold_out(hold) for hold in (await db.execute(stmt)).scalars().all()]


class ContractHoldResolveIn(BaseModel):
    mapping_revision_id: uuid.UUID


@router.post("/contract-holds/{hold_id}/resolve", response_model=ContractHoldOut)
async def resolve_contract_hold(
    hold_id: uuid.UUID,
    body: ContractHoldResolveIn,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:manage")),
) -> ContractHoldOut:
    """Operator decision: interpret a held record under one explicitly chosen revision of the same source.

    This never edits a mapping or a revision; it only ingests the retained record with
    `contract_evidence = 'operator_resolved'` and records who decided and which revision was chosen.
    """
    hold = (
        await db.execute(select(TelemetryContractHold).where(TelemetryContractHold.id == hold_id).with_for_update())
    ).scalar_one_or_none()
    if hold is None:
        raise ApiError(status_code=404, title="Hold not found", detail="No such contract hold.")
    if hold.status == "resolved":
        raise ApiError(status_code=409, title="Already resolved", detail="This contract hold is already resolved.")
    try:
        async with db.begin_nested():
            outcome = await ingest_reading(
                db, collector_id=hold.collector_id, integration_id=hold.integration_id, dedup_key=hold.dedup_key,
                external_identifier=hold.external_identifier, source_identifier=hold.source_identifier,
                occurred_at=hold.occurred_at, value=Decimal(hold.value_text), value_text=hold.value_text,
                attributes=hold.attributes,
                mapping_revision_id=body.mapping_revision_id, pinned_evidence="operator_resolved",
            )
    except (UnknownMappingRevision, MappingRevisionMismatch, MetricMappingNotFound) as error:
        raise ApiError(status_code=422, title="Revision not valid for this hold", detail=str(error)) from error
    except (InvalidTelemetryValue, ConversionContractDrift, AlarmUnitCompatibilityError, UnknownUnit,
            UnitDimensionMismatch, UnknownMetric, UnknownRegistryVersion) as error:
        raise ApiError(status_code=422, title="Record cannot be stored under that revision", detail=str(error)) from error
    hold.status = "resolved"
    hold.resolved_at = datetime.now(UTC)
    hold.resolved_by = ctx.user.id
    hold.resolved_revision_id = body.mapping_revision_id
    hold.resolved_reading_id = outcome.reading_id
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="telemetry.contract_hold.resolve", entity_type="telemetry_contract_hold",
        entity_id=hold.id, request_id=None, correlation_id=None,
        after={"mapping_revision_id": str(body.mapping_revision_id), "reading_id": str(outcome.reading_id)},
    )
    await db.commit()
    return _hold_out(hold)


@router.get("/latest", response_model=list[TelemetryOut])
async def latest_readings(
    integration_id: uuid.UUID | None = None,
    managed_asset_id: uuid.UUID | None = None,
    metric: str | None = None,
    limit: int = Query(default=100, ge=1, le=MAX_HISTORY_POINTS),
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:read")),
) -> list[TelemetryOut]:
    # Poll cadence comes from the authoritative integration configuration in the
    # same bounded query; the UI must not guess a global interval or issue N+1 reads.
    stmt = (
        select(TelemetryReading, Integration.poll_interval_seconds)
        .join(Integration, Integration.id == TelemetryReading.integration_id)
        .order_by(TelemetryReading.occurred_at.desc())
        .limit(limit)
    )
    if integration_id is not None:
        stmt = stmt.where(TelemetryReading.integration_id == integration_id)
    if managed_asset_id is not None:
        stmt = stmt.where(TelemetryReading.managed_asset_id == managed_asset_id)
    if metric is not None:
        stmt = stmt.where(TelemetryReading.metric == metric)
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
    start, end = as_utc(start), as_utc(end)
    if start >= end:
        raise ApiError(status_code=422, title="Invalid time range", detail="start must be before end.")
    raw_stmt = (
        select(
            TelemetryReading.id.label("id"),
            TelemetryReading.occurred_at.label("occurred_at"),
            literal("raw").label("resolution"),
        )
        .where(
            TelemetryReading.metric == metric,
            TelemetryReading.occurred_at >= start,
            TelemetryReading.occurred_at <= end,
        )
    )
    if integration_id is not None:
        raw_stmt = raw_stmt.where(TelemetryReading.integration_id == integration_id)
    if managed_asset_id is not None:
        raw_stmt = raw_stmt.where(TelemetryReading.managed_asset_id == managed_asset_id)
    daily_stmt = (
        select(
            DailyTelemetryAggregate.id.label("id"),
            func.timezone("UTC", cast(DailyTelemetryAggregate.day, DateTime())).label("occurred_at"),
            literal("daily").label("resolution"),
        )
        .where(
            DailyTelemetryAggregate.metric == metric,
            DailyTelemetryAggregate.day >= utc_day(start),
            DailyTelemetryAggregate.day <= utc_day(end),
        )
    )
    if integration_id is not None:
        daily_stmt = daily_stmt.where(DailyTelemetryAggregate.integration_id == integration_id)
    if managed_asset_id is not None:
        daily_stmt = daily_stmt.where(DailyTelemetryAggregate.managed_asset_id == managed_asset_id)
    # See telemetry_retention's storage contract: never suppress raw rows merely
    # because their day has an aggregate. SKIP LOCKED compaction may be partial.
    # A single statement prevents READ COMMITTED snapshots straddling compaction
    # (two separate selects could miss evidence or read it twice).
    points = union_all(raw_stmt, daily_stmt).subquery()
    bounded = (
        select(points).order_by(points.c.occurred_at, points.c.resolution, points.c.id).limit(limit).subquery()
    )
    stmt = (
        select(TelemetryReading, DailyTelemetryAggregate)
        .select_from(bounded)
        .outerjoin(TelemetryReading, and_(bounded.c.resolution == "raw", TelemetryReading.id == bounded.c.id))
        .outerjoin(DailyTelemetryAggregate, and_(bounded.c.resolution == "daily", DailyTelemetryAggregate.id == bounded.c.id))
        .order_by(bounded.c.occurred_at, bounded.c.resolution, bounded.c.id)
    )
    # Preserve the API's whole-day daily buckets (including a start day's midnight
    # bucket for an intraday start); raw endpoints remain inclusive.
    return [_history_raw(raw) if raw is not None else _history_daily(daily) for raw, daily in (await db.execute(stmt)).all()]



def decimal_text(value: Decimal | float | int | None) -> str | None:
    """A stored NUMERIC as plain positional decimal text: exact, deterministic, never via a float or scientific form."""
    if value is None:
        return None
    return format(value if isinstance(value, Decimal) else Decimal(str(value)), "f")


def _out(row: TelemetryReading, *, poll_interval_seconds: int | None = None) -> TelemetryOut:
    presentation = (
        convert_to_presentation(row.metric, row.value, row.unit, registry_version=row.registry_version)
        if row.registry_version is not None else None
    )
    return TelemetryOut(
        id=row.id,
        integration_id=row.integration_id,
        managed_asset_id=row.managed_asset_id,
        external_identifier=row.external_identifier,
        metric=row.metric,
        unit=row.unit,
        value=float(row.value),
        presentation_unit=presentation.unit if presentation is not None else row.unit,
        presentation_value=float(presentation.value) if presentation is not None else float(row.value),
        raw_value=None if row.raw_value is None else float(row.raw_value),
        raw_unit=row.raw_unit,
        source_scale=None if row.source_scale is None else float(row.source_scale),
        value_decimal=decimal_text(row.value),
        raw_value_decimal=decimal_text(row.raw_value),
        source_scale_decimal=decimal_text(row.source_scale),
        raw_value_text=row.raw_value_text,
        registry_version=row.registry_version,
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        expected_poll_interval_seconds=poll_interval_seconds,
    )


def _history_raw(row: TelemetryReading) -> TelemetryHistoryOut:
    return TelemetryHistoryOut(**_out(row).model_dump(), resolution="raw")


def _history_daily(row: DailyTelemetryAggregate) -> TelemetryHistoryOut:
    occurred_at = datetime.combine(row.day, datetime.min.time(), tzinfo=UTC)
    if row.registry_version is None:
        average = minimum = maximum = None
    else:
        average = convert_to_presentation(row.metric, row.average_value, row.unit, registry_version=row.registry_version)
        minimum = convert_to_presentation(row.metric, row.minimum_value, row.unit, registry_version=row.registry_version)
        maximum = convert_to_presentation(row.metric, row.maximum_value, row.unit, registry_version=row.registry_version)
    return TelemetryHistoryOut(
        id=row.id,
        integration_id=row.integration_id,
        managed_asset_id=row.managed_asset_id,
        external_identifier=row.external_identifier,
        metric=row.metric,
        unit=row.unit,
        value=float(row.average_value),
        # The stored average is exact NUMERIC; there is no source numeral for an aggregate, so no raw_* is invented.
        value_decimal=decimal_text(row.average_value),
        presentation_unit=average.unit if average is not None else row.unit,
        presentation_value=float(average.value) if average is not None else float(row.average_value),
        registry_version=row.registry_version,
        occurred_at=occurred_at,
        received_at=occurred_at,
        resolution="daily",
        minimum_value=float(row.minimum_value),
        maximum_value=float(row.maximum_value),
        presentation_minimum_value=float(minimum.value) if minimum is not None else float(row.minimum_value),
        presentation_maximum_value=float(maximum.value) if maximum is not None else float(row.maximum_value),
        sample_count=row.sample_count,
    )


# --------------------------------------------------------------------------------------
# Phase 10C: port/power-inlet telemetry bindings + cached latest status
# --------------------------------------------------------------------------------------


class TelemetryBindingIn(BaseModel):
    equipment_id: uuid.UUID
    target_type: str
    equipment_port_id: uuid.UUID | None = None
    equipment_power_inlet_id: uuid.UUID | None = None
    protocol: str
    external_ref: str = Field(max_length=255)
    label: str | None = Field(default=None, max_length=128)


class TelemetryBindingOut(TelemetryBindingIn):
    id: uuid.UUID


class PortStatusIngestIn(BaseModel):
    binding_id: uuid.UUID
    payload: dict
    sampled_at: datetime


class PortStatusOut(BaseModel):
    binding_id: uuid.UUID
    equipment_id: uuid.UUID
    target_type: str
    equipment_port_id: uuid.UUID | None
    equipment_power_inlet_id: uuid.UUID | None
    label: str | None
    status_level: str | None = None
    payload: dict | None = None
    sampled_at: datetime | None = None
    received_at: datetime | None = None


def _binding_out(binding: PortTelemetryBinding) -> TelemetryBindingOut:
    return TelemetryBindingOut(
        id=binding.id,
        equipment_id=binding.equipment_id,
        target_type=binding.target_type,
        equipment_port_id=binding.equipment_port_id,
        equipment_power_inlet_id=binding.equipment_power_inlet_id,
        protocol=binding.protocol,
        external_ref=binding.external_ref,
        label=binding.label,
    )


def _port_status_out(item: LatestPortStatus) -> PortStatusOut:
    binding = item.binding
    status = item.status
    return PortStatusOut(
        binding_id=binding.id,
        equipment_id=binding.equipment_id,
        target_type=binding.target_type,
        equipment_port_id=binding.equipment_port_id,
        equipment_power_inlet_id=binding.equipment_power_inlet_id,
        label=binding.label,
        status_level=status.status_level if status else None,
        payload=status.payload if status else None,
        sampled_at=status.sampled_at if status else None,
        received_at=status.received_at if status else None,
    )


@router.post("/bindings", response_model=TelemetryBindingOut, status_code=201)
async def create_telemetry_binding(
    body: TelemetryBindingIn,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:manage")),
) -> TelemetryBindingOut:
    if body.target_type not in TELEMETRY_TARGET_TYPES:
        raise ApiError(
            status_code=422, title="Invalid target type", detail=f"target_type must be one of {TELEMETRY_TARGET_TYPES}."
        )
    if body.protocol not in TELEMETRY_PROTOCOLS:
        raise ApiError(status_code=422, title="Invalid protocol", detail=f"protocol must be one of {TELEMETRY_PROTOCOLS}.")
    if await db.get(ManagedAsset, body.equipment_id) is None:
        raise ApiError(status_code=422, title="Invalid equipment", detail="equipment_id does not exist.")
    try:
        binding = await create_port_telemetry_binding(
            db,
            equipment_id=body.equipment_id,
            target_type=body.target_type,
            equipment_port_id=body.equipment_port_id,
            equipment_power_inlet_id=body.equipment_power_inlet_id,
            protocol=body.protocol,
            external_ref=body.external_ref,
            label=body.label,
        )
    except BindingNotFound as exc:
        raise ApiError(status_code=422, title="Invalid binding target", detail=str(exc)) from exc
    except InvalidBindingTarget as exc:
        raise ApiError(status_code=422, title="Invalid binding target", detail=str(exc)) from exc
    await write_audit_log(
        db,
        actor_user_id=ctx.user.id,
        action="telemetry.binding.create",
        entity_type="port_telemetry_binding",
        entity_id=binding.id,
        request_id=None,
        correlation_id=None,
        after={"equipment_id": str(binding.equipment_id), "target_type": binding.target_type},
    )
    await write_outbox_event(
        db,
        event_type="TelemetryBindingCreated",
        aggregate_type="port_telemetry_binding",
        aggregate_id=binding.id,
        payload={"equipment_id": str(binding.equipment_id), "target_type": binding.target_type},
    )
    await db.commit()
    return _binding_out(binding)


@router.get("/bindings", response_model=list[TelemetryBindingOut])
async def list_telemetry_bindings(
    equipment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:read")),
) -> list[TelemetryBindingOut]:
    bindings = await list_port_telemetry_bindings(db, equipment_id=equipment_id)
    return [_binding_out(b) for b in bindings]


@router.post("/port-status/ingest", response_model=PortStatusOut)
async def ingest_port_status(
    body: PortStatusIngestIn,
    db: AsyncSession = Depends(get_db),
    # A caller with telemetry:manage — a poller/collector service account or, for a
    # synthetic/demo feed, an operator — never a raw device: unlike the MVP pipeline's
    # collector-signed batch endpoint (ingest_collector_telemetry, above), Phase 10C
    # introduces no new machine-trust boundary of its own.
    ctx=Depends(require_permission("telemetry:manage")),
) -> PortStatusOut:
    try:
        await record_latest_status(db, binding_id=body.binding_id, payload=body.payload, sampled_at=body.sampled_at)
    except BindingNotFound as exc:
        raise ApiError(status_code=404, title="Binding not found", detail=str(exc)) from exc
    except KeyError as exc:
        raise ApiError(
            status_code=422, title="Missing telemetry field", detail=f"payload is missing required field {exc}."
        ) from exc
    except ValueError as exc:
        raise ApiError(status_code=422, title="Invalid telemetry payload", detail=str(exc)) from exc
    await db.commit()
    binding = await db.get(PortTelemetryBinding, body.binding_id)
    assert binding is not None
    items = await get_latest_status_for_equipment(db, equipment_id=binding.equipment_id)
    (item,) = [i for i in items if i.binding.id == binding.id]
    return _port_status_out(item)


@router.get("/port-status/latest", response_model=list[PortStatusOut])
async def latest_port_status(
    equipment_id: uuid.UUID | None = None,
    rack_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:read")),
) -> list[PortStatusOut]:
    """The rack-elevation overlay's own bulk read: pass `rack_id` to fetch every binding
    for every currently rack-mounted equipment in one bounded call, or `equipment_id` for
    a single equipment's own bindings (`EquipmentDetailPage`'s "Simulate Outage" and live-
    status panels). Exactly one of the two is required — this is never an unfiltered scan
    of the whole telemetry_latest_status table."""
    if (equipment_id is None) == (rack_id is None):
        raise ApiError(
            status_code=422, title="Filter required", detail="Exactly one of equipment_id or rack_id is required."
        )
    if equipment_id is not None:
        items = await get_latest_status_for_equipment(db, equipment_id=equipment_id)
    else:
        assert rack_id is not None
        items = await get_latest_status_for_rack(db, rack_id=rack_id)
    return [_port_status_out(item) for item in items]
