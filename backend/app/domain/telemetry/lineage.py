"""G6 contract summaries for permanently compacted source evidence.

Only freshly claimed G4-R samples may enter this function. A caller must persist
these summaries in the aggregate transaction before deleting raw evidence.
No source value, numeric lexeme or arbitrary attributes enter lineage.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal


@dataclass(frozen=True)
class LineageKey:
    mapping_revision_id: uuid.UUID | None
    contract_evidence: str | None
    raw_unit: str | None
    source_scale: Decimal | None


@dataclass(frozen=True)
class LineageSummary:
    key: LineageKey
    sample_count: int
    first_occurred_at: datetime
    last_occurred_at: datetime


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def summarize_fresh_readings(readings: Iterable) -> tuple[LineageSummary, ...]:
    groups: dict[LineageKey, LineageSummary] = {}
    for reading in readings:
        key = LineageKey(reading.mapping_revision_id, reading.contract_evidence,
                         reading.raw_unit, Decimal(str(reading.source_scale)) if reading.source_scale is not None else None)
        occurred = _utc(reading.occurred_at)
        previous = groups.get(key)
        groups[key] = LineageSummary(
            key, (previous.sample_count if previous else 0) + 1,
            min(previous.first_occurred_at, occurred) if previous else occurred,
            max(previous.last_occurred_at, occurred) if previous else occurred,
        )
    return tuple(groups.values())


def evidence_class(keys: Iterable[LineageKey]) -> str:
    statuses = set()
    for key in keys:
        status = {"pinned": "pinned", "inferred_single_revision": "inferred",
                  "operator_resolved": "operator_resolved"}.get(key.contract_evidence or "")
        statuses.add(status if status and key.mapping_revision_id is not None else "unverified")
    if not statuses:
        return "unverified"
    return next(iter(statuses)) if len(statuses) == 1 else "mixed"


def lineage_coverage(aggregate_sample_count: int, lineage_sample_count: int) -> str:
    if aggregate_sample_count < 1 or lineage_sample_count < 0 or lineage_sample_count > aggregate_sample_count:
        raise ValueError("Lineage counts contradict the aggregate")
    if lineage_sample_count == 0:
        return "none"
    return "complete" if lineage_sample_count == aggregate_sample_count else "partial"
