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
    },
    beat_schedule={
        "dispatch-outbox-events": {
            "task": "app.infrastructure.tasks.outbox_dispatcher.dispatch_pending_outbox_events",
            "schedule": 5.0,
            "options": {"queue": "default"},
        },
        "ensure-audit-log-partitions": {
            "task": "app.infrastructure.tasks.audit_partition_maintenance.ensure_future_partitions",
            "schedule": 86400.0,
            "options": {"queue": "maintenance"},
        },
    },
)

celery_app.conf.imports = (
    "app.infrastructure.tasks.outbox_dispatcher",
    "app.infrastructure.tasks.audit_partition_maintenance",
)
# `autodiscover_tasks` assumes a Django-style `<package>.tasks` submodule per app and
# does not fit this project's layout (multiple task modules directly under
# infrastructure/tasks/) — explicit imports are simpler and don't fail silently.
import app.infrastructure.tasks.audit_partition_maintenance  # noqa: E402,F401
import app.infrastructure.tasks.outbox_dispatcher  # noqa: E402,F401
