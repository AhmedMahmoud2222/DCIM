"""Floor-plan CRUD, secure upload/import (ARCHITECTURE_REVIEW.md §8/§9/§10/§10a/§11), and
the accept/reject review queue that keeps imported geometry from ever becoming
authoritative inventory without an explicit human decision (§21 of the Phase 2 prompt)."""

import base64
import hashlib
import uuid
from datetime import datetime
from typing import cast

from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Table, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db
from app.api.pagination import Page, Pagination, pagination_params
from app.application.audit_service import write_audit_log
from app.application.concurrency import check_version_match, require_if_match
from app.application.outbox_service import write_outbox_event
from app.application.rbac import require_permission
from app.application.svg_sanitizer import MAX_RASTER_FILE_SIZE_BYTES, MAX_SVG_FILE_SIZE_BYTES
from app.core.errors import ApiError, NotFoundError
from app.domain.floorplan_import.models import FloorPlanImportCandidate, FloorPlanImportDiagnostics, FloorPlanImportJob
from app.domain.location.models import Room
from app.domain.spatial.models import OBJECT_TYPES, FloorPlan, SpatialLayer, SpatialObject
from app.infrastructure.tasks.floorplan_import import run_floor_plan_import_job, validate_and_run_raster_import_job

router = APIRouter(prefix="/floor-plans", tags=["floor-plans"])


def _request_ids(request: Request) -> tuple[str | None, str | None]:
    return getattr(request.state, "request_id", None), getattr(request.state, "correlation_id", None)


# --------------------------------------------------------------------------- FloorPlan
class FloorPlanIn(BaseModel):
    room_id: uuid.UUID
    room_width_mm: int | None = None
    room_height_mm: int | None = None


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

    model_config = {"from_attributes": True}


@router.post("", response_model=FloorPlanOut, status_code=201)
async def create_floor_plan(
    body: FloorPlanIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> FloorPlan:
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
    return floor_plan


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
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


@router.get("/{floor_plan_id}", response_model=FloorPlanOut)
async def get_floor_plan(
    floor_plan_id: uuid.UUID, db: AsyncSession = Depends(get_db), ctx=Depends(require_permission("floor_plan:read"))
) -> FloorPlan:
    floor_plan = await db.get(FloorPlan, floor_plan_id)
    if floor_plan is None:
        raise NotFoundError(f"FloorPlan {floor_plan_id} not found.")
    return floor_plan


@router.post("/{floor_plan_id}/activate", response_model=FloorPlanOut)
async def activate_floor_plan(
    floor_plan_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    if_match_version: int = Depends(require_if_match),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> FloorPlan:
    """§8: "exactly one ACTIVE FloorPlan at a time; prior ones retained, status=superseded".
    The partial unique index (migration 0004) is the hard guarantee; this supersedes
    whatever was previously active for the room in the same transaction."""
    floor_plan = await db.get(FloorPlan, floor_plan_id)
    if floor_plan is None:
        raise NotFoundError(f"FloorPlan {floor_plan_id} not found.")
    check_version_match(expected=if_match_version, actual=floor_plan.version)

    previously_active = (
        await db.execute(
            select(FloorPlan).where(FloorPlan.room_id == floor_plan.room_id, FloorPlan.status == "active")
        )
    ).scalar_one_or_none()
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
    return floor_plan


# --------------------------------------------------------------------------- Upload / Import
class ImportJobOut(BaseModel):
    id: uuid.UUID
    floor_plan_id: uuid.UUID
    status: str
    original_filename: str
    file_size_bytes: int
    rejection_reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/{floor_plan_id}/upload", response_model=ImportJobOut, status_code=202)
async def upload_floor_plan_file(
    floor_plan_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    file: UploadFile = File(...),
    ctx=Depends(require_permission("floor_plan:import")),
) -> FloorPlanImportJob:
    """Untrusted input (§10a) — never trusted by extension; content is content-sniffed
    and size-capped by app.application.svg_sanitizer before any parsing is attempted.
    Async (§36): the response is 202 with a job resource the caller polls, not the
    parse result itself — the actual sanitize/classify work runs in a Celery task with
    no route to this request's own database transaction."""
    floor_plan = await db.get(FloorPlan, floor_plan_id)
    if floor_plan is None:
        raise NotFoundError(f"FloorPlan {floor_plan_id} not found.")

    content = await file.read()
    if len(content) > max(MAX_SVG_FILE_SIZE_BYTES, MAX_RASTER_FILE_SIZE_BYTES):
        raise ApiError(status_code=413, title="Payload Too Large", detail="Uploaded file exceeds the maximum allowed size.")

    # Never trust the client-supplied filename for anything beyond display — no
    # filesystem path this application constructs is ever derived from it. (`file.read()`
    # above may itself have already spooled to a real, immediately-unlinked OS temp file
    # if `content` is over 1MB — that's Starlette's UploadFile, using its own
    # process-random path, never this filename; see svg_sanitizer.py's docstring, RT-2.)
    original_filename = (file.filename or "upload")[:255]
    file_hash = hashlib.sha256(content).hexdigest()

    # Content-sniff to decide which pipeline to run — filename/declared content-type are
    # advisory only (§10a: "reject on mismatch" against actual content, not label).
    head = content[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in content[:4096]:
        source_format = "svg"
    elif content[:8] == b"\x89PNG\r\n\x1a\n":
        source_format = "png"
    elif content[:3] == b"\xff\xd8\xff":
        source_format = "jpeg"
    else:
        raise ApiError(
            status_code=422, title="Unsupported File Type",
            detail="File content is not recognized as SVG, PNG, or JPEG (checked by content, not filename).",
        )

    job = FloorPlanImportJob(
        floor_plan_id=floor_plan_id, uploaded_by_user_id=ctx.user.id, status="queued",
        original_filename=original_filename, file_hash=file_hash, file_size_bytes=len(content),
    )
    db.add(job)
    await db.flush()

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
    total = (
        await db.execute(select(func.count()).select_from(FloorPlanImportJob).where(FloorPlanImportJob.floor_plan_id == floor_plan_id))
    ).scalar_one()
    rows = (
        await db.execute(stmt.order_by(FloorPlanImportJob.created_at.desc()).offset(pagination.offset).limit(pagination.limit))
    ).scalars().all()
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
    objects_discovered: int
    objects_classified: int
    racks_detected: int
    unsupported_object_count: int
    warnings: list
    errors: list
    ambiguous_count: int
    confirmed_count: int
    duration_ms: int | None

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


# --------------------------------------------------------------------------- Candidates
class ImportCandidateOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID
    raw_geometry: dict
    suggested_object_type: str | None
    suggested_label: str | None
    confidence: float | None
    status: str
    resulting_spatial_object_id: uuid.UUID | None

    model_config = {"from_attributes": True}


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
    rows = (await db.execute(stmt.order_by(FloorPlanImportCandidate.created_at).offset(pagination.offset).limit(pagination.limit))).scalars().all()
    return Page(items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset)


class AcceptCandidateIn(BaseModel):
    object_type: str = Field(description="rack | equipment | room_outline | annotation | imported_shape")
    label: str | None = None

    @field_validator("object_type")
    @classmethod
    def _object_type_allowed(cls, value: str) -> str:
        # Mirrors SpatialObject's own object_type_allowed CHECK constraint (migration
        # 0004) at the API boundary — a clean 422 for a bad value here, rather than
        # letting an arbitrary string reach the INSERT and surface as a raw IntegrityError
        # mapped to a generic 409 by the global handler.
        if value not in OBJECT_TYPES:
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
    index by its column list + predicate (index inference; PostgreSQL's `ON CONFLICT ON
    CONSTRAINT` clause only matches a true catalogued constraint, and a *partial* unique
    index, unlike a plain one, can only ever be expressed as an index, never as a
    `UNIQUE` table constraint — the same reason migration 0004's own
    `uq_floor_plan_one_active_per_room` is a raw partial index rather than a named
    constraint) — either wins and creates the one canonical row, or — if a concurrent
    transaction is mid-insert for the same floor_plan_id — blocks on that row's lock
    (real DB-level serialization, not an application lock, so it holds across multiple
    Uvicorn workers/processes) until that transaction commits or rolls back, then
    correctly reports no row inserted. Either way a plain re-SELECT afterward is
    guaranteed to find the canonical row: nothing in this codebase ever deletes a
    SpatialLayer, so a row observed via conflict can't have vanished by the time we
    re-fetch it. This is the same INSERT-as-atomic-claim pattern
    `app/application/idempotency.py` already uses elsewhere in this codebase — not a new
    mechanism.

    `index_where` below is deliberately `text("layer_type = 'imported'")` — a genuine SQL
    literal — and NOT `table.c.layer_type == "imported"` (a Python comparison that
    compiles to a *bound parameter*, e.g. `WHERE layer_type = $7`). That distinction was
    found live, under this correction's own real-Uvicorn browser validation (not caught by
    any pytest run, which never drives one physical connection through this statement
    enough times to trigger it): PostgreSQL's extended query protocol re-plans a prepared
    statement generically after ~5 executions on the same connection, and a *generic* plan
    cannot verify a parameter's runtime value against a partial index's predicate — so the
    bound-parameter form started intermittently throwing "no unique or exclusion
    constraint matching the ON CONFLICT specification" once a pooled connection had reused
    this statement enough times, while a fresh connection (or the first few uses) still
    worked, exactly the pattern observed. A literal predicate has no such value to verify
    at plan time, so it matches on every plan, generic or custom."""
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


@router.post("/import-jobs/{job_id}/candidates/{candidate_id}/accept", response_model=ImportCandidateOut)
async def accept_import_candidate(
    job_id: uuid.UUID,
    candidate_id: uuid.UUID,
    body: AcceptCandidateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> FloorPlanImportCandidate:
    """The only path by which imported geometry becomes an authoritative SpatialObject
    (§21 of the Phase 2 prompt) — always an explicit human decision, never automatic,
    regardless of the importer's own confidence score.

    RT-1 correction: locks the candidate row (`FOR UPDATE`, the same idiom
    app/application/placement_service.py already uses) before checking/changing its
    status. Without this, two concurrent accepts of the *same* candidate could both
    observe status='pending' under READ COMMITTED, both create their own SpatialObject,
    and the second UPDATE would silently overwrite the first's `resulting_spatial_
    object_id` — leaving an orphaned duplicate SpatialObject and an inconsistent audit
    trail, the same class of "unlocked check-then-act" defect RT-1 diagnosed for the
    layer lookup. Locking here makes the second request's transaction block until the
    first commits, then correctly observe status='accepted' and return a clean 409."""
    candidate = (
        await db.execute(
            select(FloorPlanImportCandidate).where(FloorPlanImportCandidate.id == candidate_id).with_for_update()
        )
    ).scalar_one_or_none()
    if candidate is None or candidate.job_id != job_id:
        raise NotFoundError(f"Candidate {candidate_id} not found for job {job_id}.")
    if candidate.status != "pending":
        raise ApiError(status_code=409, title="Conflict", detail=f"Candidate is already {candidate.status}.")

    job = await db.get(FloorPlanImportJob, job_id)
    assert job is not None

    layer = await _get_or_create_imported_layer(db, job.floor_plan_id)

    geometry = candidate.raw_geometry
    spatial_object = SpatialObject(
        spatial_layer_id=layer.id,
        object_type=body.object_type,
        geometry_type=geometry.get("shape_type", "rect"),
        x_mm=int(geometry.get("x", 0)),
        y_mm=int(geometry.get("y", 0)),
        width_mm=int(geometry["width"]) if geometry.get("width") is not None else None,
        height_mm=int(geometry["height"]) if geometry.get("height") is not None else None,
        label=body.label or candidate.suggested_label,
        source="imported",
    )
    db.add(spatial_object)
    await db.flush()

    candidate.status = "accepted"
    candidate.resulting_spatial_object_id = spatial_object.id
    candidate.reviewed_by_user_id = ctx.user.id
    from datetime import UTC, datetime as _dt

    candidate.reviewed_at = _dt.now(UTC)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.candidate_accept", entity_type="floor_plan_import_candidate",
        entity_id=candidate.id, request_id=request_id, correlation_id=correlation_id,
        after={"spatial_object_id": str(spatial_object.id), "object_type": body.object_type},
    )
    await db.commit()
    await db.refresh(candidate)
    return candidate


@router.post("/import-jobs/{job_id}/candidates/{candidate_id}/reject", response_model=ImportCandidateOut)
async def reject_import_candidate(
    job_id: uuid.UUID,
    candidate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    ctx=Depends(require_permission("floor_plan:manage")),
) -> FloorPlanImportCandidate:
    # RT-1 correction: same locked-row idiom as accept_import_candidate, for the same
    # reason — see that function's docstring.
    candidate = (
        await db.execute(
            select(FloorPlanImportCandidate).where(FloorPlanImportCandidate.id == candidate_id).with_for_update()
        )
    ).scalar_one_or_none()
    if candidate is None or candidate.job_id != job_id:
        raise NotFoundError(f"Candidate {candidate_id} not found for job {job_id}.")
    if candidate.status != "pending":
        raise ApiError(status_code=409, title="Conflict", detail=f"Candidate is already {candidate.status}.")

    from datetime import UTC, datetime as _dt

    candidate.status = "rejected"
    candidate.reviewed_by_user_id = ctx.user.id
    candidate.reviewed_at = _dt.now(UTC)
    await db.flush()

    request_id, correlation_id = _request_ids(request)
    await write_audit_log(
        db, actor_user_id=ctx.user.id, action="floor_plan.candidate_reject", entity_type="floor_plan_import_candidate",
        entity_id=candidate.id, request_id=request_id, correlation_id=correlation_id,
    )
    await db.commit()
    await db.refresh(candidate)
    return candidate


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
    label: str | None
    source: str

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
    )
    return list((await db.execute(stmt)).scalars().all())
