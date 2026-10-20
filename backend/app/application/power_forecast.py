"""Deterministic, explainable capacity forecasting (Issue #102, area D).

Method `linear_ols_v1`: ordinary least squares on daily mean load (kW) built from hourly snapshots whose
load basis is `measured`. It reads only prepared snapshots, never raw telemetry, and uses no randomness,
so the same snapshots and `now` always give the same answer. Every result names its inputs and, when it
declines to forecast, says why.

Statuses (exactly one per result):
  good                 enough measured days, fresh, projects an exhaustion date within the horizon
  flat                 the trend is below the flat threshold; no exhaustion expected
  already_over_capacity current load is at or above capacity; exhaustion date is `now`
  stale                the newest snapshot is older than `STALE_AFTER_HOURS`; no projection
  sparse               fewer than `MIN_DAYS` measured days, or coverage below `MIN_COVERAGE`
  missing              no snapshots, or no capacity to compare against
  estimated_mixed      more than `MAX_ESTIMATED_SHARE` of the window rests on non-measured buckets
"""

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.power.analytics_models import PowerUtilizationSnapshot

METHOD = "linear_ols_v1"
WINDOW_DAYS = 30
HORIZON_DAYS = 365
MIN_DAYS = 7
MIN_COVERAGE = 0.5
STALE_AFTER_HOURS = 6
MAX_ESTIMATED_SHARE = 0.2
FLAT_PCT_OF_CAPACITY_PER_30D = 1.0


@dataclass(frozen=True)
class SnapshotPoint:
    bucket_start: datetime
    load_kw: float | None
    load_basis: str
    capacity_kw: float | None
    coverage_ratio: float
    sample_count: int


@dataclass
class Forecast:
    metric: str
    unit: str
    method: str
    status: str
    no_forecast_reason: str | None
    current_load_kw: float | None
    capacity_kw: float | None
    headroom_kw: float | None
    utilization_pct: float | None
    window_start: datetime
    window_end: datetime
    bucket_count: int
    measured_bucket_count: int
    day_count: int
    sample_count: int
    coverage_ratio: float | None
    slope_kw_per_day: float | None
    r_squared: float | None
    horizon_days: int
    exhaustion_date: datetime | None
    days_to_exhaustion: float | None
    confidence: str


def compute_forecast(points: list[SnapshotPoint], *, now: datetime, metric: str = "power_kw", unit: str = "kW") -> Forecast:
    now = now.astimezone(UTC)
    start = now - timedelta(days=WINDOW_DAYS)
    window = sorted((p for p in points if start <= p.bucket_start < now), key=lambda p: p.bucket_start)
    usable = [p for p in window if p.load_kw is not None]
    measured = [p for p in usable if p.load_basis == "measured"]
    latest = usable[-1] if usable else None
    capacity = next((p.capacity_kw for p in reversed(window) if p.capacity_kw is not None), None)
    samples = sum(p.sample_count for p in window)
    coverage = (sum(p.coverage_ratio for p in window) / len(window)) if window else None

    def out(status: str, reason: str | None, **kw) -> Forecast:
        base: dict[str, Any] = {
            "metric": metric, "unit": unit, "method": METHOD, "status": status, "no_forecast_reason": reason,
            "current_load_kw": latest.load_kw if latest else None, "capacity_kw": capacity,
            "headroom_kw": None, "utilization_pct": None, "window_start": start, "window_end": now,
            "bucket_count": len(window), "measured_bucket_count": len(measured), "day_count": 0,
            "sample_count": samples, "coverage_ratio": None if coverage is None else round(coverage, 4),
            "slope_kw_per_day": None, "r_squared": None, "horizon_days": HORIZON_DAYS, "exhaustion_date": None,
            "days_to_exhaustion": None, "confidence": "none",
        }
        base.update(kw)
        cur, cap = base["current_load_kw"], base["capacity_kw"]
        if cur is not None and cap is not None:
            base["headroom_kw"] = round(cap - cur, 4)
            base["utilization_pct"] = round(cur / cap * 100.0, 4) if cap > 0 else None
        return Forecast(**base)

    if not window or latest is None:
        return out("missing", "no utilization snapshots in the window")
    if capacity is None:
        return out("missing", "no capacity is recorded for this scope")
    if now - latest.bucket_start - timedelta(hours=1) > timedelta(hours=STALE_AFTER_HOURS):
        return out("stale", f"newest snapshot is older than {STALE_AFTER_HOURS} hours")
    cur = latest.load_kw or 0.0
    if cur >= capacity:
        return out(
            "already_over_capacity", "current load is at or above capacity", exhaustion_date=now,
            days_to_exhaustion=0.0, confidence="high" if latest.load_basis == "measured" else "low",
        )
    if len(usable) and (len(usable) - len(measured)) / len(usable) > MAX_ESTIMATED_SHARE:
        return out("estimated_mixed", "too much of the window rests on estimated or missing data")

    daily: dict[datetime, list[float]] = {}
    for p in measured:
        day = p.bucket_start.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        daily.setdefault(day, []).append(float(p.load_kw or 0.0))
    days = sorted(daily)
    if len(days) < MIN_DAYS or (coverage is not None and coverage < MIN_COVERAGE):
        return out(
            "sparse", f"needs at least {MIN_DAYS} measured days and {int(MIN_COVERAGE * 100)}% coverage",
            day_count=len(days),
        )
    xs = [(d - days[0]).total_seconds() / 86400.0 for d in days]
    ys = [sum(daily[d]) / len(daily[d]) for d in days]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True))
    slope = sxy / sxx if sxx else 0.0
    ss_tot = sum((y - my) ** 2 for y in ys)
    intercept = my - slope * mx
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys, strict=True))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    r2 = max(0.0, min(1.0, r2))
    confidence = "high" if r2 >= 0.7 and (coverage or 0) >= 0.9 else "medium" if r2 >= 0.4 else "low"
    common = {
        "day_count": len(days), "slope_kw_per_day": round(slope, 6), "r_squared": round(r2, 4),
        "confidence": confidence,
    }
    flat_limit = capacity * FLAT_PCT_OF_CAPACITY_PER_30D / 100.0 / 30.0
    if slope <= flat_limit:
        return out("flat", "load is flat or falling; no exhaustion expected", **common)
    days_left = (capacity - cur) / slope
    if not math.isfinite(days_left) or days_left > HORIZON_DAYS:
        return out("flat", f"projected exhaustion is beyond the {HORIZON_DAYS} day horizon", **common)
    return out(
        "good", None, exhaustion_date=now + timedelta(days=days_left), days_to_exhaustion=round(days_left, 2), **common
    )


async def load_points(db: AsyncSession, scope_type: str, scope_id: uuid.UUID, now: datetime) -> list[SnapshotPoint]:
    rows = (
        await db.execute(
            select(PowerUtilizationSnapshot)
            .where(
                PowerUtilizationSnapshot.scope_type == scope_type, PowerUtilizationSnapshot.scope_id == scope_id,
                PowerUtilizationSnapshot.bucket_start >= now - timedelta(days=WINDOW_DAYS),
                PowerUtilizationSnapshot.bucket_start < now,
            )
            .order_by(PowerUtilizationSnapshot.bucket_start)
        )
    ).scalars()
    return [
        SnapshotPoint(
            r.bucket_start, None if r.load_kw is None else float(r.load_kw), r.load_basis,
            None if r.effective_capacity_kw is None else float(r.effective_capacity_kw), float(r.coverage_ratio),
            r.sample_count,
        )
        for r in rows
    ]


async def forecast_scope(db: AsyncSession, scope_type: str, scope_id: uuid.UUID, now: datetime) -> Forecast:
    return compute_forecast(await load_points(db, scope_type, scope_id, now), now=now)
