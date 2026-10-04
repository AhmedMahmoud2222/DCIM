"""Celery foundation (§20 of the Phase 1 prompt). Named queues match the architecture's
async job design (§35) so later phases can isolate workloads; Phase 1 only actually uses
`default` and `maintenance` — the rest are declared so routing is stable when integration/
telemetry/alarm/import/report/notification work is added in later phases, not so they can
be exercised now."""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery("dcim", broker=settings.redis_url, backend=settings.redis_url)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_default_queue="default",
    task_queues={
        "default": {},
        "maintenance": {},
        "polling": {},
        "telemetry": {},
        "alarms": {},
        "imports": {},
        "reports": {},
        "notifications": {},
        "high_priority": {},
        "extraction": {},
    },
    beat_schedule={
        "dispatch-outbox-events": {
            "task": "app.infrastructure.tasks.outbox_dispatcher.dispatch_pending_outbox_events",
            "schedule": 5.0,
            "options": {"queue": "default"},
        },
        "prune-collector-nonces-and-heartbeats": {
            "task": "app.infrastructure.tasks.maintenance.prune_collector_nonces_and_heartbeats",
            "schedule": 3600.0,
            "options": {"queue": "maintenance"},
        },
        "ensure-audit-log-partitions": {
            "task": "app.infrastructure.tasks.audit_partition_maintenance.ensure_future_partitions",
            "schedule": 86400.0,
            "options": {"queue": "maintenance"},
        },
        "purge-expired-staged-catalog-documents": {
            "task": "app.infrastructure.tasks.maintenance.purge_expired_staged_catalog_documents",
            "schedule": 86400.0,
            "options": {"queue": "maintenance"},
        },
        "requeue-stuck-catalog-extraction-jobs": {
            "task": "app.infrastructure.tasks.catalog_extraction.requeue_stuck_catalog_extraction_jobs",
            "schedule": 60.0,
            "options": {"queue": "default"},
        },
        # SEC (Codex PR #50 review, ROUND 3, finding #1): half of
        # BULK_IMPORT_COMMIT_LEASE_SECONDS (app/application/bulk_import/limits.py), so a
        # commit stuck by a crashed worker is typically caught within about 1.5x the lease
        # duration rather than waiting a full extra lease window on top of the expiry
        # itself.
        "requeue-stuck-bulk-import-commits": {
            "task": "app.infrastructure.tasks.bulk_import.requeue_stuck_bulk_import_commits",
            "schedule": 30.0,
            "options": {"queue": "default"},
        },
    },
)

celery_app.conf.imports = (
    "app.infrastructure.tasks.outbox_dispatcher",
    "app.infrastructure.tasks.audit_partition_maintenance",
    "app.infrastructure.tasks.maintenance",
    "app.infrastructure.tasks.floorplan_import",
    "app.infrastructure.tasks.bulk_import",
    "app.infrastructure.tasks.catalog_extraction",
)
# `autodiscover_tasks` assumes a Django-style `<package>.tasks` submodule per app and
# does not fit this project's layout (multiple task modules directly under
# infrastructure/tasks/) — explicit imports are simpler and don't fail silently.
#
# app.db.models must be imported here, in the worker process, for the same reason
# migrations/env.py imports it: any task module that only imports the specific ORM
# classes it directly touches (e.g. floorplan_import.py importing just
# FloorPlanImportJob) leaves every *other* mapped class — including the target of that
# class's own foreign keys, like app_user or room — unregistered on Base.metadata in
# this process. SQLAlchemy resolves FK targets lazily at first flush, so this surfaces
# not at import time but as a `NoReferencedTableError` the first time a worker actually
# tries to INSERT a row with such a foreign key — found via an empirical browser-driven
# smoke test of the floor-plan import pipeline (uploads got stuck at status=queued
# forever, since the task raised before it could ever mark the job parsed/failed).
import app.db.models  # noqa: E402,F401
import app.infrastructure.tasks.audit_partition_maintenance  # noqa: E402,F401
import app.infrastructure.tasks.bulk_import  # noqa: E402,F401
import app.infrastructure.tasks.catalog_extraction  # noqa: E402,F401
import app.infrastructure.tasks.floorplan_import  # noqa: E402,F401
import app.infrastructure.tasks.maintenance  # noqa: E402,F401
import app.infrastructure.tasks.outbox_dispatcher  # noqa: E402,F401
