"""AuditLog partition maintenance (§30a — resolves H6). Only creates future monthly
partitions ahead of time; it never archives or drops one — retention duration is an open,
non-blocking organizational decision (ARCHITECTURE_REVIEW.md §49), and the privileged
`dcim_retention_admin` role this task would eventually need for that is created in the
Phase 1 migration but is not used by any code yet (there is nothing to retain until a
duration is approved)."""

from datetime import date

from sqlalchemy import text

from app.core.logging import get_logger
from app.db.sync_session import sync_engine
from app.infrastructure.celery_app import celery_app

logger = get_logger(__name__)

MONTHS_AHEAD = 3


def _month_bounds(base: date, offset_months: int) -> tuple[date, date]:
    year = base.year + (base.month - 1 + offset_months) // 12
    month = (base.month - 1 + offset_months) % 12 + 1
    start = date(year, month, 1)
    end_year = year + (month // 12)
    end_month = month % 12 + 1
    end = date(end_year, end_month, 1)
    return start, end


@celery_app.task(name="app.infrastructure.tasks.audit_partition_maintenance.ensure_future_partitions")
def ensure_future_partitions() -> list[str]:
    created = []
    today = date.today()
    with sync_engine.begin() as conn:
        for offset in range(0, MONTHS_AHEAD):
            start, end = _month_bounds(today, offset)
            partition_name = f"audit_log_{start.strftime('%Y_%m')}"
            conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {partition_name}
                    PARTITION OF audit_log
                    FOR VALUES FROM (:start) TO (:end)
                    """
                ),
                {"start": start, "end": end},
            )
            created.append(partition_name)
    logger.info("audit_log_partitions_ensured", partitions=created)
    return created
