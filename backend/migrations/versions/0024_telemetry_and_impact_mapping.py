"""Phase 10C: telemetry sensor bindings for instantiated port/power-inlet markers, plus
the cached latest-status row each binding's poller/ingest path keeps current
(app/domain/telemetry/mapping_models.py's own module docstring has the full rationale,
including why this is a new module rather than an addition to the MVP telemetry tables
migration 0009 already created).

The failure-impact ("blast radius") engine (app/application/impact_service.py) needs no
new tables of its own — it is pure graph traversal over tables this migration does not
touch (`power_node`/`power_connection` from migration 0006, `equipment_port`/
`port_connection` from migration 0023).

Revision ID: 0024_telemetry_and_impact_mapping
Revises: 0023_equipment_instantiation
Create Date: 2026-09-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_telemetry_impact_map"
down_revision = "0023_equipment_instantiation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "port_telemetry_binding",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("equipment_id", sa.Uuid(), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("equipment_port_id", sa.Uuid(), nullable=True),
        sa.Column("equipment_power_inlet_id", sa.Uuid(), nullable=True),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("external_ref", sa.String(length=255), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "target_type IN ('network_port', 'power_inlet', 'environmental')",
            name=op.f("ck_port_telemetry_binding_target_type_allowed"),
        ),
        sa.CheckConstraint(
            "protocol IN ('snmp', 'modbus', 'pdu_outlet', 'synthetic')",
            name=op.f("ck_port_telemetry_binding_protocol_allowed"),
        ),
        sa.CheckConstraint(
            "(target_type = 'network_port' AND equipment_port_id IS NOT NULL AND equipment_power_inlet_id IS NULL) OR "
            "(target_type = 'power_inlet' AND equipment_power_inlet_id IS NOT NULL AND equipment_port_id IS NULL) OR "
            "(target_type = 'environmental' AND equipment_port_id IS NULL AND equipment_power_inlet_id IS NULL)",
            name=op.f("ck_port_telemetry_binding_target_reference_matches_type"),
        ),
        sa.ForeignKeyConstraint(
            ["equipment_id"], ["equipment.id"], name=op.f("fk_port_telemetry_binding_equipment_id_equipment"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_port_id"], ["equipment_port.id"],
            name=op.f("fk_port_telemetry_binding_equipment_port_id_equipment_port"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_power_inlet_id"], ["equipment_power_inlet.id"],
            name=op.f("fk_port_telemetry_binding_equipment_power_inlet_id_equipment_power_inlet"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_port_telemetry_binding")),
        sa.UniqueConstraint("equipment_port_id", name="uq_port_telemetry_binding_equipment_port_id"),
        sa.UniqueConstraint("equipment_power_inlet_id", name="uq_port_telemetry_binding_equipment_power_inlet_id"),
    )
    op.create_index("ix_port_telemetry_binding_equipment_id", "port_telemetry_binding", ["equipment_id"])

    op.create_table(
        "telemetry_latest_status",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("status_level", sa.String(length=16), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("sampled_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status_level IN ('UP', 'DOWN', 'DEGRADED', 'NORMAL', 'WARNING', 'CRITICAL')",
            name=op.f("ck_telemetry_latest_status_status_level_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["binding_id"], ["port_telemetry_binding.id"],
            name=op.f("fk_telemetry_latest_status_binding_id_port_telemetry_binding"), ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telemetry_latest_status")),
        sa.UniqueConstraint("binding_id", name="uq_telemetry_latest_status_binding_id"),
    )


def downgrade() -> None:
    op.drop_table("telemetry_latest_status")
    op.drop_index("ix_port_telemetry_binding_equipment_id", table_name="port_telemetry_binding")
    op.drop_table("port_telemetry_binding")
