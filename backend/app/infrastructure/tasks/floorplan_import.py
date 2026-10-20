"""Floor-plan import job execution (§10/§36: "floor-plan import... is asynchronous —
202 Accepted + a job resource URL to poll"). The uploaded bytes are passed as the task
argument (base64-encoded, JSON-serializable) rather than written to a shared filesystem
path of this pipeline's own choosing — a reasonable choice at the size caps this pipeline
enforces (20 MB for DXF/VSDX/raster, 5 MB for SVG); a much larger ceiling would call for a
different transport (e.g. object storage with a short-lived signed reference), not attempted
here. (This is a separate claim from app.application.svg_sanitizer's own docstring: the
upload *endpoint* itself, upstream of this task, does spool to a real OS temp file above 1MB
via Starlette's UploadFile.)

Issue #104: DXF and VSDX never parse in this process. `run_spatial_import_job` hands the bytes to
app.application.spatial_import.runner, which runs the parser in a sandboxed child (no network, no
filesystem write, no credentials, bounded CPU/memory/time/output) and re-validates the sanitized
intermediate representation it returns. SVG keeps its in-process hardened sanitizer (its XML/DoS
controls predate this work) but now flows through the same SIR/candidate pipeline. Every task starts
from `queued` only, so a redelivered message cannot double-create candidates."""

import base64
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.application.spatial_import.persist import mark_rejected, new_diagnostics, store_parse_result
from app.application.spatial_import.runner import ParseFailure, parse_in_sandbox
from app.application.spatial_import.sir import SirDocument
from app.application.spatial_import.svg_adapter import sir_from_svg
from app.application.svg_sanitizer import SvgRejected, sanitize_svg, validate_raster_image
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.sync_session import get_sync_db
from app.domain.floorplan_import.models import FloorPlanImportDiagnostics, FloorPlanImportJob
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)


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
    job.dedup_key = None
    db.add(
        FloorPlanImportDiagnostics(
            job_id=job.id, source_format=source_format, parser_name=parser_name, parser_version="1",
            started_at=finished_at, finished_at=finished_at, objects_discovered=0, objects_classified=0,
            racks_detected=0, equipment_detected=0, unsupported_object_count=0, warnings=[],
            errors=["internal error during parsing"], ambiguous_count=0, rejected_count=0, confirmed_count=0,
            failure_code="internal_error",
        )
    )
    db.commit()


def _start(db: Session, job_id: str) -> FloorPlanImportJob | None:
    job = db.get(FloorPlanImportJob, job_id)
    if job is None:
        logger.error("floor_plan_import_job_not_found", job_id=job_id)
        return None
    if job.status != "queued":
        logger.warning("floor_plan_import_not_queued", job_id=job_id, status=job.status)
        return None
    job.status = "parsing"
    db.commit()
    return job


@celery_app.task(name="app.infrastructure.tasks.floorplan_import.run_floor_plan_import_job")
def run_floor_plan_import_job(job_id: str, content_b64: str) -> None:
    with get_sync_db() as db:
        job = _start(db, job_id)
        if job is None:
            return
        try:
            diagnostics = new_diagnostics(job, source_format="svg", parser_name="svg_sanitizer", parser_version="1")
            db.add(diagnostics)
            content = base64.b64decode(content_b64)
            try:
                result = sanitize_svg(content)
            except SvgRejected as exc:
                mark_rejected(db, job, diagnostics, code="svg_rejected", reason=exc.reason)
                db.commit()
                logger.warning("floor_plan_import_rejected", job_id=job_id, reason=exc.reason)
                return
            document = sir_from_svg(result)
            store_parse_result(db, job, diagnostics, document, document.to_dict())
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


@celery_app.task(name="app.infrastructure.tasks.floorplan_import.run_spatial_import_job")
def run_spatial_import_job(job_id: str, content_b64: str, source_format: str) -> None:
    """DXF / VSDX: isolated parse -> SIR -> candidates. A rejected or hostile file ends in `failed` with a
    stable failure code and no candidates."""
    with get_sync_db() as db:
        job = _start(db, job_id)
        if job is None:
            return
        parser_name = {"dxf": "dxf_ascii_subset", "vsdx": "vsdx_zip_xml"}.get(source_format, "unsupported")
        try:
            diagnostics = new_diagnostics(job, source_format=source_format, parser_name=parser_name, parser_version="1")
            db.add(diagnostics)
            content = base64.b64decode(content_b64)
            settings = get_settings()
            try:
                outcome = parse_in_sandbox(
                    content, source_format, require_landlock=settings.catalog_extraction_require_landlock
                )
            except ParseFailure as exc:
                mark_rejected(db, job, diagnostics, code=exc.code, reason=exc.reason)
                db.commit()
                logger.warning("floor_plan_import_rejected", job_id=job_id, code=exc.code, source_format=source_format)
                return
            document: SirDocument = outcome.document
            diagnostics.parser_name = document.parser_name
            diagnostics.parser_version = document.parser_version
            store_parse_result(db, job, diagnostics, document, outcome.raw)
            db.commit()
            logger.info(
                "floor_plan_import_parsed", job_id=job_id, source_format=source_format,
                objects_discovered=diagnostics.objects_discovered, objects_classified=diagnostics.objects_classified,
            )
        except Exception:
            db.rollback()
            logger.exception("floor_plan_import_unexpected_failure", job_id=job_id)
            _mark_job_failed_on_unexpected_error(db, job_id, source_format=source_format, parser_name=parser_name)
            raise


@celery_app.task(name="app.infrastructure.tasks.floorplan_import.validate_and_run_raster_import_job")
def validate_and_run_raster_import_job(job_id: str, content_b64: str, declared_format: str) -> None:
    """Calibration-only per §10 — no shape extraction, just a validated, diagnosed job
    completion so the frontend can proceed to manual calibration against the image."""
    with get_sync_db() as db:
        job = _start(db, job_id)
        if job is None:
            return
        try:
            diagnostics = new_diagnostics(
                job, source_format=declared_format, parser_name="raster_calibration_only", parser_version="1"
            )
            db.add(diagnostics)
            content = base64.b64decode(content_b64)
            try:
                validate_raster_image(content, declared_format=declared_format)
            except SvgRejected as exc:
                mark_rejected(db, job, diagnostics, code="raster_rejected", reason=exc.reason)
                db.commit()
                logger.warning("floor_plan_import_rejected", job_id=job_id, reason=exc.reason)
                return
            document = SirDocument(
                source_format=declared_format, parser_name="raster_calibration_only", parser_version="1", source_units="px",
                units_trusted=False, y_axis="down", notes={"calibration_only": True},
            )
            store_parse_result(db, job, diagnostics, document, document.to_dict())
            diagnostics.warnings = ["raster image accepted for calibration-only import; no shape auto-detection is performed"]
            db.commit()
            logger.info("floor_plan_raster_import_validated", job_id=job_id)
        except Exception:
            db.rollback()
            logger.exception("floor_plan_import_unexpected_failure", job_id=job_id)
            _mark_job_failed_on_unexpected_error(db, job_id, source_format=declared_format, parser_name="raster_calibration_only")
            raise
