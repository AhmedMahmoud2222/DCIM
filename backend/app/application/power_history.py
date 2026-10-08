"""Historical utilization snapshots (Issue #102, area C).

One row per (UTC hour bucket, scope) holds the rolled-up load, capacity and headroom for a power node or
a site. Buckets are closed hours only, computed from `telemetry_reading` rows of the canonical metric
(kW, per the #99 registry; no conversion happens here), written with INSERT .. ON CONFLICT DO NOTHING so
a rerun never changes an existing row, and never touching the raw readings. Scope ids are kept without a
foreign key so a retired node's history stays queryable.

The topology and capacity used for a bucket are the ones in force when the bucket is computed, which is
within an hour of the bucket closing for the scheduled task. A backfill run later therefore records the
topology at backfill time; the row says so through `computed_at`."""

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.power_rollup import EquipmentIn, RollupResult, compute_rollup
from app.application.power_rollup_loader import POWER_METRIC, POWER_UNIT, load_rollup_inputs
from app.domain.location.models import Site
from app.domain.power.analytics_models import PowerUtilizationSnapshot
from app.domain.telemetry.models import TelemetryReading
from app.domain.telemetry.registry import REGISTRY_VERSION

METHOD_VERSION = "1"
EXPECTED_SAMPLES_PER_HOUR = 60  # one reading a minute per equipment item
MAX_BACKFILL_HOURS = 24 * 31


def floor_hour(ts: datetime) -> datetime:
    return ts.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def closed_buckets(now: datetime, lookback_hours: int) -> list[datetime]:
    """Start times of the last `lookback_hours` fully elapsed hours, oldest first."""
    if lookback_hours < 1 or lookback_hours > MAX_BACKFILL_HOURS:
        raise ValueError("lookback_hours out of range")
    end = floor_hour(now)
    return [end - timedelta(hours=i) for i in range(lookback_hours, 0, -1)]


async def _bucket_means(
    db: AsyncSession, equipment_ids: list[uuid.UUID], start: datetime, end: datetime
) -> dict[uuid.UUID, tuple[float, int]]:
    if not equipment_ids:
        return {}
    rows = (
        await db.execute(
            select(TelemetryReading.managed_asset_id, func.avg(TelemetryReading.value), func.count())
            .where(
                TelemetryReading.metric == POWER_METRIC, TelemetryReading.unit == POWER_UNIT,
                TelemetryReading.managed_asset_id.in_(equipment_ids),
                TelemetryReading.occurred_at >= start, TelemetryReading.occurred_at < end,
            )
            .group_by(TelemetryReading.managed_asset_id)
        )
    ).all()
    return {aid: (float(avg), int(n)) for aid, avg, n in rows if aid is not None}


def _basis(quality: str, load: float | None) -> str:
    if load is None or quality == "missing":
        return "none"
    return "measured" if quality == "measured" else "estimated"


async def compute_bucket(
    db: AsyncSession, site_id: uuid.UUID, bucket_start: datetime, now: datetime
) -> tuple[RollupResult, dict[uuid.UUID, int], dict[uuid.UUID, int]]:
    """Rollup for one closed hour. Returns (result, per-equipment sample counts, expected per equipment)."""
    bucket_end = bucket_start + timedelta(hours=1)
    nodes, edges, equipment = await load_rollup_inputs(db, site_id)
    means = await _bucket_means(db, [e.id for e in equipment], bucket_start, bucket_end)
    bucketed: list[EquipmentIn] = []
    counts: dict[uuid.UUID, int] = {}
    for e in equipment:
        mean = means.get(e.id)
        counts[e.id] = mean[1] if mean else 0
        bucketed.append(replace(e, measured_kw=mean[0] if mean else None, measured_at=bucket_end if mean else None))
    result = compute_rollup(nodes, edges, bucketed, now=bucket_end, site_id=site_id)
    return result, counts, {e.id: EXPECTED_SAMPLES_PER_HOUR for e in equipment}


async def write_site_bucket(db: AsyncSession, site_id: uuid.UUID, bucket_start: datetime, now: datetime) -> int:
    """Insert the site and node rows for one bucket; existing rows are left exactly as they are.
    Returns the number of rows newly inserted."""
    if bucket_start != floor_hour(bucket_start) or bucket_start + timedelta(hours=1) > now:
        raise ValueError("bucket must be a whole UTC hour that has already closed")
    result, counts, expected = await compute_bucket(db, site_id, bucket_start, now)
    bucket_end = bucket_start + timedelta(hours=1)
    rows: list[dict] = []

    def row(scope_type: str, scope_id: uuid.UUID, load, alloc, cap, head, util, quality, members) -> dict:
        samples = sum(counts.get(m, 0) for m in members)
        exp = sum(expected.get(m, 0) for m in members)
        coverage = min(1.0, samples / exp) if exp else 0.0
        basis = _basis(quality, load)
        return {
            "id": uuid.uuid4(), "granularity": "hour", "bucket_start": bucket_start, "bucket_end": bucket_end,
            "scope_type": scope_type, "scope_id": scope_id, "site_id": site_id, "metric": POWER_METRIC,
            "unit": POWER_UNIT, "registry_version": REGISTRY_VERSION,
            "load_kw": None if basis == "none" else load, "load_basis": basis,
            "allocated_kw": alloc if basis != "none" else None, "effective_capacity_kw": cap,
            "headroom_kw": head if basis != "none" else None, "utilization_pct": util if basis != "none" else None,
            "sample_count": samples, "expected_samples": exp, "coverage_ratio": round(coverage, 5),
            "quality": quality, "window_start": bucket_start, "window_end": bucket_end,
            "method_version": METHOD_VERSION, "computed_at": now,
        }

    s = result.site
    rows.append(
        row(
            "site", site_id, s.load_kw, s.allocated_kw, s.capacity_kw, s.headroom_kw, s.utilization_pct, s.quality,
            list(counts),
        )
    )
    for nr in result.nodes.values():
        rows.append(
            row(
                "power_node", nr.id, nr.load_kw, nr.allocated_kw, nr.capacity_kw, nr.headroom_kw, nr.utilization_pct,
                nr.quality, list(nr.contributors),
            )
        )
    inserted = 0
    for chunk_start in range(0, len(rows), 500):
        stmt = (
            insert(PowerUtilizationSnapshot)
            .values(rows[chunk_start : chunk_start + 500])
            .on_conflict_do_nothing(constraint="uq_power_snapshot_bucket_scope")
            .returning(PowerUtilizationSnapshot.id)
        )
        inserted += len((await db.execute(stmt)).all())
    return inserted


async def snapshot_all_sites(db: AsyncSession, now: datetime, lookback_hours: int = 3) -> dict[str, int]:
    """Scheduled entry point: every closed bucket in the lookback for every site. Idempotent."""
    summary = {"sites": 0, "buckets": 0, "rows_inserted": 0}
    site_ids = list((await db.execute(select(Site.id).order_by(Site.id))).scalars())
    for site_id in site_ids:
        summary["sites"] += 1
        for bucket in closed_buckets(now, lookback_hours):
            summary["rows_inserted"] += await write_site_bucket(db, site_id, bucket, now)
            summary["buckets"] += 1
        await db.commit()
    return summary
