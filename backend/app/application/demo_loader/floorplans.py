# ruff: noqa: E501
"""Calibrates the active demo floor plans with the supported declared-units DXF flow.

For each demo room whose active plan has no calibration the loader:
  1. validates the geometry of the racks already placed there (inside the room, no overlap) and stops for that
     plan if it is not valid; it never moves a rack;
  2. writes an ASCII DXF that declares millimetres ($INSUNITS = 4), holding the room outline and the existing rack
     footprints, and imports it through the same parser, intermediate representation and candidate classifier as an
     operator upload (job `queued` -> `parsed`);
  3. records an immutable `declared_units` calibration, with the origin at the DXF's top-left corner, so canonical
     room coordinates equal the drawing's millimetres and every existing rack position keeps its meaning;
  4. runs the advisory rack match and reports how many rack footprints matched the existing racks.

A plan that already has a calibration is left alone, so a second run does nothing."""

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit_service import write_audit_log
from app.application.demo_loader import LOADER_VERSION
from app.application.demo_loader.selection import LoaderError, RackInfo, Targets
from app.application.outbox_service import write_outbox_event
from app.application.spatial_import import calibration as cal
from app.application.spatial_import import service as spatial_service
from app.application.spatial_import.formats import detect_format
from app.application.spatial_import.persist import new_diagnostics, store_parse_result
from app.application.spatial_import.sir import SirDocument
from app.domain.floorplan_import.models import FloorPlanImportJob
from app.domain.location.models import Room
from app.domain.spatial.models import FloorPlan, FloorPlanCalibration

LAYER_ROOM = "ROOM-OUTLINE"
LAYER_RACKS = "RACKS"


def rack_extent(rack: RackInfo) -> tuple[float, float]:
    """Footprint (width, depth) in room millimetres. A quarter-turn swaps them; the same rule the heat map uses."""
    width, depth = rack.width_mm, rack.depth_mm
    return (depth, width) if (rack.rotation_deg or 0) % 180 == 90 else (width, depth)


def validate_geometry(width_mm: int, height_mm: int, racks: list[RackInfo]) -> list[str]:
    """Problems that make calibrating this plan meaningless. An empty list means the racks are positioned correctly."""
    problems: list[str] = []
    boxes: list[tuple[RackInfo, float, float, float, float]] = []
    for rack in racks:
        if rack.x_mm is None or rack.y_mm is None:
            problems.append(f"{rack.asset_tag}: no position")
            continue
        if (rack.rotation_deg or 0) % 90 != 0:
            problems.append(f"{rack.asset_tag}: rotation {rack.rotation_deg} is not a quarter turn")
            continue
        w, d = rack_extent(rack)
        x0, y0, x1, y1 = rack.x_mm, rack.y_mm, rack.x_mm + w, rack.y_mm + d
        if x0 < 0 or y0 < 0 or x1 > width_mm or y1 > height_mm:
            problems.append(f"{rack.asset_tag}: footprint ({x0},{y0})-({x1},{y1}) leaves the {width_mm}x{height_mm} mm room")
        boxes.append((rack, x0, y0, x1, y1))
    boxes.sort(key=lambda b: b[1])
    for i, (a, ax0, ay0, ax1, ay1) in enumerate(boxes):
        for b, bx0, by0, bx1, by1 in boxes[i + 1:]:
            if bx0 >= ax1:
                break
            if ay0 < by1 and by0 < ay1 and ax0 < bx1 and bx0 < ax1:
                problems.append(f"{a.asset_tag} overlaps {b.asset_tag}")
    return problems


def _pairs(*items: tuple[int, object]) -> str:
    return "".join(f"{code}\n{value}\n" for code, value in items)


def _rect(layer: str, x0: float, y0: float, x1: float, y1: float, height_mm: int) -> str:
    """A closed LWPOLYLINE. DXF's Y axis points up, so canonical y (down) becomes height - y."""
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    body = _pairs((0, "LWPOLYLINE"), (8, layer), (90, 4), (70, 1))
    for x, y in corners:
        body += _pairs((10, f"{x:.3f}"), (20, f"{height_mm - y:.3f}"))
    return body


def build_dxf(width_mm: int, height_mm: int, racks: list[RackInfo]) -> bytes:
    """ASCII DXF that declares millimetres, holds the room outline and the existing rack footprints."""
    out = _pairs((0, "SECTION"), (2, "HEADER"), (9, "$INSUNITS"), (70, 4), (0, "ENDSEC"))
    out += _pairs((0, "SECTION"), (2, "TABLES"), (0, "TABLE"), (2, "LAYER"), (70, 2))
    for layer in (LAYER_ROOM, LAYER_RACKS):
        out += _pairs((0, "LAYER"), (2, layer), (70, 0), (62, 7), (6, "CONTINUOUS"))
    out += _pairs((0, "ENDTAB"), (0, "ENDSEC"), (0, "SECTION"), (2, "ENTITIES"))
    out += _rect(LAYER_ROOM, 0, 0, width_mm, height_mm, height_mm)
    for rack in sorted(racks, key=lambda r: r.asset_tag):
        if rack.x_mm is None or rack.y_mm is None:
            continue
        w, d = rack_extent(rack)
        out += _rect(LAYER_RACKS, rack.x_mm, rack.y_mm, rack.x_mm + w, rack.y_mm + d, height_mm)
    out += _pairs((0, "ENDSEC"), (0, "EOF"))
    return out.encode("ascii")


@dataclass
class PlanResult:
    site: str
    room: str
    plan_id: uuid.UUID | None = None
    width_mm: int | None = None
    height_mm: int | None = None
    racks: int = 0
    state: str = ""  # calibrated | already_calibrated | skipped
    calibration_id: uuid.UUID | None = None
    method: str | None = None
    scale_mm_per_unit: float | None = None
    confidence: str | None = None
    match: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


async def _import_job(db: AsyncSession, plan: FloorPlan, content: bytes, actor_id: uuid.UUID, *, sandbox: bool) -> FloorPlanImportJob:
    digest = hashlib.sha256(content).hexdigest()
    existing = (
        await db.execute(
            select(FloorPlanImportJob).where(FloorPlanImportJob.floor_plan_id == plan.id, FloorPlanImportJob.dedup_key == digest)
        )
    ).scalar_one_or_none()
    if existing is not None and existing.status == "parsed":
        return existing
    if existing is not None:
        raise LoaderError(f"Import job {existing.id} for plan {plan.id} is {existing.status!r}; resolve it before re-running.")
    if detect_format(content) != "dxf":
        raise LoaderError("The generated drawing was not recognised as DXF.")
    job = FloorPlanImportJob(
        floor_plan_id=plan.id, uploaded_by_user_id=actor_id, status="queued", original_filename="demo-room-declared-mm.dxf",
        file_hash=digest, file_size_bytes=len(content), detected_format="dxf", declared_format="application/dxf", dedup_key=digest,
    )
    db.add(job)
    await db.flush()
    document: SirDocument
    if sandbox:
        from app.application.spatial_import.runner import ParseFailure, parse_in_sandbox
        from app.core.config import get_settings

        try:
            outcome = parse_in_sandbox(content, "dxf", require_landlock=get_settings().catalog_extraction_require_landlock)
        except ParseFailure as exc:
            raise LoaderError(f"The sandboxed DXF parser rejected the drawing: {exc.reason}") from exc
        document, raw = outcome.document, outcome.raw
    else:
        from app.application.spatial_import.dxf_parser import parse_dxf

        document = parse_dxf(content)
        raw = document.to_dict()
    job.status = "parsing"
    diagnostics = new_diagnostics(job, source_format="dxf", parser_name=document.parser_name, parser_version=document.parser_version)
    db.add(diagnostics)
    await db.flush()
    await db.run_sync(lambda sync_session: store_parse_result(sync_session, job, diagnostics, document, raw))
    await db.flush()
    return job


async def _calibrate(db: AsyncSession, plan: FloorPlan, job: FloorPlanImportJob, actor_id: uuid.UUID) -> FloorPlanCalibration:
    from app.application.spatial_import.sir import validate_sir
    from app.domain.floorplan_import.models import FloorPlanImportSir

    stored = (await db.execute(select(FloorPlanImportSir).where(FloorPlanImportSir.job_id == job.id))).scalar_one()
    document = validate_sir(stored.sir)
    try:
        params = cal.calibrate_declared_units(document)
    except cal.CalibrationError as exc:
        raise LoaderError(f"Declared-units calibration refused: {exc.detail}") from exc
    if not document.units_trusted or params.method != "declared_units":
        raise LoaderError("The drawing's units are not authoritative; refusing to calibrate it as declared units.")
    row = FloorPlanCalibration(
        floor_plan_id=plan.id, sequence=1, job_id=job.id, supersedes_id=None, method=params.method, source_units=params.source_units,
        mm_per_unit=params.mm_per_unit, origin_x=params.origin_x, origin_y=params.origin_y, y_axis=params.y_axis,
        rotation_quadrants=params.rotation_quadrants, error_bound_mm=params.error_bound_mm, relative_error=params.relative_error,
        confidence=params.confidence, reference={**params.reference, "applied_by": "demo_loader"}, warnings=list(params.warnings),
        created_by_user_id=actor_id, created_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    plan.current_calibration_id = row.id
    plan.calibration_scale_mm_per_px = None
    plan.version += 1
    await db.flush()
    return row


async def calibrate_demo_plans(db: AsyncSession, targets: Targets, actor_id: uuid.UUID, *, sandbox: bool = True) -> list[PlanResult]:
    """Process every demo room that holds racks. Returns one result per room."""
    results: list[PlanResult] = []
    site_by_id = {s.id: s for s in targets.sites}
    rooms: dict[uuid.UUID, list[RackInfo]] = {}
    for rack in targets.racks:
        rooms.setdefault(rack.room_id, []).append(rack)
    for room_id, racks in sorted(rooms.items(), key=lambda kv: str(kv[0])):
        room = await db.get(Room, room_id)
        assert room is not None
        result = PlanResult(site=site_by_id[racks[0].site_id].code, room=room.name, racks=len(racks))
        results.append(result)
        plan = (await db.execute(select(FloorPlan).where(FloorPlan.room_id == room_id, FloorPlan.status == "active"))).scalar_one_or_none()
        if plan is None:
            result.state = "skipped"
            result.problems.append("no active floor plan")
            continue
        result.plan_id, result.width_mm, result.height_mm = plan.id, plan.room_width_mm, plan.room_height_mm
        if plan.current_calibration_id is not None:
            calibration = await db.get(FloorPlanCalibration, plan.current_calibration_id)
            result.state = "already_calibrated"
            result.calibration_id = plan.current_calibration_id
            if calibration is not None:
                result.method, result.scale_mm_per_unit, result.confidence = calibration.method, float(calibration.mm_per_unit), calibration.confidence
            continue
        if not plan.room_width_mm or not plan.room_height_mm:
            result.state = "skipped"
            result.problems.append("plan has no room_width_mm / room_height_mm")
            continue
        problems = validate_geometry(plan.room_width_mm, plan.room_height_mm, racks)
        if problems:
            result.state = "skipped"
            result.problems = problems
            continue
        content = build_dxf(plan.room_width_mm, plan.room_height_mm, racks)
        job = await _import_job(db, plan, content, actor_id, sandbox=sandbox)
        calibration = await _calibrate(db, plan, job, actor_id)
        result.match = await spatial_service.reconcile_job(db, floor_plan=plan, job=job, calibration=calibration)
        await write_audit_log(
            db, actor_user_id=actor_id, action="floor_plan.calibrate", entity_type="floor_plan", entity_id=plan.id, request_id=None,
            correlation_id=None, source="system", reason=f"demo_loader {LOADER_VERSION}",
            after={"calibration_id": str(calibration.id), "method": calibration.method, "mm_per_unit": float(calibration.mm_per_unit),
                   "confidence": calibration.confidence, "job_id": str(job.id)},
        )
        await write_outbox_event(
            db, event_type="FloorPlanCalibrated", aggregate_type="floor_plan", aggregate_id=plan.id,
            payload={"calibration_id": str(calibration.id), "method": calibration.method},
        )
        await db.commit()
        result.state = "calibrated"
        result.calibration_id = calibration.id
        result.method, result.scale_mm_per_unit, result.confidence = calibration.method, float(calibration.mm_per_unit), calibration.confidence
    return results


__all__ = ["calibrate_demo_plans", "validate_geometry", "build_dxf", "PlanResult"]
