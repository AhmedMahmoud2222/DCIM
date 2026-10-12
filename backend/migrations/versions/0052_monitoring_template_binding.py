"""G9b optional monitoring-template registry binding; existing rows remain unbound.

Provisional sibling migration: recheck/reparent after G8/G6 land; never merge with
multiple heads. No historical backfill or published-template rewriting.
"""

import sqlalchemy as sa
from alembic import op

revision = "0052_monitoring_template_binding"
down_revision = "0049_telemetry_replay_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("monitoring_metric_template", sa.Column("registry_metric", sa.String(64), nullable=True))
    op.add_column("monitoring_metric_template", sa.Column("registry_version", sa.String(16), nullable=True))
    op.add_column("monitoring_metric_template", sa.Column("threshold_unit", sa.String(32), nullable=True))


def downgrade() -> None:
    # Serialize against authoring writes before checking evidence. A check
    # preceding the DDL lock could miss a binding committed before DROP COLUMN.
    op.execute(sa.text("LOCK TABLE monitoring_metric_template IN ACCESS EXCLUSIVE MODE"))
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM monitoring_metric_template WHERE registry_metric IS NOT NULL "
        "OR registry_version IS NOT NULL OR threshold_unit IS NOT NULL)"
    )).scalar():
        raise RuntimeError("Cannot downgrade 0052: monitoring templates carry registry binding evidence")
    op.drop_column("monitoring_metric_template", "threshold_unit")
    op.drop_column("monitoring_metric_template", "registry_version")
    op.drop_column("monitoring_metric_template", "registry_metric")
