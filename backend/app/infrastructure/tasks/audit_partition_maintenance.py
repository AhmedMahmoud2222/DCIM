"""AuditLog partition maintenance (§30a — resolves H6). Only creates future monthly
partitions ahead of time; it never archives or drops one — retention duration is an open,
non-blocking organizational decision (ARCHITECTURE_REVIEW.md §49).

Partition creation goes through the `dcim_create_audit_log_partition` SQL function
(scripts/bootstrap_privileged_roles.sql), not a raw `CREATE TABLE ... PARTITION OF`, as of
the Finding C1 correction (PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md /
PHASE1_CORRECTION_REPORT.md): audit_log is now owned by the privileged
`dcim_retention_admin` role, not by this task's own `dcim_app` credentials, so attaching a
new partition (an implicit ALTER of the parent table) is no longer something dcim_app can
do directly. The SQL function is SECURITY DEFINER, owned by dcim_retention_admin, and
dcim_app is granted EXECUTE on it and nothing more — this task can trigger exactly this
one privileged operation without holding ALTER/DROP/TRUNCATE on audit_log itself."""

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
                text("SELECT dcim_create_audit_log_partition(:name, :start, :end)"),
                {"name": partition_name, "start": start, "end": end},
            )
            created.append(partition_name)
    logger.info("audit_log_partitions_ensured", partitions=created)
    return created
