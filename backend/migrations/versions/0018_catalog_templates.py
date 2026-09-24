"""Phase 10A PR-1: NetworkPortTemplate, PowerSupplyTemplate, MonitoringMetricTemplate —
the typed, revision-scoped component templates (spec §4.3/§4.4/§4.6, aligned plan §3.1).

Each gets `fn_reject_write_on_non_draft_revision()` (created by migration 0017) attached
`BEFORE INSERT OR UPDATE OR DELETE`, since each carries `catalog_model_revision_id`
directly — the function's generic `NEW.catalog_model_revision_id`/
`OLD.catalog_model_revision_id` references work unmodified for all three.

Revision ID: 0018_catalog_templates
Revises: 0017_catalog_model
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018_catalog_templates"
down_revision = "0017_catalog_model"
branch_labels = None
depends_on = None

_CHILD_TABLES = ("network_port_template", "power_supply_template", "monitoring_metric_template")


def upgrade() -> None:
    # ---------------------------------------------------------------- NetworkPortTemplate
    op.create_table(
        "network_port_template",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_model_revision_id", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("numbering_pattern", sa.String(length=64), nullable=True),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("supported_speeds_mbps", postgresql.JSONB(), nullable=False),
        sa.Column("connector_type", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="other"),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("module_group", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "media_type IN ('copper', 'fiber', 'other')", name=op.f("ck_network_port_template_media_type_allowed")
        ),
        sa.CheckConstraint(
            "role IN ('uplink', 'access', 'management', 'stack', 'other')",
            name=op.f("ck_network_port_template_role_allowed"),
        ),
        sa.CheckConstraint("side IN ('front', 'rear')", name=op.f("ck_network_port_template_side_allowed")),
        sa.ForeignKeyConstraint(
            ["catalog_model_revision_id"], ["catalog_model_revision.id"],
            name=op.f("fk_network_port_template_catalog_model_revision_id_catalog_model_revision"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_network_port_template")),
        sa.UniqueConstraint(
            "catalog_model_revision_id", "stable_key", name="uq_network_port_template_catalog_model_revision_id"
        ),
    )
    op.create_index(
        "ix_network_port_template_catalog_model_revision_id", "network_port_template", ["catalog_model_revision_id"]
    )

    # ---------------------------------------------------------------- PowerSupplyTemplate
    op.create_table(
        "power_supply_template",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_model_revision_id", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("redundancy_mode", sa.String(length=8), nullable=False, server_default="single"),
        sa.Column("connector_type", sa.String(length=32), nullable=False),
        sa.Column("rated_voltage_min", sa.Numeric(6, 1), nullable=True),
        sa.Column("rated_voltage_max", sa.Numeric(6, 1), nullable=True),
        sa.Column("rated_frequency_hz", sa.Numeric(5, 1), nullable=True),
        sa.Column("rated_current_a", sa.Numeric(6, 2), nullable=True),
        sa.Column("hot_swappable", sa.Boolean(), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("quantity > 0", name=op.f("ck_power_supply_template_quantity_positive")),
        sa.CheckConstraint(
            "redundancy_mode IN ('single', '1+1', 'n+1')", name=op.f("ck_power_supply_template_redundancy_mode_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["catalog_model_revision_id"], ["catalog_model_revision.id"],
            name=op.f("fk_power_supply_template_catalog_model_revision_id_catalog_model_revision"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_power_supply_template")),
        sa.UniqueConstraint(
            "catalog_model_revision_id", "stable_key", name="uq_power_supply_template_catalog_model_revision_id"
        ),
    )
    op.create_index(
        "ix_power_supply_template_catalog_model_revision_id", "power_supply_template", ["catalog_model_revision_id"]
    )

    # ---------------------------------------------------------------- MonitoringMetricTemplate
    op.create_table(
        "monitoring_metric_template",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("catalog_model_revision_id", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("protocol", sa.String(length=8), nullable=False),
        sa.Column("protocol_other_label", sa.String(length=64), nullable=True),
        sa.Column("metric_name", sa.String(length=128), nullable=False),
        sa.Column("oid", sa.String(length=255), nullable=True),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("scale", sa.Numeric(18, 8), nullable=False, server_default="1"),
        sa.Column("transform", sa.String(length=16), nullable=False, server_default="none"),
        sa.Column("offset", sa.Numeric(18, 8), nullable=True),
        sa.Column("default_collection_interval_seconds", sa.Integer(), nullable=True),
        sa.Column("default_warning_threshold", sa.Numeric(18, 4), nullable=True),
        sa.Column("default_critical_threshold", sa.Numeric(18, 4), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("protocol IN ('snmp', 'other')", name=op.f("ck_monitoring_metric_template_protocol_allowed")),
        sa.CheckConstraint(
            "(protocol = 'other' AND protocol_other_label IS NOT NULL) OR "
            "(protocol <> 'other' AND protocol_other_label IS NULL)",
            name=op.f("ck_monitoring_metric_template_protocol_other_label_matches_protocol"),
        ),
        sa.CheckConstraint(
            "value_type IN ('integer', 'float', 'string', 'boolean', 'counter', 'gauge')",
            name=op.f("ck_monitoring_metric_template_value_type_allowed"),
        ),
        sa.CheckConstraint(
            "transform IN ('none', 'scale', 'offset', 'scale_and_offset')",
            name=op.f("ck_monitoring_metric_template_transform_allowed"),
        ),
        sa.CheckConstraint(
            "default_collection_interval_seconds IS NULL OR default_collection_interval_seconds > 0",
            name=op.f("ck_monitoring_metric_template_default_collection_interval_seconds_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_model_revision_id"], ["catalog_model_revision.id"],
            name=op.f("fk_monitoring_metric_template_catalog_model_revision_id_catalog_model_revision"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_monitoring_metric_template")),
        sa.UniqueConstraint(
            "catalog_model_revision_id", "stable_key", name="uq_monitoring_metric_template_catalog_model_revision_id"
        ),
    )
    op.create_index(
        "ix_monitoring_metric_template_catalog_model_revision_id",
        "monitoring_metric_template",
        ["catalog_model_revision_id"],
    )

    for table_name in _CHILD_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table_name}_immutable
            BEFORE INSERT OR UPDATE OR DELETE ON {table_name}
            FOR EACH ROW EXECUTE FUNCTION fn_reject_write_on_non_draft_revision();
            """
        )


def downgrade() -> None:
    for table_name in _CHILD_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table_name}_immutable ON {table_name}")
    op.drop_table("monitoring_metric_template")
    op.drop_table("power_supply_template")
    op.drop_table("network_port_template")
