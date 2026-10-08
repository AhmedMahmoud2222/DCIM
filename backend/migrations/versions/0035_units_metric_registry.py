"""Add source telemetry provenance and registry version without rewriting history.

Revision ID: 0035_units_metric_registry
Revises: 0034_asset_decommission_guard
"""

import sqlalchemy as sa
from alembic import op

revision = "0035_units_metric_registry"
down_revision = "0034_asset_decommission_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("integration_metric_mapping", sa.Column("registry_version", sa.String(16), nullable=True))
    op.add_column("telemetry_reading", sa.Column("raw_value", sa.Numeric(18, 8), nullable=True))
    op.add_column("telemetry_reading", sa.Column("raw_unit", sa.String(32), nullable=True))
    op.add_column("telemetry_reading", sa.Column("source_scale", sa.Numeric(18, 8), nullable=True))
    op.add_column("telemetry_reading", sa.Column("registry_version", sa.String(16), nullable=True))
    op.add_column("daily_telemetry_aggregate", sa.Column("registry_version", sa.String(16), nullable=True))
    op.add_column("alarm_rule", sa.Column("unit", sa.String(32), nullable=True))
    op.add_column("alarm_rule", sa.Column("registry_version", sa.String(16), nullable=True))

    # Freeze only units supported by unambiguous pre-registry evidence. Numeric
    # thresholds/history remain untouched. Conflicting or missing evidence leaves
    # unit NULL and evaluation rejects explicitly rather than guessing a unit.
    op.execute("""
        UPDATE alarm_rule AS rule SET unit = evidence.unit
        FROM (
            SELECT integration_id, metric, min(unit) AS unit
            FROM (
                SELECT integration_id, canonical_metric AS metric, unit
                FROM integration_metric_mapping
                UNION ALL
                SELECT integration_id, metric, unit FROM telemetry_reading
                UNION ALL
                SELECT integration_id, metric, unit FROM daily_telemetry_aggregate
            ) AS sources
            GROUP BY integration_id, metric
            HAVING count(DISTINCT unit) = 1
        ) AS evidence
        WHERE rule.integration_id = evidence.integration_id AND rule.metric = evidence.metric
    """)


def downgrade() -> None:
    # The old application interprets thresholds/readings in source mapping units.
    # Removing registry metadata from live canonical data would silently change
    # those semantics. Lock before checking so a concurrent write cannot race the
    # check and column drops; no values or provenance are rewritten/discarded.
    op.execute("""
        LOCK TABLE integration_metric_mapping, telemetry_reading,
            daily_telemetry_aggregate, alarm_rule IN ACCESS EXCLUSIVE MODE
    """)
    populated_registry = op.get_bind().scalar(sa.text("""
        SELECT EXISTS (
            SELECT 1 FROM integration_metric_mapping WHERE registry_version IS NOT NULL
            UNION ALL
            SELECT 1 FROM alarm_rule WHERE registry_version IS NOT NULL
            UNION ALL
            SELECT 1 FROM daily_telemetry_aggregate WHERE registry_version IS NOT NULL
            UNION ALL
            SELECT 1 FROM telemetry_reading
            WHERE registry_version IS NOT NULL OR raw_value IS NOT NULL
                OR raw_unit IS NOT NULL OR source_scale IS NOT NULL
        )
    """))
    if populated_registry:
        raise RuntimeError(
            "Cannot downgrade 0035: registry telemetry, alarm configuration or raw provenance remains. "
            "Preserve this metadata and establish an explicit legacy-compatible data contract before rollback."
        )
    op.drop_column("alarm_rule", "registry_version")
    op.drop_column("alarm_rule", "unit")
    op.drop_column("daily_telemetry_aggregate", "registry_version")
    op.drop_column("telemetry_reading", "registry_version")
    op.drop_column("telemetry_reading", "source_scale")
    op.drop_column("telemetry_reading", "raw_unit")
    op.drop_column("telemetry_reading", "raw_value")
    op.drop_column("integration_metric_mapping", "registry_version")
