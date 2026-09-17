"""Floor-plan import job execution (§10/§36: "floor-plan import... is asynchronous —
202 Accepted + a job resource URL to poll"). The uploaded bytes are passed as the task
argument (base64-encoded, JSON-serializable) rather than written to a shared filesystem
path of this pipeline's own choosing — a reasonable choice at the 5MB SVG size cap this
pipeline enforces; a much larger file-size ceiling would call for a different transport
(e.g. object storage with a short-lived signed reference), not attempted here since it
isn't needed at this scale. (This is a separate claim from app.application.svg_sanitizer's
own docstring, corrected per RT-2: the upload *endpoint* itself, upstream of this task,
does spool to a real OS temp file above 1MB via Starlette's UploadFile — see that
docstring for what does and doesn't still hold there.)"""

import base64
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.application.svg_sanitizer import SvgRejected, sanitize_svg, validate_raster_image
from app.core.logging import get_logger
from app.db.sync_session import get_sync_db
from app.domain.floorplan_import.models import FloorPlanImportCandidate, FloorPlanImportDiagnostics, FloorPlanImportJob
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)

# A shape between roughly 0.3 and 0.9 width:height is a coarse geometric proxy for "looks
# like a rack footprint viewed from above" — a heuristic, not real detection; every
# candidate still requires an explicit human accept (§10) regardless of this guess.
_RACK_LIKE_ASPECT_RATIO = (0.3, 0.9)


def _mark_job_failed_on_unexpected_error(db: Session, job_id: str, source_format: str, parser_name: str) -> None:
    """PHASE2_NESTED_SVG_CORRECTION_REPORT.md: `except SvgRejected` below only ever
    catches the *expected*, deliberate rejections `sanitize_svg`/`validate_raster_image`
    raise for input they've recognized as bad. Before this correction, any other
    exception — the concrete case found was an uncaught `RecursionError` from a
    pathologically deep (but small) SVG, though the same gap could just as easily have
    let an unrelated bug do the same thing — propagated straight out of the
    `with get_sync_db() as db:` block. That block's `__exit__` only closes the session
    (an implicit rollback of whatever was uncommitted), so the job was left at whatever
    status was last *durably committed* — "parsing", set unconditionally near the top of
    each task — forever, with no path to a terminal state and nothing for the frontend to
    ever show but an indefinite spinner.

    Called from a bare `except Exception:` in each task, after `db.rollback()` has
    already discarded any partially-built candidates/diagnostics for the failed attempt
    (§"DATABASE CONSISTENCY": a failed import must never leave partial candidate state
    behind) — so `job` here is re-fetched fresh rather than reusing the (now possibly
    session-detached) instance from before the rollback."""
    job = db.get(FloorPlanImportJob, job_id)
    if job is None:
        return
    finished_at = datetime.now(UTC)
    job.status = "failed"
    # Deliberately generic and safe — never the raw exception's own message, which
    # could echo internal details; the real exception is only ever in the structured
    # log (logger.exception, called by the caller), never in anything API-visible.
    job.rejection_reason = "The uploaded file could not be processed due to an internal error."
    job.finished_at = finished_at
    db.add(
        FloorPlanImportDiagnostics(
            job_id=job.id, source_format=source_format, parser_name=parser_name, parser_version="1",
            started_at=finished_at, finished_at=finished_at, objects_discovered=0, objects_classified=0,
            racks_detected=0, equipment_detected=0, unsupported_object_count=0, warnings=[],
            errors=["internal error during parsing"], ambiguous_count=0, rejected_count=0, confirmed_count=0,
        )
    )
    db.commit()


@celery_app.task(name="app.infrastructure.tasks.floorplan_import.run_floor_plan_import_job")
def run_floor_plan_import_job(job_id: str, content_b64: str) -> None:
    with get_sync_db() as db:
        job = db.get(FloorPlanImportJob, job_id)
        if job is None:
            logger.error("floor_plan_import_job_not_found", job_id=job_id)
            return

        job.status = "parsing"
        db.commit()

        try:
            diagnostics = FloorPlanImportDiagnostics(
                job_id=job.id,
                source_format="svg",
                parser_name="svg_sanitizer",
                parser_version="1",
                started_at=datetime.now(UTC),
                objects_discovered=0,
                objects_classified=0,
                racks_detected=0,
                equipment_detected=0,
                unsupported_object_count=0,
                warnings=[],
                errors=[],
                ambiguous_count=0,
                rejected_count=0,
                confirmed_count=0,
            )
            db.add(diagnostics)

            content = base64.b64decode(content_b64)
            try:
                result = sanitize_svg(content)
            except SvgRejected as exc:
                job.status = "failed"
                job.rejection_reason = exc.reason
                job.finished_at = datetime.now(UTC)
                diagnostics.errors = [exc.reason]
                diagnostics.finished_at = job.finished_at
                db.commit()
                logger.warning("floor_plan_import_rejected", job_id=job_id, reason=exc.reason)
                return

            diagnostics.objects_discovered = result.objects_discovered
            diagnostics.unsupported_object_count = result.unsupported_object_count
            diagnostics.warnings = result.warnings

            racks_detected = 0
            for shape in result.shapes:
                suggested_type = None
                confidence = None
                if shape.shape_type == "rect" and shape.width and shape.height and shape.width > 0 and shape.height > 0:
                    ratio = shape.width / shape.height
                    if _RACK_LIKE_ASPECT_RATIO[0] <= ratio <= _RACK_LIKE_ASPECT_RATIO[1]:
                        suggested_type = "rack"
                        confidence = 0.5
                        racks_detected += 1

                candidate = FloorPlanImportCandidate(
                    job_id=job.id,
                    raw_geometry={
                        "shape_type": shape.shape_type,
                        "x": shape.x,
                        "y": shape.y,
                        "width": shape.width,
                        "height": shape.height,
                        "radius": shape.radius,
                        "text": shape.text,
                    },
                    suggested_object_type=suggested_type,
                    suggested_label=shape.text,
                    confidence=confidence,
                    status="pending",
                )
                db.add(candidate)
                diagnostics.objects_classified += 1

            diagnostics.racks_detected = racks_detected
            diagnostics.ambiguous_count = diagnostics.objects_classified - racks_detected
            diagnostics.finished_at = datetime.now(UTC)
            diagnostics.duration_ms = int((diagnostics.finished_at - diagnostics.started_at).total_seconds() * 1000)

            job.status = "parsed"
            job.finished_at = diagnostics.finished_at
            db.commit()
            logger.info(
                "floor_plan_import_parsed", job_id=job_id, objects_discovered=diagnostics.objects_discovered,
                objects_classified=diagnostics.objects_classified,
            )
        except Exception:
            db.rollback()
            logger.exception("floor_plan_import_unexpected_failure", job_id=job_id)
            _mark_job_failed_on_unexpected_error(db, job_id, source_format="svg", parser_name="svg_sanitizer")
            raise


@celery_app.task(name="app.infrastructure.tasks.floorplan_import.validate_and_run_raster_import_job")
def validate_and_run_raster_import_job(job_id: str, content_b64: str, declared_format: str) -> None:
    """Calibration-only per §10 — no shape extraction, just a validated, diagnosed job
    completion so the frontend can proceed to manual calibration against the image."""
    with get_sync_db() as db:
        job = db.get(FloorPlanImportJob, job_id)
        if job is None:
            logger.error("floor_plan_import_job_not_found", job_id=job_id)
            return
        job.status = "parsing"
        db.commit()

        try:
            diagnostics = FloorPlanImportDiagnostics(
                job_id=job.id, source_format=declared_format, parser_name="raster_calibration_only", parser_version="1",
                started_at=datetime.now(UTC), objects_discovered=0, objects_classified=0, racks_detected=0,
                equipment_detected=0, unsupported_object_count=0, warnings=[], errors=[], ambiguous_count=0,
                rejected_count=0, confirmed_count=0,
            )
            db.add(diagnostics)

            content = base64.b64decode(content_b64)
            try:
                validate_raster_image(content, declared_format=declared_format)
            except SvgRejected as exc:
                job.status = "failed"
                job.rejection_reason = exc.reason
                job.finished_at = datetime.now(UTC)
                diagnostics.errors = [exc.reason]
                diagnostics.finished_at = job.finished_at
                db.commit()
                logger.warning("floor_plan_import_rejected", job_id=job_id, reason=exc.reason)
                return

            diagnostics.warnings = ["raster image accepted for calibration-only import; no shape auto-detection is performed"]
            diagnostics.finished_at = datetime.now(UTC)
            diagnostics.duration_ms = int((diagnostics.finished_at - diagnostics.started_at).total_seconds() * 1000)
            job.status = "parsed"
            job.finished_at = diagnostics.finished_at
            db.commit()
            logger.info("floor_plan_raster_import_validated", job_id=job_id)
        except Exception:
            db.rollback()
            logger.exception("floor_plan_import_unexpected_failure", job_id=job_id)
            _mark_job_failed_on_unexpected_error(db, job_id, source_format=declared_format, parser_name="raster_calibration_only")
            raise
