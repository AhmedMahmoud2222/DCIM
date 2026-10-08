"""Power analytics endpoints (Issue #102, areas B to G): capacity rollups, utilization history,
forecasts and queued operational reports. All reads need `power:read`; creating a report needs
`power:manage`. Report jobs belong to their requester: another user's job answers 404, so job ids give
no oracle. `power:*` is not a scope-aware permission, so a site-restricted user holds none of it and every
endpoint here answers 403 for them, the same fail-closed rule the rest of the power API follows."""

import json
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.outbox_service import write_outbox_event
from app.application.power_forecast import forecast_scope
from app.application.power_reports import render_csv
from app.application.power_rollup_loader import rollup_for_site
from app.application.rbac import require_permission
from app.core.errors import ApiError, NotFoundError
from app.core.logging import get_logger
from app.domain.location.models import Site
from app.domain.power.analytics_models import (
    REPORT_FORMATS,
    PowerReportJob,
    PowerUtilizationSnapshot,
)
from app.domain.power.models import PowerNode
from app.infrastructure.tasks.power_analytics import REPORTS_QUEUE, generate_power_report

logger = get_logger(__name__)
router = APIRouter(prefix="/power/analytics", tags=["power-analytics"])

SCOPE_TYPES = ("site", "power_node")
MAX_HISTORY_DAYS = 90


def _ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


async def _require_site(db: AsyncSession, site_id: uuid.UUID) -> None:
    if await db.get(Site, site_id) is None:
        raise NotFoundError(f"Site {site_id} not found.")


async def _require_scope(db: AsyncSession, scope_type: str, scope_id: uuid.UUID) -> None:
    if scope_type not in SCOPE_TYPES:
        raise ApiError(status_code=422, title="Unprocessable Entity", detail=f"scope_type must be one of {SCOPE_TYPES}.")
    if scope_type == "site":
        await _require_site(db, scope_id)
    elif await db.get(PowerNode, scope_id) is None:
        raise NotFoundError(f"PowerNode {scope_id} not found.")


# ------------------------------------------------------------------------------------ rollup


class NodeRollupOut(BaseModel):
    id: uuid.UUID
    node_type: str
    label: str
    rated_kw: float | None
    capacity_kw: float | None
    load_kw: float
    allocated_kw: float
    headroom_kw: float | None
    allocated_headroom_kw: float | None
    utilization_pct: float | None
    quality: str
    level: str
    protection_state: str | None
    interrupted: bool
    overloaded: bool
    measured_count: int
    stale_count: int
    estimated_count: int
    missing_count: int
    warnings: list[str]


class EquipmentRollupOut(BaseModel):
    id: uuid.UUID
    label: str
    scenario: str
    quality: str
    demand_kw: float | None
    allocated_kw: float | None
    served: bool
    live_inlets: int
    inlet_count: int


class ScopeRollupOut(BaseModel):
    scope: str
    id: uuid.UUID | None
    load_kw: float
    allocated_kw: float
    unserved_kw: float
    capacity_kw: float | None
    capacity_basis: str | None
    headroom_kw: float | None
    utilization_pct: float | None
    quality: str
    equipment_count: int


class RollupOut(BaseModel):
    site_id: uuid.UUID
    generated_at: datetime
    metric: str
    unit: str
    site: ScopeRollupOut
    rooms: list[ScopeRollupOut]
    racks: list[ScopeRollupOut]
    nodes: list[NodeRollupOut]
    equipment: list[EquipmentRollupOut]
    warnings: list[str]


@router.get("/sites/{site_id}/rollup", response_model=RollupOut)
async def get_site_rollup(
    site_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("power:read"))
) -> RollupOut:
    await _require_site(db, site_id)
    now = datetime.now(UTC)
    r = await rollup_for_site(db, site_id, now)

    def scope(s) -> ScopeRollupOut:
        return ScopeRollupOut(**s.__dict__)

    return RollupOut(
        site_id=site_id, generated_at=now, metric="power_kw", unit="kW", site=scope(r.site),
        rooms=[scope(s) for s in r.rooms.values()], racks=[scope(s) for s in r.racks.values()],
        nodes=[
            NodeRollupOut(
                id=n.id, node_type=n.node_type, label=n.label, rated_kw=n.rated_kw, capacity_kw=n.capacity_kw,
                load_kw=n.load_kw, allocated_kw=n.allocated_kw, headroom_kw=n.headroom_kw,
                allocated_headroom_kw=n.allocated_headroom_kw, utilization_pct=n.utilization_pct, quality=n.quality,
                level=n.level, protection_state=n.state, interrupted=n.interrupted, overloaded=n.overloaded,
                measured_count=n.measured_count, stale_count=n.stale_count, estimated_count=n.estimated_count,
                missing_count=n.missing_count, warnings=n.warnings,
            )
            for n in r.nodes.values()
        ],
        equipment=[
            EquipmentRollupOut(
                id=e.id, label=e.label, scenario=e.scenario, quality=e.quality, demand_kw=e.demand_kw,
                allocated_kw=e.allocated_kw, served=e.served, live_inlets=e.live_inlets, inlet_count=e.inlet_count,
            )
            for e in r.equipment.values()
        ],
        warnings=r.warnings,
    )


# ----------------------------------------------------------------------------------- history


class SnapshotOut(BaseModel):
    bucket_start: datetime
    bucket_end: datetime
    scope_type: str
    scope_id: uuid.UUID
    metric: str
    unit: str
    load_kw: float | None
    load_basis: str
    allocated_kw: float | None
    effective_capacity_kw: float | None
    headroom_kw: float | None
    utilization_pct: float | None
    sample_count: int
    expected_samples: int
    coverage_ratio: float
    quality: str
    window_start: datetime
    window_end: datetime
    method_version: str
    computed_at: datetime

    model_config = {"from_attributes": True}


@router.get("/history", response_model=Page[SnapshotOut])
async def get_history(
    scope_type: str, scope_id: uuid.UUID, start: datetime | None = None, end: datetime | None = None,
    db: AsyncSession = Depends(get_db), pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("power:read")),
) -> Page:
    await _require_scope(db, scope_type, scope_id)
    end = (end or datetime.now(UTC)).astimezone(UTC)
    start = (start or end - timedelta(days=7)).astimezone(UTC)
    if start >= end:
        raise ApiError(status_code=422, title="Unprocessable Entity", detail="start must be before end.")
    if end - start > timedelta(days=MAX_HISTORY_DAYS):
        raise ApiError(status_code=422, title="Unprocessable Entity", detail=f"Window is limited to {MAX_HISTORY_DAYS} days.")
    stmt = select(PowerUtilizationSnapshot).where(
        PowerUtilizationSnapshot.scope_type == scope_type, PowerUtilizationSnapshot.scope_id == scope_id,
        PowerUtilizationSnapshot.bucket_start >= start, PowerUtilizationSnapshot.bucket_start < end,
    )
    all_rows = (await db.execute(stmt.order_by(PowerUtilizationSnapshot.bucket_start))).scalars().all()
    rows = all_rows[pagination.offset : pagination.offset + pagination.limit]
    return Page(
        items=[SnapshotOut.model_validate(r) for r in rows], total=len(all_rows), limit=pagination.limit,
        offset=pagination.offset,
    )


# ---------------------------------------------------------------------------------- forecast


class ForecastOut(BaseModel):
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


@router.get("/forecast", response_model=ForecastOut)
async def get_forecast(
    scope_type: str, scope_id: uuid.UUID, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:read")),
) -> ForecastOut:
    await _require_scope(db, scope_type, scope_id)
    f = await forecast_scope(db, scope_type, scope_id, datetime.now(UTC))
    return ForecastOut(**f.__dict__)


# ------------------------------------------------------------------------------------ reports


class ReportIn(BaseModel):
    site_id: uuid.UUID | None = None
    format: str = "json"


class ReportJobOut(BaseModel):
    id: uuid.UUID
    report_type: str
    format: str
    site_id: uuid.UUID | None
    status: str
    attempts: int
    row_count: int | None
    failure_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    model_config = {"from_attributes": True}


async def _own_job(db: AsyncSession, job_id: uuid.UUID, ctx) -> PowerReportJob:
    job = await db.get(PowerReportJob, job_id)
    if job is None or job.requested_by_user_id != ctx.user.id:
        raise NotFoundError(f"Report {job_id} not found.")
    return job


@router.post("/reports", response_model=ReportJobOut, status_code=202)
async def create_report(
    body: ReportIn, request: Request, db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:manage")),
) -> ReportJobOut:
    if body.format not in REPORT_FORMATS:
        raise ApiError(status_code=422, title="Unprocessable Entity", detail=f"format must be one of {REPORT_FORMATS}.")
    if body.site_id is not None:
        await _require_site(db, body.site_id)
    job = PowerReportJob(
        requested_by_user_id=ctx.user.id, report_type="operational", format=body.format, site_id=body.site_id,
        status="queued", attempts=0,
    )
    db.add(job)
    await db.flush()
    request_id, correlation_id = _ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="power.report.request", entity_type="power_report_job",
        entity_id=job.id, request_id=request_id, correlation_id=correlation_id,
        after={"format": body.format, "site_id": str(body.site_id) if body.site_id else None},
    )
    await write_outbox_event(
        db, event_type="PowerReportRequested", aggregate_type="power_report_job", aggregate_id=job.id, payload={},
        correlation_id=correlation_id,
    )
    await db.commit()
    try:
        generate_power_report.apply_async(args=[str(job.id)], queue=REPORTS_QUEUE)
    except Exception as exc:
        job.status, job.failure_code, job.finished_at = "failed", "GENERATION_FAILED", datetime.now(UTC)
        await db.commit()
        logger.error("power_report_dispatch_failed", job_id=str(job.id), error_code=type(exc).__name__)
        raise ApiError(
            status_code=502, title="Bad Gateway", detail="The report could not be queued. Try again shortly."
        ) from exc
    return ReportJobOut.model_validate(job)


@router.get("/reports", response_model=Page[ReportJobOut])
async def list_reports(
    db: AsyncSession = Depends(get_db), pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("power:read")),
) -> Page:
    stmt = select(PowerReportJob).where(PowerReportJob.requested_by_user_id == ctx.user.id)
    rows = (await db.execute(stmt.order_by(PowerReportJob.created_at.desc(), PowerReportJob.id))).scalars().all()
    page = rows[pagination.offset : pagination.offset + pagination.limit]
    return Page(
        items=[ReportJobOut.model_validate(r) for r in page], total=len(rows), limit=pagination.limit,
        offset=pagination.offset,
    )


@router.get("/reports/{job_id}", response_model=ReportJobOut)
async def get_report(
    job_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("power:read"))
) -> ReportJobOut:
    return ReportJobOut.model_validate(await _own_job(db, job_id, ctx))


@router.get("/reports/{job_id}/download")
async def download_report(
    job_id: uuid.UUID, format: str | None = Query(default=None), db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("power:read")),
) -> Response:
    job = await _own_job(db, job_id, ctx)
    if job.status != "completed" or job.result is None:
        raise ApiError(status_code=409, title="Conflict", detail="The report is not ready.")
    fmt = format or job.format
    if fmt not in REPORT_FORMATS:
        raise ApiError(status_code=422, title="Unprocessable Entity", detail=f"format must be one of {REPORT_FORMATS}.")
    headers = {"X-Content-Type-Options": "nosniff"}
    if fmt == "csv":
        headers["Content-Disposition"] = f'attachment; filename="power-report-{job.id}.csv"'
        return Response(render_csv(job.result), media_type="text/csv", headers=headers)
    headers["Content-Disposition"] = f'attachment; filename="power-report-{job.id}.json"'
    return Response(json.dumps(job.result, sort_keys=True), media_type="application/json", headers=headers)
