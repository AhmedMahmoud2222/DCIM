# ruff: noqa: E501
"""Series definitions (one per metric mapping) and the ingestion of simulated samples through the supported
collector contract (`ingest_collector_telemetry`): the same function the signed collector endpoint calls, so
collector assignment, pinned mapping revisions, unit conversion, de-duplication and alarm evaluation all apply."""

import asyncio
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.telemetry import TelemetryBatchIn, TelemetryRecordIn, ingest_collector_telemetry
from app.domain.integration.models import Collector
from app.domain.telemetry.models import IntegrationMetricMapping, IntegrationMetricMappingRevision, TelemetryReading
from app.domain.telemetry.registry import convert_value, normalize_unit, registry_snapshot
from app.tools.demo_loader.constants import (
    DEDUP_PREFIX,
    MAX_BATCH,
    POLL_INTERVAL_FALLBACK_SECONDS,
    SOURCE_PREFIX,
    reading_attributes,
)
from app.tools.demo_loader.selection import IntegrationRoute, LoaderError, Targets
from app.tools.demo_loader.signals import canonical_value, slot_grid, to_decimal


@dataclass
class Series:
    mapping: IntegrationMetricMapping
    revision: IntegrationMetricMappingRevision
    route: IntegrationRoute
    external_identifier: str
    signal_key: str
    base: float | None = None

    @property
    def metric(self) -> str:
        return self.revision.canonical_metric

    @property
    def interval(self) -> int:
        return self.route.integration.poll_interval_seconds or POLL_INTERVAL_FALLBACK_SECONDS


def raw_value_for(revision: IntegrationMetricMappingRevision, canonical: float) -> Decimal:
    """The source-unit numeral a device would send so that Central's pinned conversion stores `canonical`.

    Central computes stored = convert(raw * scale, source_unit -> canonical_unit); this is the inverse."""
    scale = Decimal(str(revision.source_scale))
    if scale == 0:
        raise LoaderError(f"Mapping revision {revision.id} has a zero scale.")
    target = to_decimal(canonical)
    if revision.registry_version is None:
        unit = normalize_unit(revision.source_unit)
        canonical_unit = registry_snapshot("1").metrics[revision.canonical_metric].canonical_unit
        in_source = target if unit == canonical_unit else convert_value(target, canonical_unit, unit)
    else:
        definition = registry_snapshot(revision.registry_version).metrics[revision.canonical_metric]
        in_source = convert_value(
            target, definition.canonical_unit, revision.source_unit, registry_version=revision.registry_version,
        )
    return (in_source / scale).quantize(Decimal("0.00000001"))


async def build_series(
    db: AsyncSession, mappings: list[IntegrationMetricMapping], routes: dict[uuid.UUID, IntegrationRoute],
    *, anchored: bool = False,
) -> list[Series]:
    """`anchored`: continue each series from its latest real reading (used for mappings that already have data)."""
    series: list[Series] = []
    for mapping in mappings:
        route = routes.get(mapping.integration_id)
        if route is None or mapping.current_revision_id is None:
            continue
        revision = await db.get(IntegrationMetricMappingRevision, mapping.current_revision_id)
        assert revision is not None
        external = external_identifier_for(mapping)
        base = None
        if anchored:
            latest = (
                await db.execute(
                    select(TelemetryReading).where(TelemetryReading.mapping_id == mapping.id)
                    .where(TelemetryReading.attributes["provenance"].astext.is_distinct_from("demo_loader"))
                    .order_by(TelemetryReading.occurred_at.desc()).limit(1)
                )
            ).scalar_one_or_none()
            if latest is not None:
                external = latest.external_identifier
                snapshot = registry_snapshot(revision.registry_version or "1")
                if snapshot.metrics[revision.canonical_metric].canonical_unit == latest.unit:
                    base = float(latest.value)
        series.append(Series(mapping=mapping, revision=revision, route=route, external_identifier=external,
                             signal_key=f"{route.integration.name}|{mapping.source_identifier}", base=base))
    return series


async def existing_dedup_keys(db: AsyncSession, since: datetime) -> set[str]:
    rows = await db.execute(
        select(TelemetryReading.dedup_key)
        .where(TelemetryReading.dedup_key.startswith(DEDUP_PREFIX), TelemetryReading.occurred_at >= since)
    )
    return set(rows.scalars())


def make_record(series: Series, at: datetime) -> TelemetryRecordIn:
    revision = series.revision
    value = raw_value_for(revision, canonical_value(series.metric, series.signal_key, at, series.base))
    return TelemetryRecordIn(
        dedup_key=f"{DEDUP_PREFIX}{series.mapping.id}:{int(at.timestamp())}",
        integration_id=series.mapping.integration_id,
        external_identifier=series.external_identifier,
        source_identifier=series.mapping.source_identifier,
        occurred_at=at,
        value=value,
        attributes=reading_attributes(),
        mapping_revision_id=revision.id,
        source_unit=revision.source_unit,
        source_scale=Decimal(str(revision.source_scale)),
    )


@dataclass
class IngestReport:
    accepted: int = 0
    duplicate: int = 0
    skipped_existing: int = 0
    rejected: Counter | None = None
    # collector name -> exception type, for a collector whose ingest raised part-way. Batches it had already
    # committed stay; the rest were not sent. Re-running resumes from what is stored.
    failed_collectors: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if self.rejected is None:
            self.rejected = Counter()
        if self.failed_collectors is None:
            self.failed_collectors = {}

    def merge(self, other: "IngestReport") -> None:
        self.accepted += other.accepted
        self.duplicate += other.duplicate
        self.skipped_existing += other.skipped_existing
        assert self.rejected is not None and other.rejected is not None
        self.rejected.update(other.rejected)
        assert self.failed_collectors is not None and other.failed_collectors is not None
        self.failed_collectors.update(other.failed_collectors)

    @property
    def clean(self) -> bool:
        return not self.rejected and not self.failed_collectors

    def as_dict(self) -> dict:
        return {"accepted": self.accepted, "duplicate": self.duplicate, "skipped_existing": self.skipped_existing,
                "rejected": dict(self.rejected or {}),
                "failed_collectors": dict(self.failed_collectors or {})}


def _failure_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {str(error)[:200]}"


async def _ingest_group(
    db: AsyncSession, collector_id: uuid.UUID, collector_name: str, records: list[TelemetryRecordIn],
    progress: Callable[[str], None] | None,
) -> IngestReport:
    """Ingest one collector's records in batches, committing each batch (the contract commits).

    Identity (`collector_id`, `collector_name`) is captured by the caller before any SQL runs, because a rollback expires
    every ORM instance in the session and an AsyncSession cannot lazily reload one. The collector is re-read at the start
    of the group and after a failure the group stops and reports; it never touches expired state or raises into `gather`."""
    report = IngestReport()
    assert report.failed_collectors is not None
    records.sort(key=lambda r: r.occurred_at)  # oldest first, so alarm open/clear transitions replay in order
    start = 0
    try:
        collector = await db.get(Collector, collector_id, populate_existing=True)
        if collector is None:
            report.failed_collectors[collector_name] = "collector no longer exists"
            return report
        for start in range(0, len(records), MAX_BATCH):
            chunk = records[start:start + MAX_BATCH]
            result = await ingest_collector_telemetry(db, collector=collector, body=TelemetryBatchIn(records=chunk))
            for ack in result.results:
                if ack.status == "accepted":
                    report.accepted += 1
                elif ack.status == "duplicate":
                    report.duplicate += 1
                else:
                    assert report.rejected is not None
                    report.rejected[ack.error or "REJECTED"] += 1
            if progress:
                progress(f"  {collector_name}: {min(start + MAX_BATCH, len(records))}/{len(records)} records")
    except Exception as error:  # noqa: BLE001 - one collector's failure must not hide, or stop, the others
        report.failed_collectors[collector_name] = _failure_text(error)
        try:
            await db.rollback()
        except Exception as rollback_error:  # noqa: BLE001 - a dead connection must not mask the original failure
            report.failed_collectors[collector_name] += f" (rollback also failed: {_failure_text(rollback_error)})"
        if progress:
            progress(f"  {collector_name}: FAILED in the batch starting at record {start} ({type(error).__name__}); a re-run resumes")
    return report


async def _ingest_group_in_own_session(
    factory: Callable[[], AsyncSession], collector_id: uuid.UUID, collector_name: str, records: list[TelemetryRecordIn],
    progress: Callable[[str], None] | None,
) -> IngestReport:
    try:
        async with factory() as session:
            return await _ingest_group(session, collector_id, collector_name, records, progress)
    except Exception as error:  # noqa: BLE001 - e.g. the session could not be opened
        report = IngestReport()
        assert report.failed_collectors is not None
        report.failed_collectors[collector_name] = _failure_text(error)
        return report


async def simulate(
    db: AsyncSession, series: list[Series], *, hours: float, now: datetime | None = None,
    progress: Callable[[str], None] | None = None, session_factory: Callable[[], AsyncSession] | None = None,
) -> IngestReport:
    """Generate and ingest every missing slot of the last `hours` hours for each series, oldest first.

    Each record goes through `ingest_collector_telemetry`. With a `session_factory`, the records of different
    collectors are ingested concurrently, one session per collector. Each integration has one collector, and the contract
    locks only the alarm rules of the integrations assigned to the calling collector (in id order), so two groups never
    wait on the same rule, reading key or mapping. That reasoning is not a proof against deadlocks for arbitrary data with
    shared assets; `--sequential` is the fallback."""
    now = now or datetime.now(UTC)
    report = IngestReport()
    if not series:
        return report
    earliest = min(slot_grid(now, s.interval, hours)[0] for s in series if slot_grid(now, s.interval, hours))
    known = await existing_dedup_keys(db, earliest)
    pending: dict[uuid.UUID, tuple[str, list[TelemetryRecordIn]]] = {}
    for item in series:
        collector_id, collector_name = item.route.collector.id, item.route.collector.name
        for at in slot_grid(now, item.interval, hours):
            record = make_record(item, at)
            if record.dedup_key in known:
                report.skipped_existing += 1
                continue
            pending.setdefault(collector_id, (collector_name, []))[1].append(record)
    if session_factory is not None and len(pending) > 1:
        await db.commit()  # release this session's snapshot; the workers read committed rows
        reports = await asyncio.gather(
            *(_ingest_group_in_own_session(session_factory, cid, name, records, progress) for cid, (name, records) in pending.items())
        )
    else:
        reports = [await _ingest_group(db, cid, name, records, progress) for cid, (name, records) in pending.items()]
    for part in reports:
        report.merge(part)
    return report


def routes_by_integration(targets: Targets) -> dict[uuid.UUID, IntegrationRoute]:
    """Routes keyed by integration id. Only integrations resolved for a demo site are present, so a mapping that
    belongs to any other integration (for example the HRC site's) is never simulated."""
    return {route.integration.id: route for route in targets.routes.values()}


def external_identifier_for(mapping: IntegrationMetricMapping) -> str:
    """Loader mappings are `demo-sim:<asset tag>:<metric>`; the asset tag is the device's own identifier. Any other
    mapping keeps its source identifier until its real external identifier is read from its latest reading."""
    if mapping.source_identifier.startswith(SOURCE_PREFIX):
        return mapping.source_identifier[len(SOURCE_PREFIX):].rsplit(":", 1)[0]
    return mapping.source_identifier


def is_loader_mapping(mapping: IntegrationMetricMapping) -> bool:
    return mapping.source_identifier.startswith(SOURCE_PREFIX)
