"""G8 unresolved legacy alarm rule visibility and operator resolution.

Reconciled chain: follows merged G6's 0051 aggregate-lineage revision, which
is the single main head (0049 -> 0053 -> 0052 -> 0054 -> 0051). This keeps
the additive alarm evidence migration in the single telemetry migration chain.
Nullable additions, no backfill. Downgrade refuses to discard block/resolution
evidence and holds the table lock across the check and column removal.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055_alarm_rule_resolution"
down_revision = "0051_aggregate_lineage"
branch_labels = None
depends_on = None

TABLE = "alarm_rule"
ACTOR_FK = "fk_alarm_rule_unit_resolved_by_app_user"


def upgrade() -> None:
    op.add_column(TABLE, sa.Column("unit_resolved_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(TABLE, sa.Column("unit_resolved_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.add_column(TABLE, sa.Column("unit_resolution_reason", sa.String(2048), nullable=True))
    op.add_column(TABLE, sa.Column("blocked_notified_at", sa.TIMESTAMP(timezone=True), nullable=True))
    op.create_foreign_key(ACTOR_FK, TABLE, "app_user", ["unit_resolved_by"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM alarm_rule WHERE unit_resolved_by IS NOT NULL "
        "OR unit_resolved_at IS NOT NULL OR unit_resolution_reason IS NOT NULL OR blocked_notified_at IS NOT NULL)"
    )):
        raise RuntimeError("Cannot downgrade 0055: alarm rules carry block or unit resolution evidence.")
    op.drop_constraint(ACTOR_FK, TABLE, type_="foreignkey")
    for name in ("blocked_notified_at", "unit_resolution_reason", "unit_resolved_at", "unit_resolved_by"):
        op.drop_column(TABLE, name)
