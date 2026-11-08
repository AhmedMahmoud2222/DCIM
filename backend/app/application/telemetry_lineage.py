"""G6 lineage writes share the caller's aggregate/ledger/raw-deletion transaction.

The parent aggregate is either new, or locked by G4-R. Only successfully claimed
samples enter here, so suppressed replay can never inflate lineage coverage.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.telemetry.lineage import LineageKey, evidence_class, summarize_fresh_readings
from app.domain.telemetry.models import DailyTelemetryAggregate, DailyTelemetryAggregateLineage, TelemetryReading


def contract_key(key: LineageKey) -> str:
    # NULL and empty-string source units remain distinct; Decimal scale normalizes
    # without depending on decimal context or PostgreSQL text formatting.
    encoded = json.dumps([str(key.mapping_revision_id) if key.mapping_revision_id else None,
                          key.contract_evidence, key.raw_unit,
                          format(key.source_scale, '.8f') if key.source_scale is not None else None],
                         ensure_ascii=True, separators=(',', ':'))
    return hashlib.sha256(encoded.encode('ascii')).hexdigest()


async def record_lineage(db: AsyncSession, aggregate: DailyTelemetryAggregate,
                         fresh: Sequence[TelemetryReading], *, now: datetime, retention_days: int) -> None:
    summaries = summarize_fresh_readings(fresh)
    await db.flush()  # allocate parent id before its FK is used; arithmetic must already be valid
    for summary in summaries:
        key = summary.key
        statement = pg_insert(DailyTelemetryAggregateLineage).values(
            aggregate_id=aggregate.id, contract_key=contract_key(key), mapping_revision_id=key.mapping_revision_id,
            contract_evidence=key.contract_evidence, raw_unit=key.raw_unit, source_scale=key.source_scale,
            sample_count=summary.sample_count, first_occurred_at=summary.first_occurred_at,
            last_occurred_at=summary.last_occurred_at,
        )
        await db.execute(statement.on_conflict_do_update(
            constraint='uq_daily_lineage_contract', set_={
                'sample_count': DailyTelemetryAggregateLineage.sample_count + statement.excluded.sample_count,
                'first_occurred_at': func.least(
                    DailyTelemetryAggregateLineage.first_occurred_at, statement.excluded.first_occurred_at),
                'last_occurred_at': func.greatest(
                    DailyTelemetryAggregateLineage.last_occurred_at, statement.excluded.last_occurred_at),
            }))
    rows = (await db.execute(select(DailyTelemetryAggregateLineage).where(
        DailyTelemetryAggregateLineage.aggregate_id == aggregate.id).execution_options(populate_existing=True))).scalars().all()
    aggregate.lineage_sample_count = sum(row.sample_count for row in rows)
    if aggregate.lineage_sample_count > aggregate.sample_count:
        raise ValueError('Lineage counts contradict the aggregate')
    keys = [LineageKey(row.mapping_revision_id, row.contract_evidence, row.raw_unit, row.source_scale) for row in rows]
    if aggregate.lineage_sample_count < aggregate.sample_count:
        keys.append(LineageKey(None, None, None, None))  # historic contributions remain explicitly unknown
    aggregate.evidence_class = evidence_class(keys)
    if aggregate.first_compacted_at is None:
        aggregate.first_compacted_at = now  # first observed lineage consumption, never fabricated historic timestamp
        aggregate.raw_retention_days_at_compaction = retention_days
