"""Bounded performance buckets and full-window statistics from existing telemetry evidence."""

import math
import uuid
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import literal, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.application.access_control import equipment_in_site_clause, equipment_visible_clause
from app.application.rbac import require_permission
from app.application.telemetry_retention import as_utc
from app.core.errors import ApiError

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
MAX_BUCKETS = 300
MAX_SERIES = 32


class PerformancePoint(BaseModel):
    start: datetime
    end: datetime
    minimum: float
    maximum: float
    average: float
    sample_count: int
    resolution: Literal["raw", "daily", "mixed"]


class PerformanceSeries(BaseModel):
    series_key: str
    integration_id: uuid.UUID
    external_identifier: str
    metric: str
    unit: str
    registry_version: str | None
    sample_count: int
    minimum: float | None
    maximum: float | None
    average: float | None
    p50: float | None
    p95: float | None
    partial_days: int
    points: list[PerformancePoint]


class PerformanceWindow(BaseModel):
    series: list[PerformanceSeries]


# Both retention tiers and both levels of aggregation must share one MVCC snapshot.
# Only whole daily buckets contribute. An overlapping partial day remains visible as
# missing evidence, including when that is the sole evidence for the series.
_WINDOW = """
(
WITH evidence AS (
    SELECT series_key, integration_id, external_identifier, unit, registry_version,
           occurred_at AS instant, value AS low, value AS high, value AS total,
           1 AS samples, false AS daily, false AS partial
      FROM telemetry_reading
     WHERE managed_asset_id IN (SELECT id FROM selected_asset) AND metric = :metric
       AND occurred_at >= :start AND occurred_at < :end
    UNION ALL
    SELECT series_key, integration_id, external_identifier, unit, registry_version,
           day::timestamp AT TIME ZONE 'UTC', minimum_value, maximum_value,
           coalesce(sum_value, average_value * sample_count), sample_count, true,
           day::timestamp AT TIME ZONE 'UTC' < :start OR (day + 1)::timestamp AT TIME ZONE 'UTC' > :end
      FROM daily_telemetry_aggregate
     WHERE managed_asset_id IN (SELECT id FROM selected_asset) AND metric = :metric
       AND day >= :first_day AND day <= :last_day
), widths AS (
    SELECT *, bool_or(daily AND NOT partial) OVER
        (PARTITION BY series_key, integration_id, external_identifier, unit, registry_version) AS has_daily
      FROM evidence
), bins AS (
    SELECT *, CASE WHEN has_daily THEN
             date_bin(CAST(:daily_stride AS interval), instant, CAST(:midnight AS timestamptz))
           ELSE date_bin(CAST(:raw_stride AS interval), instant, CAST(:start AS timestamptz)) END AS bucket
      FROM widths
)
SELECT series_key, integration_id, external_identifier, unit, registry_version, bucket,
       grouping(bucket) AS summary, bool_or(has_daily) AS has_daily,
       count(*) FILTER (WHERE partial) AS partial_days,
       coalesce(sum(samples) FILTER (WHERE NOT partial), 0) AS sample_count,
       min(low) FILTER (WHERE NOT partial) AS minimum, max(high) FILTER (WHERE NOT partial) AS maximum,
       sum(total) FILTER (WHERE NOT partial) / nullif(sum(samples) FILTER (WHERE NOT partial), 0) AS average,
       CASE WHEN grouping(bucket) = 1 AND NOT bool_or(daily) THEN
            percentile_cont(0.5) WITHIN GROUP (ORDER BY low) END AS p50,
       CASE WHEN grouping(bucket) = 1 AND NOT bool_or(daily) THEN
            percentile_cont(0.95) WITHIN GROUP (ORDER BY low) END AS p95,
       CASE WHEN bool_and(daily) FILTER (WHERE NOT partial) THEN 'daily'
            WHEN bool_or(daily) FILTER (WHERE NOT partial) THEN 'mixed' ELSE 'raw' END AS resolution
  FROM bins
 GROUP BY series_key, integration_id, external_identifier, unit, registry_version, GROUPING SETS ((), (bucket))
 ORDER BY series_key, integration_id, external_identifier, unit, registry_version, summary DESC, bucket
 LIMIT :row_limit
) AS performance
"""


@router.get("/performance", response_model=PerformanceWindow)
async def equipment_performance(
    managed_asset_id: uuid.UUID,
    metric: str = Query(min_length=1, max_length=64),
    start: datetime = Query(),
    end: datetime = Query(),
    site_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("telemetry:read")),
) -> PerformanceWindow:
    start, end = as_utc(start), as_utc(end)
    duration = end - start
    if not timedelta(0) < duration <= timedelta(days=366):
        raise ApiError(status_code=422, title="Invalid time range", detail="Select a positive range of at most 366 days.")

    asset_id = literal(managed_asset_id)
    allowed = select(asset_id.label("id")).where(equipment_visible_clause(ctx.scope, asset_id))
    if site_id is not None:
        allowed = allowed.where(equipment_in_site_clause(site_id, asset_id))

    raw_stride = timedelta(seconds=max(1, math.ceil(duration.total_seconds() / MAX_BUCKETS)))
    midnight = start.replace(hour=0, minute=0, second=0, microsecond=0)
    last_day = (end - timedelta(microseconds=1)).date()
    calendar_days = (last_day - midnight.date()).days + 1
    daily_stride = timedelta(days=math.ceil(calendar_days / MAX_BUCKETS))
    # A partial day preceding a raw-only series may add one empty bucket before
    # the first sample; budget that and a summary row per series, plus an overflow sentinel.
    row_limit = MAX_SERIES * (MAX_BUCKETS + 2) + 1
    statement = (
        select(text("*")).select_from(text(_WINDOW)).add_cte(allowed.cte("selected_asset"))
        .order_by(text("series_key, integration_id, external_identifier, unit, registry_version, summary DESC, bucket"))
    )
    rows = (await db.execute(statement, {
        "metric": metric, "start": start, "end": end, "first_day": start.date(), "last_day": last_day,
        "raw_stride": raw_stride, "daily_stride": daily_stride, "midnight": midnight, "row_limit": row_limit,
    })).mappings()
    result: list[PerformanceSeries] = []
    for row in rows:
        if row["summary"]:
            if len(result) == MAX_SERIES:
                raise ApiError(status_code=422, title="Too many series", detail="Equipment and metric exceed 32 series.")
            result.append(PerformanceSeries(
                series_key=row["series_key"], integration_id=row["integration_id"],
                external_identifier=row["external_identifier"], metric=metric, unit=row["unit"],
                registry_version=row["registry_version"], minimum=row["minimum"], maximum=row["maximum"],
                average=row["average"], p50=row["p50"], p95=row["p95"], partial_days=row["partial_days"],
                sample_count=row["sample_count"], points=[],
            ))
        elif row["sample_count"]:
            stride = daily_stride if row["has_daily"] else raw_stride
            result[-1].points.append(PerformancePoint(
                start=max(start, row["bucket"]), end=min(end, row["bucket"] + stride), minimum=row["minimum"],
                maximum=row["maximum"], average=row["average"], sample_count=row["sample_count"], resolution=row["resolution"],
            ))
    return PerformanceWindow(series=result)
