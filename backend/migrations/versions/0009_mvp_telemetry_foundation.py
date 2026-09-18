"""MVP telemetry foundation, additive to Phase 8.

Revision ID: 0009_mvp_telemetry
Revises: 0008_phase8
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_mvp_telemetry"
down_revision = "0008_phase8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "integration_metric_mapping",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("source_identifier", sa.String(length=255), nullable=False),
        sa.Column("canonical_metric", sa.String(length=64), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("scale", sa.Numeric(18, 8), nullable=False, server_default="1"),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("canonical_metric IN ('temperature_c', 'humidity_percent', 'power_kw', 'load_percent', 'availability')", name=op.f("ck_integration_metric_mapping_canonical_metric_allowed")),
        sa.ForeignKeyConstraint(["integration_id"], ["integration.id"], name=op.f("fk_integration_metric_mapping_integration_id_integration"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration_metric_mapping")),
        sa.UniqueConstraint("integration_id", "source_identifier", name=op.f("uq_metric_mapping_integration_source")),
    )
    op.create_index("ix_integration_metric_mapping_integration_id", "integration_metric_mapping", ["integration_id"])
    op.create_table(
        "telemetry_reading",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("mapping_id", sa.Uuid(), nullable=False),
        sa.Column("managed_asset_id", sa.Uuid(), nullable=True),
        sa.Column("external_identifier", sa.String(length=255), nullable=False),
        sa.Column("dedup_key", sa.String(length=255), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("value", sa.Numeric(18, 8), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.ForeignKeyConstraint(["collector_id"], ["collector.id"], name=op.f("fk_telemetry_reading_collector_id_collector"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["integration_id"], ["integration.id"], name=op.f("fk_telemetry_reading_integration_id_integration"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["mapping_id"], ["integration_metric_mapping.id"], name=op.f("fk_telemetry_reading_mapping_id_integration_metric_mapping"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["managed_asset_id"], ["managed_asset.id"], name=op.f("fk_telemetry_reading_managed_asset_id_managed_asset"), ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telemetry_reading")),
        sa.UniqueConstraint("collector_id", "dedup_key", name=op.f("uq_telemetry_reading_collector_dedup")),
    )
    op.create_index("ix_telemetry_reading_integration_metric_occurred", "telemetry_reading", ["integration_id", "metric", "occurred_at"])
    op.create_index("ix_telemetry_reading_asset_metric_occurred", "telemetry_reading", ["managed_asset_id", "metric", "occurred_at"])
    _seed_permissions()


def _seed_permissions() -> None:
    from app.application.rbac import DEFAULT_ROLE_PERMISSIONS
    bind = op.get_bind()
    permissions = {(row.resource, row.action): row.id for row in bind.execute(sa.text("SELECT id, resource, action FROM permission"))}
    roles = {row.name: row.id for row in bind.execute(sa.text("SELECT id, name FROM role"))}
    existing = {(row.role_id, row.permission_id) for row in bind.execute(sa.text("SELECT role_id, permission_id FROM role_permission"))}
    for codes in DEFAULT_ROLE_PERMISSIONS.values():
        for code in codes:
            resource, action = code.split(":")
            if (resource, action) not in permissions:
                permission_id = _uuid.uuid4()
                bind.execute(sa.text("INSERT INTO permission (id, resource, action, description) VALUES (:id, :r, :a, :d)"), {"id": permission_id, "r": resource, "a": action, "d": f"{action} on {resource}"})
                permissions[(resource, action)] = permission_id
    for role_name, codes in DEFAULT_ROLE_PERMISSIONS.items():
        if role_name not in roles:
            continue
        for code in codes:
            resource, action = code.split(":")
            key = (roles[role_name], permissions[(resource, action)])
            if key not in existing:
                bind.execute(sa.text("INSERT INTO role_permission (role_id, permission_id) VALUES (:role_id, :permission_id)"), {"role_id": key[0], "permission_id": key[1]})


def downgrade() -> None:
    op.drop_index("ix_telemetry_reading_asset_metric_occurred", table_name="telemetry_reading")
    op.drop_index("ix_telemetry_reading_integration_metric_occurred", table_name="telemetry_reading")
    op.drop_table("telemetry_reading")
    op.drop_index("ix_integration_metric_mapping_integration_id", table_name="integration_metric_mapping")
    op.drop_table("integration_metric_mapping")
