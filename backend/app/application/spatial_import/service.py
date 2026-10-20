# ruff: noqa: E501
"""Async DB operations behind the floor-plan import API: calibration persistence, effective candidate
geometry, canonical conversion and the rack reconciliation loader. Authoritative writes live in the API
handlers (accept) so their locking order is visible in one place: floor plan -> candidate -> placement."""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.spatial_import.calibration import CalibrationParams
from app.application.spatial_import.reconcile import (
    CandidateFootprint,
    ExistingObject,
    MatchResult,
    RackRef,
    reconcile_candidate,
    resolve_shared_matches,
)
from app.core.errors import ApiError
from app.domain.catalog.models import RackModelRevision
from app.domain.floorplan_import.models import FloorPlanImportCandidate, FloorPlanImportJob
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Rack
from app.domain.placement.models import RackPlacement
from app.domain.spatial.models import FloorPlan, FloorPlanCalibration, SpatialLayer, SpatialObject

MAX_COORDINATE_MM = 1_000_000
MAX_CORRECTION_HISTORY = 20


def params_from_row(row: FloorPlanCalibration) -> CalibrationParams:
    return CalibrationParams(
        method=row.method, source_units=row.source_units, mm_per_unit=float(row.mm_per_unit), origin_x=float(row.origin_x),
        origin_y=float(row.origin_y), y_axis=row.y_axis, rotation_quadrants=int(row.rotation_quadrants),
        error_bound_mm=None if row.error_bound_mm is None else float(row.error_bound_mm),
        relative_error=None if row.relative_error is None else float(row.relative_error), confidence=row.confidence,
        reference=dict(row.reference or {}), warnings=tuple(row.warnings or ()),
    )


async def current_calibration(db: AsyncSession, floor_plan: FloorPlan) -> FloorPlanCalibration | None:
    if floor_plan.current_calibration_id is None:
        return None
    return await db.get(FloorPlanCalibration, floor_plan.current_calibration_id)


def effective_geometry(candidate: FloorPlanImportCandidate) -> dict[str, Any]:
    """The parser's geometry overlaid with the operator's staged correction (both in source coordinates)."""
    merged = dict(candidate.raw_geometry or {})
    correction = candidate.correction or {}
    for key in ("cx", "cy", "width", "height", "rotation_deg", "points", "radius"):
        if key in correction and correction[key] is not None:
            merged[key] = correction[key]
    if "cx" in correction or "width" in correction:  # keep the convenience top-left in step with the centre
        if merged.get("shape_type") == "rect":
            merged["x"] = merged["cx"] - merged["width"] / 2
            merged["y"] = merged["cy"] - merged["height"] / 2
    if "label" in correction:
        merged["label"] = correction["label"]
    return merged


def effective_object_type(candidate: FloorPlanImportCandidate) -> str | None:
    correction = candidate.correction or {}
    return correction.get("object_type") or candidate.suggested_object_type


def effective_label(candidate: FloorPlanImportCandidate) -> str | None:
    correction = candidate.correction or {}
    if "label" in correction:
        return correction["label"]
    return candidate.suggested_label


def to_canonical(geometry: dict[str, Any], params: CalibrationParams) -> dict[str, Any] | None:
    """Canonical-mm SpatialObject columns for an effective candidate geometry, or None when it cannot be
    represented. Pure; used for previews and for the accept write alike so both always agree."""
    shape = geometry.get("shape_type")
    try:
        if shape == "rect":
            r = params.rect_to_canonical(
                geometry["cx"], geometry["cy"], geometry["width"], geometry["height"], geometry.get("rotation_deg", 0.0)
            )
            return {
                "geometry_type": "rect", "x_mm": round(r["x_mm"]), "y_mm": round(r["y_mm"]), "width_mm": round(r["width_mm"]),
                "height_mm": round(r["height_mm"]), "rotation_deg": int(round(r["rotation_deg"])) % 360, "geometry_data": None,
            }
        if shape == "circle":
            cx, cy = params.to_canonical(geometry["cx"], geometry["cy"])
            radius = params.length_to_mm(geometry.get("radius") or 0.0)
            return {
                "geometry_type": "circle", "x_mm": round(cx - radius), "y_mm": round(cy - radius), "width_mm": round(2 * radius),
                "height_mm": round(2 * radius), "rotation_deg": 0, "geometry_data": None,
            }
        if shape == "text":
            x, y = params.to_canonical(geometry["cx"], geometry["cy"])
            return {"geometry_type": "text", "x_mm": round(x), "y_mm": round(y), "width_mm": None, "height_mm": None, "rotation_deg": 0, "geometry_data": None}
        if shape in ("polygon", "polyline", "line"):
            pts = [params.to_canonical(p[0], p[1]) for p in geometry["points"]]
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            return {
                "geometry_type": "polygon" if shape == "polygon" else "path", "x_mm": round(min(xs)), "y_mm": round(min(ys)),
                "width_mm": round(max(xs) - min(xs)), "height_mm": round(max(ys) - min(ys)), "rotation_deg": 0,
                "geometry_data": {"points": [[round(x), round(y)] for x, y in pts]},
            }
    except (KeyError, TypeError, ValueError):
        return None
    return None


def check_canonical_bounds(canonical: dict[str, Any]) -> None:
    for key in ("x_mm", "y_mm", "width_mm", "height_mm"):
        value = canonical.get(key)
        if value is not None and abs(value) > MAX_COORDINATE_MM:
            raise ApiError(
                status_code=422, title="Validation Error",
                detail=f"{key} is {value} mm, outside the permitted +/-{MAX_COORDINATE_MM} mm; check the calibration.",
            )
    if canonical["geometry_type"] in ("rect", "circle", "polygon") and not ((canonical.get("width_mm") or 0) > 0 and (canonical.get("height_mm") or 0) > 0):
        raise ApiError(status_code=422, title="Validation Error", detail="The shape is smaller than 1 mm after calibration.")


async def load_rack_refs(db: AsyncSession, room_id: uuid.UUID) -> list[RackRef]:
    rows = (
        await db.execute(
            select(RackPlacement, Rack, ManagedAsset, RackModelRevision)
            .join(Rack, Rack.id == RackPlacement.rack_id)
            .join(ManagedAsset, ManagedAsset.id == RackPlacement.rack_id)
            .join(RackModelRevision, RackModelRevision.id == Rack.model_revision_id)
            .where(RackPlacement.room_id == room_id, RackPlacement.effective_to.is_(None))
        )
    ).all()
    return [
        RackRef(
            rack_id=str(rack.id), name=rack.name, asset_tag=asset.asset_tag, x_mm=float(placement.x_mm), y_mm=float(placement.y_mm),
            width_mm=float(revision.width_mm), depth_mm=float(revision.depth_mm), rotation_deg=float(placement.rotation_deg or 0),
        )
        for placement, rack, asset, revision in rows
        if placement.x_mm is not None and placement.y_mm is not None
    ]


async def load_existing_rack_objects(db: AsyncSession, floor_plan_id: uuid.UUID) -> list[SpatialObject]:
    return list(
        (
            await db.execute(
                select(SpatialObject)
                .join(SpatialLayer, SpatialLayer.id == SpatialObject.spatial_layer_id)
                .where(
                    SpatialLayer.floor_plan_id == floor_plan_id, SpatialObject.object_type == "rack",
                    SpatialObject.width_mm.is_not(None), SpatialObject.height_mm.is_not(None),
                )
            )
        ).scalars()
    )


def _is_match_item(item: dict[str, Any]) -> bool:
    return item.get("phase") == "match"


async def reconcile_job(
    db: AsyncSession, *, floor_plan: FloorPlan, job: FloorPlanImportJob, calibration: FloorPlanCalibration
) -> dict[str, int]:
    """Re-derives match evidence for the job's pending rack candidates against the room's authoritative racks
    and the floor plan's already-accepted geometry. Advisory only: writes evidence/status columns on pending
    candidates (version bumped so a stale editor must reload) and nothing authoritative."""
    params = params_from_row(calibration)
    candidates = list(
        (
            await db.execute(
                select(FloorPlanImportCandidate)
                .where(FloorPlanImportCandidate.job_id == job.id, FloorPlanImportCandidate.status == "pending")
                .with_for_update()
            )
        ).scalars()
    )
    racks = await load_rack_refs(db, floor_plan.room_id)
    existing_objects = await load_existing_rack_objects(db, floor_plan.id)
    existing = [
        ExistingObject(
            object_id=str(o.id), x_mm=float(o.x_mm), y_mm=float(o.y_mm), width_mm=float(o.width_mm or 0), height_mm=float(o.height_mm or 0),
            rotation_deg=float(o.rotation_deg or 0), label=o.label,
        )
        for o in existing_objects
    ]
    seen_refs: dict[str, str] = {}
    accepted_rows = (
        await db.execute(
            select(FloorPlanImportCandidate.source_ref, FloorPlanImportCandidate.resulting_spatial_object_id)
            .join(FloorPlanImportJob, FloorPlanImportJob.id == FloorPlanImportCandidate.job_id)
            .where(
                FloorPlanImportJob.floor_plan_id == floor_plan.id, FloorPlanImportCandidate.status == "accepted",
                FloorPlanImportCandidate.source_ref.is_not(None), FloorPlanImportCandidate.resulting_spatial_object_id.is_not(None),
            )
        )
    ).all()
    for source_ref, object_id in accepted_rows:
        if source_ref is not None:
            seen_refs[source_ref] = str(object_id)

    results: dict[str, MatchResult] = {}
    touched: dict[str, FloorPlanImportCandidate] = {}
    for candidate in candidates:
        if effective_object_type(candidate) != "rack":
            continue
        canonical = to_canonical(effective_geometry(candidate), params)
        if canonical is None or canonical["geometry_type"] != "rect":
            continue
        footprint = CandidateFootprint(
            candidate_id=str(candidate.id), x_mm=canonical["x_mm"], y_mm=canonical["y_mm"], width_mm=canonical["width_mm"],
            height_mm=canonical["height_mm"], rotation_deg=canonical["rotation_deg"], label=effective_label(candidate),
            source_ref=candidate.source_ref or "",
        )
        results[str(candidate.id)] = reconcile_candidate(footprint, racks, existing, seen_refs)
        touched[str(candidate.id)] = candidate
    resolve_shared_matches(results)

    counts = {"matched": 0, "ambiguous": 0, "conflict": 0, "duplicate": 0, "unmatched": 0}
    for cid, result in results.items():
        candidate = touched[cid]
        detection = [i for i in (candidate.evidence or []) if not _is_match_item(i)]
        candidate.evidence = detection + [{**item, "phase": "match"} for item in result.evidence]
        candidate.match_status = result.status
        candidate.match_score = round(result.score, 3) if result.score else None
        chosen = (candidate.correction or {}).get("matched_asset_id")
        candidate.matched_asset_id = uuid.UUID(chosen) if chosen else (uuid.UUID(result.matched_rack_id) if result.matched_rack_id else None)
        candidate.duplicate_of_spatial_object_id = uuid.UUID(result.duplicate_of_object_id) if result.duplicate_of_object_id else None
        candidate.reconciled_calibration_id = calibration.id
        candidate.version += 1
        counts[result.status] += 1
    await db.flush()
    return counts
