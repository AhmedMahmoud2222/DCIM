# ruff: noqa: E501
"""Floor-plan CRUD, secure upload/import (ARCHITECTURE_REVIEW.md §8/§9/§10/§10a/§11), and
the accept/reject review queue that keeps imported geometry from ever becoming
authoritative inventory without an explicit human decision (§21 of the Phase 2 prompt)."""

import base64
import hashlib
import math
import uuid
from datetime import UTC, datetime
from typing import Literal, cast

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Table, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.concurrency import lock_versioned_row, require_if_match
from app.application.outbox_service import write_outbox_event
from app.application.placement_service import (
    PlacementConflict,
    get_current_equipment_placement,
    get_current_rack_placement,
    move_rack,
)
from app.application.rbac import require_permission
from app.application.spatial_import import calibration as cal
from app.application.spatial_import import service as spatial_service
from app.application.spatial_import.boundary import assert_within_boundary, load_boundary_object
from app.application.spatial_import.formats import UnsupportedFormat, check_declared_matches, detect_format
from app.application.spatial_import.geometry import Point, polygon_area, rect_corners, rect_iou
from app.application.spatial_import.sir import SirInvalid, validate_sir
from app.application.spatial_validation import validate_coordinate
from app.application.svg_sanitizer import MAX_RASTER_FILE_SIZE_BYTES, MAX_SVG_FILE_SIZE_BYTES
from app.core.errors import ApiError, ConflictError, NotFoundError
from app.domain.floorplan_import.models import (
    FloorPlanImportCandidate,
    FloorPlanImportDiagnostics,
    FloorPlanImportJob,
    FloorPlanImportSir,
)
from app.domain.location.models import Room
from app.domain.physical.models import Equipment, Rack
from app.domain.placement.models import EquipmentPlacement, RackPlacement
from app.domain.spatial.models import OBJECT_TYPES, FloorPlan, FloorPlanCalibration, SpatialLayer, SpatialObject
from app.infrastructure.tasks.floorplan_import import (
    run_floor_plan_import_job,
    run_spatial_import_job,
    validate_and_run_raster_import_job,
)

router = APIRouter(prefix="/floor-plans", tags=["floor-plans"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


# --------------------------------------------------------------------------- FloorPlan
class FloorPlanIn(BaseModel):
    room_id: uuid.UUID
    room_width_mm: int | None = None
    room_height_mm: int | None = None


class CalibrationOut(BaseModel):
    id: uuid.UUID
    sequence: int
    method: str
    source_units: str
    mm_per_unit: float
    origin_x: float
    origin_y: float
    y_axis: str
    rotation_quadrants: int
    error_bound_mm: float | None
    relative_error: float | None
    confidence: str
    reference: dict
    warnings: list
    job_id: uuid.UUID | None
    supersedes_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class FloorPlanOut(BaseModel):
    id: uuid.UUID
    room_id: uuid.UUID
    revision_number: int
    status: str
    source_file_name: str | None
    source_format: str | None
    calibration_scale_mm_per_px: float | None
    room_width_mm: int | None
    room_height_mm: int | None
    version: int
    created_at: datetime
    current_calibration: CalibrationOut | None = None

    model_config = {"from_attributes": True}


async def _floor_plan_outs(db: AsyncSession, plans: list[FloorPlan]) -> list[FloorPlanOut]:
    ids = {p.current_calibration_id for p in plans if p.current_calibration_id is not None}
    calibrations: dict[uuid.UUID, FloorPlanCalibration] = {}
    if ids:
        calibrations = {
            c.id: c for c in (await db.execute(select(FloorPlanCalibration).where(FloorPlanCalibration.id.in_(ids)))).scalars()
        }
    out: list[FloorPlanOut] = []
    for plan in plans:
        item = FloorPlanOut.model_validate(plan)
        calibration = calibrations.get(plan.current_calibration_id) if plan.current_calibration_id else None
        item.current_calibration = CalibrationOut.model_validate(calibration) if calibration is not None else None
        out.append(item)
    return out


@router.post("", response_model=FloorPlanOut, status_code=201)
async def create_floor_plan(
    body: FloorPlanIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> FloorPlanOut:
    if await db.get(Room, body.room_id) is None:
        raise NotFoundError(f"Room {body.room_id} not found.")
    max_revision = (
        await db.execute(select(func.max(FloorPlan.revision_number)).where(FloorPlan.room_id == body.room_id))
    ).scalar_one()
    floor_plan = FloorPlan(
        room_id=body.room_id, revision_number=(max_revision or 0) + 1, status="draft",
        room_width_mm=body.room_width_mm, room_height_mm=body.room_height_mm,
        created_by_user_id=ctx.user.id,
    )
    db.add(floor_plan)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.create", entity_type="floor_plan", entity_id=floor_plan.id,
        request_id=request_id, correlation_id=correlation_id, after={"room_id": str(body.room_id)},
    )
    await db.commit()
    await db.refresh(floor_plan)
    return (await _floor_plan_outs(db, [floor_plan]))[0]


@router.get("", response_model=Page[FloorPlanOut])
async def list_floor_plans(
    room_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("floor_plan:read")),
) -> Page:
    stmt = select(FloorPlan)
    count_stmt = select(func.count()).select_from(FloorPlan)
    if room_id is not None:
        stmt = stmt.where(FloorPlan.room_id == room_id)
        count_stmt = count_stmt.where(FloorPlan.room_id == room_id)
    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(stmt.order_by(FloorPlan.revision_number.desc()).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
    return Page(items=await _floor_plan_outs(db, list(rows)), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/{floor_plan_id}", response_model=FloorPlanOut)
async def get_floor_plan(
    floor_plan_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("floor_plan:read"))
) -> FloorPlanOut:
    floor_plan = await db.get(FloorPlan, floor_plan_id)
    if floor_plan is None:
        raise NotFoundError(f"FloorPlan {floor_plan_id} not found.")
    return (await _floor_plan_outs(db, [floor_plan]))[0]


@router.post("/{floor_plan_id}/activate", response_model=FloorPlanOut)
async def activate_floor_plan(
    floor_plan_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> FloorPlanOut:
    """§8: "exactly one ACTIVE FloorPlan at a time; prior ones retained, status=superseded".
    The partial unique index (migration 0004) is the hard guarantee; this supersedes
    whatever was previously active for the room in the same transaction."""
    # Concurrency: the invariant spans rows (one active plan per room), so a lock on the
    # plan alone is not enough: two *different* plans of one room would each lock only
    # themselves. Serialize per room instead (FOR NO KEY UPDATE on the Room row, which
    # does not block FK checks from inserting plans), then lock the plan and compare
    # If-Match against its post-lock version. The partial unique index remains as defense
    # in depth; the controlled 409s below mean it should never be what rejects a request.
    candidate = await db.get(FloorPlan, floor_plan_id)
    if candidate is None:
        raise NotFoundError(f"FloorPlan {floor_plan_id} not found.")
    observed_active_id = (
        await db.execute(
            select(FloorPlan.id).where(FloorPlan.room_id == candidate.room_id, FloorPlan.status == "active")
        )
    ).scalar_one_or_none()
    await db.execute(select(Room.id).where(Room.id == candidate.room_id).with_for_update(key_share=True))
    floor_plan = await lock_versioned_row(db, FloorPlan, floor_plan_id, expected_version=if_match_version)
    previously_active = (
        await db.execute(
            select(FloorPlan)
            .where(FloorPlan.room_id == floor_plan.room_id, FloorPlan.status == "active")
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if (previously_active.id if previously_active is not None else None) != observed_active_id:
        # The room's active plan changed while this request waited for the room lock: the
        # caller decided against state that is no longer current.
        raise ConflictError(detail="This room's active floor plan was changed by another request; reload and retry.")
    if previously_active is not None and previously_active.id != floor_plan.id:
        previously_active.status = "superseded"
        previously_active.version += 1
        # Flushed separately, before this floor plan is marked active: the partial
        # unique index (uq_floor_plan_one_active_per_room) is a plain index-backed
        # constraint, not a deferrable one (PostgreSQL does not support a deferrable
        # UNIQUE INDEX with a WHERE clause), so PostgreSQL checks it per-statement —
        # if both UPDATEs were flushed in the same round trip in the wrong order, the
        # database would transiently see two rows active for the same room and reject
        # it even though the net result is a single active row.
        await db.flush()

    floor_plan.status = "active"
    floor_plan.version += 1
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.activate", entity_type="floor_plan", entity_id=floor_plan.id,
        request_id=request_id, correlation_id=correlation_id, after={"status": "active"},
    )
    await write_outbox_event(
        db, event_type="FloorPlanRevisionCreated", aggregate_type="floor_plan", aggregate_id=floor_plan.id,
        payload={"room_id": str(floor_plan.room_id), "revision_number": floor_plan.revision_number},
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(floor_plan)
    return (await _floor_plan_outs(db, [floor_plan]))[0]


# --------------------------------------------------------------------------- Upload / Import
class ImportJobOut(BaseModel):
    id: uuid.UUID
    floor_plan_id: uuid.UUID
    status: str
    original_filename: str
    file_size_bytes: int
    file_hash: str
    detected_format: str | None = None
    rejection_reason: str | None
    created_at: datetime
    deduplicated: bool = False

    model_config = {"from_attributes": True}


_FORMAT_SIZE_CAP = {"svg": MAX_SVG_FILE_SIZE_BYTES}


async def _existing_live_job(db: AsyncSession, floor_plan_id: uuid.UUID, file_hash: str) -> FloorPlanImportJob | None:
    return (
        await db.execute(
            select(FloorPlanImportJob).where(
                FloorPlanImportJob.floor_plan_id == floor_plan_id, FloorPlanImportJob.dedup_key == file_hash
            )
        )
    ).scalar_one_or_none()


@router.post("/{floor_plan_id}/upload", response_model=ImportJobOut, status_code=202)
async def upload_floor_plan_file(
    floor_plan_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    ctx=Depends(require_permission("floor_plan:import")),
) -> FloorPlanImportJob:
    """Untrusted input (§10a) — never trusted by extension; content is sniffed and size-capped before
    anything parses it, and a declared extension/Content-Type that names a *different* known spatial format
    than the content is rejected. SVG, PNG and JPEG are handled as before; DXF and VSDX are parsed only in the
    isolated sandboxed child (app.application.spatial_import). Async (§36): 202 plus a job the caller polls;
    an identical re-upload to the same floor plan returns the existing live job instead of a duplicate."""
    floor_plan = await db.get(FloorPlan, floor_plan_id)
    if floor_plan is None:
        raise NotFoundError(f"FloorPlan {floor_plan_id} not found.")

    content = await file.read()
    if len(content) > max(MAX_SVG_FILE_SIZE_BYTES, MAX_RASTER_FILE_SIZE_BYTES):
        raise ApiError(status_code=413, title="Payload Too Large", detail="Uploaded file exceeds the maximum allowed size.")

    # Never trust the client-supplied filename for anything beyond display.
    original_filename = (file.filename or "upload")[:255]
    file_hash = hashlib.sha256(content).hexdigest()

    try:
        source_format = detect_format(content)
        check_declared_matches(source_format, file.filename, file.content_type)
    except UnsupportedFormat as exc:
        raise ApiError(
            status_code=422, title="Unsupported File Type" if exc.code != "format_mismatch" else "File Type Mismatch",
            detail=exc.detail,
        ) from exc

    existing = await _existing_live_job(db, floor_plan_id, file_hash)
    if existing is not None:
        existing.deduplicated = True
        return existing

    job = FloorPlanImportJob(
        floor_plan_id=floor_plan_id, uploaded_by_user_id=ctx.user.id, status="queued",
        original_filename=original_filename, file_hash=file_hash, file_size_bytes=len(content),
        detected_format=source_format, declared_format=(file.content_type or "")[:32] or None, dedup_key=file_hash,
    )
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        # A concurrent identical upload won the unique (floor_plan_id, dedup_key) index.
        existing = await _existing_live_job(db, floor_plan_id, file_hash)
        if existing is None:
            raise
        existing.deduplicated = True
        return existing

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.import_upload", entity_type="floor_plan_import_job",
        entity_id=job.id, request_id=request_id, correlation_id=correlation_id,
        after={"filename": original_filename, "file_hash": file_hash, "size_bytes": len(content), "format": source_format},
    )
    await db.commit()
    await db.refresh(job)

    content_b64 = base64.b64encode(content).decode("ascii")
    if source_format == "svg":
        run_floor_plan_import_job.delay(str(job.id), content_b64)
    elif source_format in ("dxf", "vsdx"):
        run_spatial_import_job.delay(str(job.id), content_b64, source_format)
    else:
        validate_and_run_raster_import_job.delay(str(job.id), content_b64, source_format)

    return job


@router.get("/{floor_plan_id}/import-jobs", response_model=Page[ImportJobOut])
async def list_import_jobs(
    floor_plan_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("floor_plan:read")),
) -> Page:
    stmt = select(FloorPlanImportJob).where(FloorPlanImportJob.floor_plan_id == floor_plan_id)
    count_stmt = (
        select(func.count())
        .select_from(FloorPlanImportJob)
        .where(FloorPlanImportJob.floor_plan_id == floor_plan_id)
    )
    total = (await db.execute(count_stmt)).scalar_one()
    paged_stmt = (
        stmt.order_by(FloorPlanImportJob.created_at.desc())
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = (await db.execute(paged_stmt)).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/import-jobs/{job_id}", response_model=ImportJobOut)
async def get_import_job(
    job_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("floor_plan:read"))
) -> FloorPlanImportJob:
    job = await db.get(FloorPlanImportJob, job_id)
    if job is None:
        raise NotFoundError(f"FloorPlanImportJob {job_id} not found.")
    return job


class ImportDiagnosticsOut(BaseModel):
    job_id: uuid.UUID
    source_format: str | None
    parser_name: str | None
    parser_version: str | None = None
    objects_discovered: int
    objects_classified: int
    racks_detected: int
    unsupported_object_count: int
    warnings: list
    errors: list
    ambiguous_count: int
    confirmed_count: int
    duration_ms: int | None
    source_units: str | None = None
    units_trusted: bool | None = None
    y_axis: str | None = None
    source_bbox: dict | None = None
    candidate_count: int = 0
    sir_sha256: str | None = None
    failure_code: str | None = None

    model_config = {"from_attributes": True}


@router.get("/import-jobs/{job_id}/diagnostics", response_model=ImportDiagnosticsOut)
async def get_import_diagnostics(
    job_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("floor_plan:read"))
) -> FloorPlanImportDiagnostics:
    stmt = select(FloorPlanImportDiagnostics).where(FloorPlanImportDiagnostics.job_id == job_id)
    diagnostics = (await db.execute(stmt)).scalar_one_or_none()
    if diagnostics is None:
        raise NotFoundError(f"Diagnostics for job {job_id} not found (job may still be queued).")
    return diagnostics


class SourceGeometryOut(BaseModel):
    job_id: uuid.UUID
    source_format: str
    source_units: str
    units_trusted: bool
    y_axis: str
    bbox: dict | None
    layers: list[str]
    sir_sha256: str
    entities: list[dict]
    total_entities: int
    truncated: bool


@router.get("/import-jobs/{job_id}/source-geometry", response_model=SourceGeometryOut)
async def get_source_geometry(
    job_id: uuid.UUID,
    limit: int = 5000,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("floor_plan:read")),
) -> SourceGeometryOut:
    """The immutable sanitized intermediate representation (bounded), so the review canvas can draw the source
    drawing's linework behind the candidates. Read-only; it is not inventory."""
    stored = (await db.execute(select(FloorPlanImportSir).where(FloorPlanImportSir.job_id == job_id))).scalar_one_or_none()
    if stored is None:
        raise NotFoundError(f"No parsed geometry for job {job_id}.")
    limit = max(1, min(limit, 20_000))
    entities = stored.sir.get("entities", [])
    return SourceGeometryOut(
        job_id=job_id, source_format=stored.sir.get("source_format", ""), source_units=stored.sir.get("source_units", "unitless"),
        units_trusted=bool(stored.sir.get("units_trusted", False)), y_axis=stored.sir.get("y_axis", "down"),
        bbox=stored.sir.get("bbox"), layers=list(stored.sir.get("layers", [])), sir_sha256=stored.sir_sha256,
        entities=entities[:limit], total_entities=len(entities), truncated=len(entities) > limit,
    )


# --------------------------------------------------------------------------- Calibration
class CalibrationIn(BaseModel):
    method: Literal["declared_units", "two_point", "room_dimension", "manual_scale"]
    job_id: uuid.UUID
    origin: tuple[float, float] | None = None
    rotation_degrees: Literal[0, 90, 180, 270] = 0
    # two_point
    p1: tuple[float, float] | None = None
    p2: tuple[float, float] | None = None
    distance_mm: float | None = Field(default=None, gt=0, le=1_000_000)
    tolerance_mm: float = Field(default=1.0, ge=0, le=100_000)
    pick_tolerance_src: float = Field(default=0.0, ge=0)
    # room_dimension
    src_width: float | None = Field(default=None, gt=0)
    real_width_mm: float | None = Field(default=None, gt=0, le=1_000_000)
    src_height: float | None = Field(default=None, gt=0)
    real_height_mm: float | None = Field(default=None, gt=0, le=1_000_000)
    # manual_scale
    mm_per_unit: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _required_fields(self) -> "CalibrationIn":
        if self.method == "two_point" and (self.p1 is None or self.p2 is None or self.distance_mm is None):
            raise ValueError("two_point calibration needs p1, p2 and distance_mm")
        if self.method == "room_dimension" and (self.src_width is None or self.real_width_mm is None):
            raise ValueError("room_dimension calibration needs src_width and real_width_mm")
        if self.method == "manual_scale" and self.mm_per_unit is None:
            raise ValueError("manual_scale calibration needs mm_per_unit")
        for pair in (self.origin, self.p1, self.p2):
            if pair is not None and not all(math.isfinite(v) and abs(v) <= 1e9 for v in pair):
                raise ValueError("coordinates must be finite and within 1e9")
        return self


class CalibrationResultOut(BaseModel):
    floor_plan: FloorPlanOut
    calibration: CalibrationOut
    reconciled: dict[str, int]


async def _load_job_document(db: AsyncSession, job: FloorPlanImportJob):
    stored = (await db.execute(select(FloorPlanImportSir).where(FloorPlanImportSir.job_id == job.id))).scalar_one_or_none()
    if stored is None:
        raise ApiError(status_code=409, title="Import Not Ready", detail="This import has no parsed geometry to calibrate against.")
    try:
        return validate_sir(stored.sir)
    except SirInvalid as exc:
        raise ApiError(status_code=409, title="Import Corrupt", detail=f"Stored geometry failed validation ({exc.code}).") from exc


@router.get("/{floor_plan_id}/calibrations", response_model=list[CalibrationOut])
async def list_calibrations(
    floor_plan_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("floor_plan:read"))
) -> list[FloorPlanCalibration]:
    if await db.get(FloorPlan, floor_plan_id) is None:
        raise NotFoundError(f"FloorPlan {floor_plan_id} not found.")
    stmt = select(FloorPlanCalibration).where(FloorPlanCalibration.floor_plan_id == floor_plan_id).order_by(FloorPlanCalibration.sequence.desc())
    return list((await db.execute(stmt)).scalars())


@router.post("/{floor_plan_id}/calibration", response_model=CalibrationResultOut, status_code=201)
async def set_calibration(
    floor_plan_id: uuid.UUID,
    body: CalibrationIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> CalibrationResultOut:
    """Records a new immutable calibration and makes it current. Recalibration is allowed until the first
    candidate is accepted with the current one (after that, accepted geometry would no longer share the
    calibration it was created under): 409 then. Candidate match evidence is re-derived against the new
    transform in the same transaction; nothing authoritative moves."""
    floor_plan = await lock_versioned_row(db, FloorPlan, floor_plan_id, expected_version=if_match_version, label="FloorPlan")
    job = await db.get(FloorPlanImportJob, body.job_id)
    if job is None or job.floor_plan_id != floor_plan.id:
        raise NotFoundError(f"Import job {body.job_id} not found for this floor plan.")
    if job.status != "parsed":
        raise ApiError(status_code=409, title="Import Not Ready", detail=f"Import job is {job.status}; calibrate once it is parsed.")
    document = await _load_job_document(db, job)

    if floor_plan.current_calibration_id is not None:
        accepted = (
            await db.execute(
                select(func.count()).select_from(SpatialObject).join(SpatialLayer, SpatialLayer.id == SpatialObject.spatial_layer_id)
                .where(
                    SpatialLayer.floor_plan_id == floor_plan.id,
                    SpatialObject.provenance["calibration_id"].astext == str(floor_plan.current_calibration_id),
                )
            )
        ).scalar_one()
        if accepted:
            raise ApiError(
                status_code=409, title="Calibration Locked",
                detail=f"{accepted} accepted shape(s) were created under the current calibration; recalibrate on a new floor plan revision.",
            )

    quadrants = body.rotation_degrees // 90
    try:
        if body.method == "declared_units":
            params = cal.calibrate_declared_units(document, origin=body.origin, quadrants=quadrants)
        elif body.method == "two_point":
            assert body.p1 and body.p2 and body.distance_mm
            params = cal.calibrate_two_point(
                document, p1=body.p1, p2=body.p2, distance_mm=body.distance_mm, tolerance_mm=body.tolerance_mm,
                pick_tolerance_src=body.pick_tolerance_src, origin=body.origin, quadrants=quadrants,
            )
        elif body.method == "room_dimension":
            assert body.src_width and body.real_width_mm
            params = cal.calibrate_room_dimension(
                document, src_width=body.src_width, real_width_mm=body.real_width_mm, src_height=body.src_height,
                real_height_mm=body.real_height_mm, tolerance_mm=body.tolerance_mm, origin=body.origin, quadrants=quadrants,
            )
        else:
            assert body.mm_per_unit
            params = cal.calibrate_manual_scale(document, mm_per_unit=body.mm_per_unit, origin=body.origin, quadrants=quadrants)
    except cal.CalibrationError as exc:
        raise ApiError(status_code=422, title="Calibration Rejected", detail=exc.detail) from exc

    max_sequence = (
        await db.execute(select(func.max(FloorPlanCalibration.sequence)).where(FloorPlanCalibration.floor_plan_id == floor_plan.id))
    ).scalar_one()
    row = FloorPlanCalibration(
        floor_plan_id=floor_plan.id, sequence=(max_sequence or 0) + 1, job_id=job.id, supersedes_id=floor_plan.current_calibration_id,
        method=params.method, source_units=params.source_units, mm_per_unit=params.mm_per_unit, origin_x=params.origin_x,
        origin_y=params.origin_y, y_axis=params.y_axis, rotation_quadrants=params.rotation_quadrants,
        error_bound_mm=params.error_bound_mm, relative_error=params.relative_error, confidence=params.confidence,
        reference=params.reference, warnings=list(params.warnings), created_by_user_id=ctx.user.id, created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    floor_plan.current_calibration_id = row.id
    floor_plan.calibration_scale_mm_per_px = params.mm_per_unit if params.source_units == "px" else None
    floor_plan.version += 1
    await db.flush()

    reconciled = await spatial_service.reconcile_job(db, floor_plan=floor_plan, job=job, calibration=row)

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.calibrate", entity_type="floor_plan", entity_id=floor_plan.id,
        request_id=request_id, correlation_id=correlation_id,
        after={
            "calibration_id": str(row.id), "method": params.method, "mm_per_unit": params.mm_per_unit,
            "error_bound_mm": params.error_bound_mm, "confidence": params.confidence, "job_id": str(job.id),
        },
    )
    await write_outbox_event(
        db, event_type="FloorPlanCalibrated", aggregate_type="floor_plan", aggregate_id=floor_plan.id,
        payload={"calibration_id": str(row.id), "method": params.method}, correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(floor_plan)
    await db.refresh(row)
    return CalibrationResultOut(
        floor_plan=(await _floor_plan_outs(db, [floor_plan]))[0], calibration=CalibrationOut.model_validate(row), reconciled=reconciled
    )


@router.post("/import-jobs/{job_id}/reconcile", response_model=dict[str, int])
async def reconcile_import_job(
    job_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("floor_plan:manage"))
) -> dict[str, int]:
    """Re-run the advisory rack match against the room's current racks (e.g. after racks were placed)."""
    job = await db.get(FloorPlanImportJob, job_id)
    if job is None:
        raise NotFoundError(f"FloorPlanImportJob {job_id} not found.")
    floor_plan = (
        await db.execute(select(FloorPlan).where(FloorPlan.id == job.floor_plan_id).with_for_update())
    ).scalar_one()
    calibration = await spatial_service.current_calibration(db, floor_plan)
    if calibration is None:
        raise ApiError(status_code=409, title="Calibration Required", detail="Calibrate the floor plan before matching racks.")
    counts = await spatial_service.reconcile_job(db, floor_plan=floor_plan, job=job, calibration=calibration)
    await db.commit()
    return counts


# --------------------------------------------------------------------------- Candidates
class ImportCandidateOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    version: int
    raw_geometry: dict
    effective_geometry: dict
    canonical: dict | None = None
    suggested_object_type: str | None
    effective_object_type: str | None
    suggested_label: str | None
    effective_label: str | None
    confidence: float | None
    evidence: list
    match_status: str
    match_score: float | None
    matched_asset_id: uuid.UUID | None
    matched_asset_name: str | None = None
    duplicate_of_spatial_object_id: uuid.UUID | None
    reconciled_calibration_id: uuid.UUID | None
    source_ref: str | None
    correction: dict | None
    can_undo: bool
    status: str
    resulting_spatial_object_id: uuid.UUID | None


async def _candidate_outs(
    db: AsyncSession, candidates: list[FloorPlanImportCandidate], floor_plan: FloorPlan | None
) -> list[ImportCandidateOut]:
    params = None
    if floor_plan is not None:
        row = await spatial_service.current_calibration(db, floor_plan)
        params = spatial_service.params_from_row(row) if row is not None else None
    asset_ids = {c.matched_asset_id for c in candidates if c.matched_asset_id is not None}
    names: dict[uuid.UUID, str] = {}
    if asset_ids:
        names = {r.id: r.name for r in (await db.execute(select(Rack).where(Rack.id.in_(asset_ids)))).scalars()}
    out: list[ImportCandidateOut] = []
    for c in candidates:
        geometry = spatial_service.effective_geometry(c)
        out.append(
            ImportCandidateOut(
                id=c.id, job_id=c.job_id, version=c.version, raw_geometry=c.raw_geometry, effective_geometry=geometry,
                canonical=spatial_service.to_canonical(geometry, params) if params is not None else None,
                suggested_object_type=c.suggested_object_type, effective_object_type=spatial_service.effective_object_type(c),
                suggested_label=c.suggested_label, effective_label=spatial_service.effective_label(c),
                confidence=None if c.confidence is None else float(c.confidence), evidence=list(c.evidence or []),
                match_status=c.match_status, match_score=None if c.match_score is None else float(c.match_score),
                matched_asset_id=c.matched_asset_id, matched_asset_name=names.get(c.matched_asset_id) if c.matched_asset_id else None,
                duplicate_of_spatial_object_id=c.duplicate_of_spatial_object_id,
                reconciled_calibration_id=c.reconciled_calibration_id, source_ref=c.source_ref, correction=c.correction,
                can_undo=bool(c.correction_history), status=c.status, resulting_spatial_object_id=c.resulting_spatial_object_id,
            )
        )
    return out


async def _job_floor_plan(db: AsyncSession, job_id: uuid.UUID) -> tuple[FloorPlanImportJob, FloorPlan]:
    job = await db.get(FloorPlanImportJob, job_id)
    if job is None:
        raise NotFoundError(f"FloorPlanImportJob {job_id} not found.")
    floor_plan = await db.get(FloorPlan, job.floor_plan_id)
    assert floor_plan is not None
    return job, floor_plan


@router.get("/import-jobs/{job_id}/candidates", response_model=Page[ImportCandidateOut])
async def list_import_candidates(
    job_id: uuid.UUID,
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    pagination: Pagination = Depends(pagination_params),
    ctx=Depends(require_permission("floor_plan:read")),
) -> Page:
    stmt = select(FloorPlanImportCandidate).where(FloorPlanImportCandidate.job_id == job_id)
    count_stmt = select(func.count()).select_from(FloorPlanImportCandidate).where(FloorPlanImportCandidate.job_id == job_id)
    if status is not None:
        stmt = stmt.where(FloorPlanImportCandidate.status == status)
        count_stmt = count_stmt.where(FloorPlanImportCandidate.status == status)
    total = (await db.execute(count_stmt)).scalar_one()
    paged_stmt = (
        stmt.order_by(FloorPlanImportCandidate.ordinal.asc().nulls_last(), FloorPlanImportCandidate.created_at, FloorPlanImportCandidate.id)
        .offset(pagination.offset)
        .limit(pagination.limit)
    )
    rows = list((await db.execute(paged_stmt)).scalars().all())
    floor_plan = None
    job = await db.get(FloorPlanImportJob, job_id)
    if job is not None:
        floor_plan = await db.get(FloorPlan, job.floor_plan_id)
    return Page(items=await _candidate_outs(db, rows, floor_plan), total=total, limit=pagination.limit, offset=pagination.offset)


def _finite(value: float | None) -> float | None:
    if value is not None and (not math.isfinite(value) or abs(value) > 1e9):
        raise ValueError("value out of range")
    return value


class CandidateCorrectionIn(BaseModel):
    """Staged operator correction, in the *source* coordinates of the drawing (so recalibration re-maps it).
    Only fields present in the request change; `clear_*` flags remove a staged value."""

    cx: float | None = None
    cy: float | None = None
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)
    rotation_deg: float | None = Field(default=None, ge=-360, le=360)
    points: list[tuple[float, float]] | None = Field(default=None, max_length=512)
    label: str | None = Field(default=None, max_length=255)
    object_type: str | None = None
    matched_asset_id: uuid.UUID | None = None
    clear_match: bool = False

    @field_validator("cx", "cy", "width", "height")
    @classmethod
    def _bounded(cls, value: float | None) -> float | None:
        return _finite(value)

    @field_validator("points")
    @classmethod
    def _points_bounded(cls, value):
        if value is not None:
            for x, y in value:
                _finite(x), _finite(y)
        return value

    @field_validator("object_type")
    @classmethod
    def _type_allowed(cls, value: str | None) -> str | None:
        if value is not None and value not in OBJECT_TYPES:
            raise ValueError(f"object_type must be one of {OBJECT_TYPES}")
        return value


async def _lock_candidate(db: AsyncSession, job_id: uuid.UUID, candidate_id: uuid.UUID, expected_version: int) -> FloorPlanImportCandidate:
    candidate = (
        await db.execute(
            select(FloorPlanImportCandidate)
            .where(FloorPlanImportCandidate.id == candidate_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if candidate is None or candidate.job_id != job_id:
        raise NotFoundError(f"Candidate {candidate_id} not found for job {job_id}.")
    if candidate.status != "pending":
        raise ApiError(status_code=409, title="Conflict", detail=f"Candidate is already {candidate.status}.")
    if candidate.version != expected_version:
        raise ConflictError(
            detail=f"Candidate was modified by another request (expected version {expected_version}, current version {candidate.version})."
        )
    return candidate


async def _require_rack(db: AsyncSession, rack_id: uuid.UUID) -> Rack:
    rack = await db.get(Rack, rack_id)
    if rack is None:
        raise ApiError(status_code=422, title="Validation Error", detail=f"matched_asset_id {rack_id} is not a rack.")
    return rack


@router.patch("/import-jobs/{job_id}/candidates/{candidate_id}", response_model=ImportCandidateOut)
async def correct_import_candidate(
    job_id: uuid.UUID,
    candidate_id: uuid.UUID,
    body: CandidateCorrectionIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> ImportCandidateOut:
    """Stage a correction (move/resize/rotate/relabel/reclassify/match). Authoritative state is untouched: the
    correction lives on the pending candidate and only takes effect through an explicit accept."""
    _, floor_plan = await _job_floor_plan(db, job_id)
    candidate = await _lock_candidate(db, job_id, candidate_id, if_match_version)
    provided = body.model_fields_set
    if body.matched_asset_id is not None:
        await _require_rack(db, body.matched_asset_id)

    previous = dict(candidate.correction or {})
    staged = dict(previous)
    shape = (candidate.raw_geometry or {}).get("shape_type")
    geometric = {"cx", "cy", "width", "height", "rotation_deg", "points"} & provided
    if geometric:
        if shape not in ("rect", "circle", "text", "polygon", "polyline", "line"):
            raise ApiError(status_code=422, title="Validation Error", detail="This shape cannot be edited geometrically.")
        if "points" in provided and shape not in ("polygon", "polyline", "line"):
            raise ApiError(status_code=422, title="Validation Error", detail="points apply to polygon/line shapes only.")
        if ({"width", "height", "rotation_deg"} & provided) and shape != "rect":
            raise ApiError(status_code=422, title="Validation Error", detail="width/height/rotation apply to rectangles only.")
    for key in ("cx", "cy", "width", "height", "rotation_deg"):
        if key in provided and getattr(body, key) is not None:
            staged[key] = getattr(body, key)
    if "points" in provided and body.points is not None:
        if len(body.points) < 2:
            raise ApiError(status_code=422, title="Validation Error", detail="points needs at least two vertices.")
        staged["points"] = [list(p) for p in body.points]
    if "label" in provided:
        staged["label"] = body.label
    if "object_type" in provided and body.object_type is not None:
        staged["object_type"] = body.object_type
    if body.clear_match:
        staged.pop("matched_asset_id", None)
        candidate.matched_asset_id = None
    elif body.matched_asset_id is not None:
        staged["matched_asset_id"] = str(body.matched_asset_id)
        candidate.matched_asset_id = body.matched_asset_id
    if staged == previous:
        return (await _candidate_outs(db, [candidate], floor_plan))[0]

    history = list(candidate.correction_history or [])
    history.append({"correction": previous or None, "at": datetime.now(UTC).isoformat(), "by": str(ctx.user.id)})
    candidate.correction_history = history[-spatial_service.MAX_CORRECTION_HISTORY :]
    candidate.correction = staged
    if staged.get("object_type") == "rack" and candidate.match_status == "not_applicable":
        candidate.match_status = "unmatched"
    candidate.version += 1
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.candidate_correct", entity_type="floor_plan_import_candidate",
        entity_id=candidate.id, request_id=request_id, correlation_id=correlation_id,
        before={"correction": previous or None}, after={"correction": staged},
    )
    await db.commit()
    await db.refresh(candidate)
    return (await _candidate_outs(db, [candidate], floor_plan))[0]


@router.post("/import-jobs/{job_id}/candidates/{candidate_id}/undo", response_model=ImportCandidateOut)
async def undo_candidate_correction(
    job_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> ImportCandidateOut:
    _, floor_plan = await _job_floor_plan(db, job_id)
    candidate = await _lock_candidate(db, job_id, candidate_id, if_match_version)
    history = list(candidate.correction_history or [])
    if not history:
        raise ApiError(status_code=409, title="Nothing To Undo", detail="This candidate has no staged correction to undo.")
    last = history.pop()
    before = candidate.correction
    candidate.correction = last.get("correction")
    candidate.correction_history = history
    restored = (candidate.correction or {}).get("matched_asset_id")
    candidate.matched_asset_id = uuid.UUID(restored) if restored else None
    candidate.version += 1
    await db.flush()
    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.candidate_undo", entity_type="floor_plan_import_candidate",
        entity_id=candidate.id, request_id=request_id, correlation_id=correlation_id,
        before={"correction": before}, after={"correction": candidate.correction},
    )
    await db.commit()
    await db.refresh(candidate)
    return (await _candidate_outs(db, [candidate], floor_plan))[0]


class AcceptCandidateIn(BaseModel):
    object_type: str | None = Field(
        default=None, description="rack | equipment | room_outline | annotation | imported_shape | wall | column | obstacle | aisle"
    )
    label: str | None = Field(default=None, max_length=255)
    matched_asset_id: uuid.UUID | None = None
    link_placement: bool = Field(default=False, description="Link the matched rack's current placement to this drawn shape; no coordinates change.")
    apply_position: bool = Field(default=False, description="Explicitly move/place the matched rack at this shape's position.")
    placement_version: int | None = Field(default=None, description="The matched rack's current placement version; required to move an already-placed rack.")
    boundary_exception_reason: str | None = Field(default=None, max_length=500)

    @field_validator("object_type")
    @classmethod
    def _object_type_allowed(cls, value: str | None) -> str | None:
        # Mirrors SpatialObject's own object_type_allowed CHECK constraint at the API boundary — a clean 422
        # for a bad value rather than a raw IntegrityError mapped to a generic 409.
        if value is not None and value not in OBJECT_TYPES:
            raise ValueError(f"object_type must be one of {OBJECT_TYPES}")
        return value


async def _get_or_create_imported_layer(db: AsyncSession, floor_plan_id: uuid.UUID) -> SpatialLayer:
    """RT-1 correction (PHASE2_INDEPENDENT_RED_TEAM_REPORT.md finding RT-1): the previous
    unlocked SELECT-then-INSERT here let concurrent accept requests for different
    candidates of the same job each observe no existing "Imported" layer and each create
    one, producing duplicate layers and — once duplicated — a permanent
    MultipleResultsFound/500 on every subsequent accept for that floor plan.

    Race-safe by construction: `INSERT ... ON CONFLICT (floor_plan_id) WHERE
    layer_type = 'imported' DO NOTHING` — targeting migration 0005's partial unique
    index by its column list + predicate (index inference). `index_where` is
    deliberately `text("layer_type = 'imported'")` — a genuine SQL literal — and NOT a
    Python comparison compiled to a bound parameter: PostgreSQL re-plans a prepared
    statement generically after ~5 executions on the same connection, and a *generic*
    plan cannot verify a parameter's runtime value against a partial index's predicate
    (found live under real-Uvicorn browser validation, not by any pytest run)."""
    table = cast(Table, SpatialLayer.__table__)
    insert_stmt = (
        pg_insert(table)
        .values(id=uuid.uuid4(), floor_plan_id=floor_plan_id, name="Imported", layer_type="imported", z_order=0)
        .on_conflict_do_nothing(
            index_elements=[table.c.floor_plan_id], index_where=text("layer_type = 'imported'")
        )
        .returning(table.c.id)
    )
    layer_id = (await db.execute(insert_stmt)).scalar_one_or_none()
    if layer_id is None:
        layer_id = (
            await db.execute(
                select(SpatialLayer.id).where(
                    SpatialLayer.floor_plan_id == floor_plan_id, SpatialLayer.layer_type == "imported"
                )
            )
        ).scalar_one()
    layer = await db.get(SpatialLayer, layer_id)
    assert layer is not None
    return layer


async def _get_or_create_layer(db: AsyncSession, floor_plan_id: uuid.UUID, layer_type: str, name: str) -> SpatialLayer:
    """For non-imported layer types. The caller holds the floor plan row lock, which serializes creation."""
    layer = (
        await db.execute(
            select(SpatialLayer).where(SpatialLayer.floor_plan_id == floor_plan_id, SpatialLayer.layer_type == layer_type).limit(1)
        )
    ).scalar_one_or_none()
    if layer is None:
        layer = SpatialLayer(floor_plan_id=floor_plan_id, name=name, layer_type=layer_type, z_order=0)
        db.add(layer)
        await db.flush()
    return layer


@router.post("/import-jobs/{job_id}/candidates/{candidate_id}/accept", response_model=ImportCandidateOut)
async def accept_import_candidate(
    job_id: uuid.UUID,
    candidate_id: uuid.UUID,
    body: AcceptCandidateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> ImportCandidateOut:
    """The only path by which imported geometry becomes an authoritative SpatialObject — always an explicit
    human decision, never automatic, regardless of the importer's own confidence.

    Lock order, always: floor plan row -> candidate row -> matched rack's current placement. The floor plan
    lock serialises acceptance against recalibration, boundary edits and other acceptances of the same plan
    (so duplicate detection and the one-boundary rule are race-free); the candidate lock plus the If-Match
    version make a second concurrent accept of the same candidate lose with a 409 rather than create a second
    object; the placement lock (inside move_rack for apply_position) makes accept-vs-move races lose cleanly.
    Rack position is only ever changed by `apply_position=true`, an explicit operator flag."""
    job = await db.get(FloorPlanImportJob, job_id)
    if job is None:
        raise NotFoundError(f"FloorPlanImportJob {job_id} not found.")
    floor_plan = (
        await db.execute(select(FloorPlan).where(FloorPlan.id == job.floor_plan_id).with_for_update().execution_options(populate_existing=True))
    ).scalar_one()
    candidate = await _lock_candidate(db, job_id, candidate_id, if_match_version)

    calibration = await spatial_service.current_calibration(db, floor_plan)
    if calibration is None:
        raise ApiError(status_code=409, title="Calibration Required", detail="Calibrate the floor plan before accepting any candidate.")
    params = spatial_service.params_from_row(calibration)

    object_type = body.object_type or spatial_service.effective_object_type(candidate)
    if object_type is None:
        raise ApiError(status_code=422, title="Validation Error", detail="Choose an object type for this candidate.")
    label = body.label if body.label is not None else spatial_service.effective_label(candidate)

    canonical = spatial_service.to_canonical(spatial_service.effective_geometry(candidate), params)
    if canonical is None:
        raise ApiError(status_code=422, title="Validation Error", detail="This candidate's geometry cannot be converted to the room.")
    spatial_service.check_canonical_bounds(canonical)
    validate_coordinate("x_mm", canonical["x_mm"])
    validate_coordinate("y_mm", canonical["y_mm"])

    matched_id = body.matched_asset_id or (uuid.UUID(candidate.correction["matched_asset_id"]) if (candidate.correction or {}).get("matched_asset_id") else None)
    if (matched_id or body.link_placement or body.apply_position) and object_type not in ("rack", "equipment"):
        raise ApiError(status_code=422, title="Validation Error", detail="Only rack and equipment candidates can be matched to an authoritative asset.")
    if (body.link_placement or body.apply_position) and matched_id is None:
        raise ApiError(status_code=422, title="Validation Error", detail="link_placement/apply_position need a matched asset.")
    if body.apply_position and object_type == "equipment":
        raise ApiError(
            status_code=422, title="Validation Error",
            detail="Equipment is positioned by its linked shape; place it through the equipment API first, then link_placement.",
        )
    if body.link_placement and body.apply_position:
        raise ApiError(status_code=422, title="Validation Error", detail="Choose either link_placement or apply_position, not both.")

    audit_exceptions: list[dict[str, str]] = []
    if object_type == "room_outline":
        if canonical["geometry_type"] not in ("rect", "polygon"):
            raise ApiError(status_code=422, title="Validation Error", detail="A room boundary must be a rectangle or a closed polygon.")
        if await load_boundary_object(db, floor_plan.id) is not None:
            raise ApiError(status_code=409, title="Boundary Exists", detail="This floor plan already has an approved room boundary; replace it explicitly.")
    elif object_type in ("rack", "equipment") and canonical["geometry_type"] == "rect":
        exception = await assert_within_boundary(
            db, floor_plan_id=floor_plan.id, x=canonical["x_mm"], y=canonical["y_mm"], width=canonical["width_mm"],
            height=canonical["height_mm"], rotation_deg=canonical["rotation_deg"], label=label or "This shape",
            exception_reason=body.boundary_exception_reason,
        )
        if exception:
            audit_exceptions.append(exception)

    if object_type == "rack" and canonical["geometry_type"] == "rect":
        corners = rect_corners(canonical["x_mm"], canonical["y_mm"], canonical["width_mm"], canonical["height_mm"], canonical["rotation_deg"])
        for existing in await spatial_service.load_existing_rack_objects(db, floor_plan.id):
            if rect_iou(corners, rect_corners(existing.x_mm, existing.y_mm, existing.width_mm or 0, existing.height_mm or 0, existing.rotation_deg or 0)) >= 0.6:
                raise ApiError(
                    status_code=409, title="Duplicate Geometry",
                    detail="An accepted rack shape already occupies this position; reject this candidate instead of creating a duplicate.",
                )

    rack: Rack | None = None
    placement: RackPlacement | None = None
    equipment_placement: EquipmentPlacement | None = None
    if matched_id is not None and object_type == "equipment":
        if await db.get(Equipment, matched_id) is None:
            raise ApiError(status_code=422, title="Validation Error", detail=f"matched_asset_id {matched_id} is not equipment.")
        equipment_placement = await get_current_equipment_placement(db, matched_id)
        if equipment_placement is not None and equipment_placement.room_id != floor_plan.room_id:
            raise ApiError(status_code=409, title="Equipment In Another Room", detail="The matched equipment is placed in a different room.")
        if body.link_placement:
            if equipment_placement is None or equipment_placement.placement_type == "rack_mounted":
                raise ApiError(status_code=409, title="Equipment Not Floor Placed", detail="Only equipment placed in this room outside a rack can be linked to a drawn shape.")
            if equipment_placement.spatial_object_id is not None:
                raise ApiError(status_code=409, title="Placement Already Linked", detail="The equipment's placement is already linked to a drawn shape.")
    if matched_id is not None:
        if object_type == "rack":
            rack = await _require_rack(db, matched_id)
        already = (
            await db.execute(
                select(func.count()).select_from(SpatialObject).join(SpatialLayer, SpatialLayer.id == SpatialObject.spatial_layer_id)
                .where(SpatialLayer.floor_plan_id == floor_plan.id, SpatialObject.provenance["matched_asset_id"].astext == str(matched_id))
            )
        ).scalar_one()
        if already:
            raise ApiError(status_code=409, title="Rack Already Matched", detail="Another accepted shape on this floor plan already represents that rack.")
        placement = await get_current_rack_placement(db, matched_id) if rack is not None else None
        if placement is not None and placement.room_id != floor_plan.room_id:
            raise ApiError(status_code=409, title="Rack In Another Room", detail="The matched rack is placed in a different room; moving it between rooms is not an import side effect.")
        if body.apply_position and not ctx.has_permission("rack:place"):
            raise ApiError(status_code=403, title="Forbidden", detail="Moving a rack requires the rack:place permission.")
        if body.apply_position and placement is not None and body.placement_version is None:
            raise ApiError(status_code=428, title="Precondition Required", detail="placement_version is required to move an already-placed rack.")
        if body.link_placement and rack is not None:
            if placement is None:
                raise ApiError(status_code=409, title="Rack Not Placed", detail="The matched rack has no placement in this room to link; use apply_position to place it.")
            if placement.spatial_object_id is not None:
                raise ApiError(status_code=409, title="Placement Already Linked", detail="The rack's placement is already linked to a drawn shape.")

    layer_type, layer_name = {
        "rack": ("racks", "Racks"), "equipment": ("equipment", "Equipment"),
        "room_outline": ("room_outline", "Room outline"), "wall": ("room_outline", "Room outline"),
        "column": ("room_outline", "Room outline"), "obstacle": ("room_outline", "Room outline"),
        "aisle": ("annotations", "Annotations"), "annotation": ("annotations", "Annotations"),
    }.get(object_type, ("imported", "Imported"))
    layer = (
        await _get_or_create_imported_layer(db, floor_plan.id) if layer_type == "imported"
        else await _get_or_create_layer(db, floor_plan.id, layer_type, layer_name)
    )

    spatial_object = SpatialObject(
        spatial_layer_id=layer.id, object_type=object_type, geometry_type=canonical["geometry_type"], x_mm=canonical["x_mm"],
        y_mm=canonical["y_mm"], width_mm=canonical["width_mm"], height_mm=canonical["height_mm"],
        rotation_deg=canonical["rotation_deg"], geometry_data=canonical["geometry_data"], label=label, source="imported",
        provenance={
            "origin": "floor_plan_import", "job_id": str(job.id), "candidate_id": str(candidate.id),
            "source_ref": candidate.source_ref, "calibration_id": str(calibration.id),
            "sir_sha256": (await db.execute(select(FloorPlanImportSir.sir_sha256).where(FloorPlanImportSir.job_id == job.id))).scalar_one_or_none(),
            "matched_asset_id": str(matched_id) if matched_id else None, "accepted_by": str(ctx.user.id),
            "accepted_at": datetime.now(UTC).isoformat(), "match_status": candidate.match_status,
            "boundary_exceptions": audit_exceptions or None,
        },
    )
    db.add(spatial_object)
    await db.flush()

    placement_change: str | None = None
    if rack is not None:
        if body.apply_position:
            try:
                new_placement = await move_rack(
                    db, rack_id=rack.id, room_id=floor_plan.room_id, x_mm=canonical["x_mm"], y_mm=canonical["y_mm"],
                    rotation_deg=canonical["rotation_deg"], if_match_version=body.placement_version,
                )
            except PlacementConflict as exc:
                await db.rollback()
                raise ConflictError(detail="The rack's placement changed while you were reviewing; reload and try again.") from exc
            new_placement.spatial_object_id = spatial_object.id
            await db.flush()
            placement_change = "moved" if placement is not None else "placed"
        elif body.link_placement and placement is not None:
            locked = (
                await db.execute(
                    select(RackPlacement).where(RackPlacement.id == placement.id, RackPlacement.effective_to.is_(None)).with_for_update()
                )
            ).scalar_one_or_none()
            if locked is None or locked.spatial_object_id is not None:
                raise ConflictError(detail="The rack's placement changed while you were reviewing; reload and try again.")
            locked.spatial_object_id = spatial_object.id
            locked.version += 1
            await db.flush()
            placement_change = "linked"
    elif equipment_placement is not None and body.link_placement:
        locked_eq = (
            await db.execute(
                select(EquipmentPlacement)
                .where(EquipmentPlacement.id == equipment_placement.id, EquipmentPlacement.effective_to.is_(None))
                .with_for_update()
            )
        ).scalar_one_or_none()
        if locked_eq is None or locked_eq.spatial_object_id is not None:
            raise ConflictError(detail="The equipment's placement changed while you were reviewing; reload and try again.")
        locked_eq.spatial_object_id = spatial_object.id
        locked_eq.version += 1
        await db.flush()
        placement_change = "linked"

    candidate.status = "accepted"
    candidate.resulting_spatial_object_id = spatial_object.id
    candidate.reviewed_by_user_id = ctx.user.id
    candidate.reviewed_at = datetime.now(UTC)
    candidate.version += 1
    if matched_id is not None:
        candidate.matched_asset_id = matched_id
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.candidate_accept", entity_type="floor_plan_import_candidate",
        entity_id=candidate.id, request_id=request_id, correlation_id=correlation_id,
        after={
            "spatial_object_id": str(spatial_object.id), "object_type": object_type, "calibration_id": str(calibration.id),
            "matched_asset_id": str(matched_id) if matched_id else None, "placement_change": placement_change,
            "boundary_exceptions": audit_exceptions or None,
        },
    )
    await write_outbox_event(
        db, event_type="SpatialObjectAccepted", aggregate_type="floor_plan", aggregate_id=floor_plan.id,
        payload={"spatial_object_id": str(spatial_object.id), "object_type": object_type, "placement_change": placement_change},
        correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(candidate)
    return (await _candidate_outs(db, [candidate], floor_plan))[0]


@router.post("/import-jobs/{job_id}/candidates/{candidate_id}/reject", response_model=ImportCandidateOut)
async def reject_import_candidate(
    job_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> ImportCandidateOut:
    # RT-1 correction: same locked-row idiom as accept_import_candidate, for the same reason.
    _, floor_plan = await _job_floor_plan(db, job_id)
    candidate = await _lock_candidate(db, job_id, candidate_id, if_match_version)
    candidate.status = "rejected"
    candidate.reviewed_by_user_id = ctx.user.id
    candidate.reviewed_at = datetime.now(UTC)
    candidate.version += 1
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.candidate_reject", entity_type="floor_plan_import_candidate",
        entity_id=candidate.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(candidate)
    return (await _candidate_outs(db, [candidate], floor_plan))[0]


# --------------------------------------------------------------------------- Room boundary
class RoomBoundaryIn(BaseModel):
    shape: Literal["rect", "polygon"]
    x_mm: int | None = None
    y_mm: int | None = None
    width_mm: int | None = Field(default=None, gt=0, le=1_000_000)
    height_mm: int | None = Field(default=None, gt=0, le=1_000_000)
    rotation_deg: int = Field(default=0, ge=0, lt=360)
    points_mm: list[tuple[int, int]] | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _shape_fields(self) -> "RoomBoundaryIn":
        if self.shape == "rect" and (self.x_mm is None or self.y_mm is None or self.width_mm is None or self.height_mm is None):
            raise ValueError("a rectangular boundary needs x_mm, y_mm, width_mm and height_mm")
        if self.shape == "polygon":
            if not self.points_mm or len(self.points_mm) < 3:
                raise ValueError("a polygonal boundary needs at least three points")
            if any(abs(v) > 1_000_000 for p in self.points_mm for v in p):
                raise ValueError("boundary points must be within +/-1,000,000 mm")
        return self


class RoomBoundaryOut(BaseModel):
    spatial_object_id: uuid.UUID
    floor_plan: FloorPlanOut
    racks_outside_boundary: list[str]


@router.put("/{floor_plan_id}/room-boundary", response_model=RoomBoundaryOut)
async def set_room_boundary(
    floor_plan_id: uuid.UUID,
    body: RoomBoundaryIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> RoomBoundaryOut:
    """Explicit operator definition (or replacement) of the approved room boundary, in canonical mm. This is a
    deliberate manual edit, not an import side effect; it also records the room width/height used for display.
    Racks already placed outside the new boundary are reported, not moved."""
    floor_plan = await lock_versioned_row(db, FloorPlan, floor_plan_id, expected_version=if_match_version, label="FloorPlan")
    if body.shape == "rect":
        assert body.x_mm is not None and body.y_mm is not None and body.width_mm and body.height_mm
        validate_coordinate("x_mm", body.x_mm)
        validate_coordinate("y_mm", body.y_mm)
        x_mm, y_mm, w_mm, h_mm, rot, geometry_type, data = body.x_mm, body.y_mm, body.width_mm, body.height_mm, body.rotation_deg, "rect", None
        polygon: list[Point] = rect_corners(x_mm, y_mm, w_mm, h_mm, rot)
    else:
        assert body.points_mm
        pts = [(float(x), float(y)) for x, y in body.points_mm]
        if abs(polygon_area(pts)) < 1.0:
            raise ApiError(status_code=422, title="Validation Error", detail="The boundary polygon has no area.")
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        x_mm, y_mm, w_mm, h_mm, rot, geometry_type = int(min(xs)), int(min(ys)), int(max(xs) - min(xs)), int(max(ys) - min(ys)), 0, "polygon"
        data = {"points": [[int(x), int(y)] for x, y in pts]}
        polygon = pts

    layer = await _get_or_create_layer(db, floor_plan.id, "room_outline", "Room outline")
    existing = await load_boundary_object(db, floor_plan.id)
    if existing is None:
        existing = SpatialObject(
            spatial_layer_id=layer.id, object_type="room_outline", geometry_type=geometry_type, x_mm=x_mm, y_mm=y_mm, width_mm=w_mm,
            height_mm=h_mm, rotation_deg=rot, geometry_data=data, label="Room boundary", source="authoritative",
            provenance={"origin": "manual_boundary", "set_by": str(ctx.user.id), "set_at": datetime.now(UTC).isoformat()},
        )
        db.add(existing)
    else:
        existing.geometry_type, existing.x_mm, existing.y_mm, existing.width_mm, existing.height_mm = geometry_type, x_mm, y_mm, w_mm, h_mm
        existing.rotation_deg, existing.geometry_data, existing.source = rot, data, "authoritative"
        existing.provenance = {
            **(existing.provenance or {}), "origin": "manual_boundary", "set_by": str(ctx.user.id), "set_at": datetime.now(UTC).isoformat(),
        }
        existing.version += 1
    floor_plan.room_width_mm = w_mm
    floor_plan.room_height_mm = h_mm
    floor_plan.version += 1
    await db.flush()

    outside: list[str] = []
    for rack_ref in await spatial_service.load_rack_refs(db, floor_plan.room_id):
        from app.application.spatial_import.boundary import footprint_inside

        if not footprint_inside(polygon, rack_ref.x_mm, rack_ref.y_mm, rack_ref.width_mm, rack_ref.depth_mm, rack_ref.rotation_deg):
            outside.append(rack_ref.name)

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.room_boundary_set", entity_type="floor_plan", entity_id=floor_plan.id,
        request_id=request_id, correlation_id=correlation_id,
        after={"shape": body.shape, "width_mm": w_mm, "height_mm": h_mm, "racks_outside": outside},
    )
    await db.commit()
    await db.refresh(floor_plan)
    await db.refresh(existing)
    return RoomBoundaryOut(spatial_object_id=existing.id, floor_plan=(await _floor_plan_outs(db, [floor_plan]))[0], racks_outside_boundary=outside)


# --------------------------------------------------------------------------- Objects (2D view)
class SpatialObjectOut(BaseModel):
    id: uuid.UUID
    object_type: str
    geometry_type: str
    x_mm: int
    y_mm: int
    width_mm: int | None
    height_mm: int | None
    rotation_deg: int
    geometry_data: dict | None = None
    label: str | None
    source: str
    provenance: dict | None = None

    model_config = {"from_attributes": True}


@router.get("/{floor_plan_id}/objects", response_model=list[SpatialObjectOut])
async def list_floor_plan_objects(
    floor_plan_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("floor_plan:read"))
) -> list[SpatialObject]:
    if await db.get(FloorPlan, floor_plan_id) is None:
        raise NotFoundError(f"FloorPlan {floor_plan_id} not found.")
    stmt = (
        select(SpatialObject)
        .join(SpatialLayer, SpatialLayer.id == SpatialObject.spatial_layer_id)
        .where(SpatialLayer.floor_plan_id == floor_plan_id)
        .order_by(SpatialObject.created_at, SpatialObject.id)
    )
    return list((await db.execute(stmt)).scalars().all())


