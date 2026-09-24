"""Phase 10B: equipment catalog instantiation + physical port/power-inlet instances
(docs describe this as "Asset Catalog Instantiation, Interactive Rack Elevation
Integration & Telemetry Mapping", following on from Phase 10A's catalog designer).

Adds `equipment.catalog_model_revision_id` — a nullable, additive snapshot FK to the
published `catalog_model_revision` an instance was instantiated from
(app/application/equipment_instantiation_service.py). NULL for every equipment row
created through the pre-existing `POST /equipment` path against a legacy
`EquipmentModelRevision` id directly — that path is entirely unaffected by this column.

Creates `equipment_port` and `equipment_power_inlet` — snapshot copies of
`NetworkPortTemplate`/`PowerSupplyTemplate` rows made once, at instantiation time, so a
later draft edit to the same catalog model never retroactively changes an already-
deployed instance (see ports.py's own module docstring for why: published revisions and
their child templates are immutable and never deleted, but the copy, not a live join, is
what decouples deployed equipment from future catalog edits). `equipment_power_inlet`
additionally owns a `power_node` row (`node_type='equipment_power_input'`), the same node
type `POST /power/equipment-feeds` (app/api/v1/power.py) already creates by hand for
equipment with no catalog template — this lets `power_capacity.py`'s existing
redundancy/capacity roll-ups see instantiated inlets with no changes to that module.

Creates `port_connection` — one outgoing cabling edge per instantiated network port, to
either another `EquipmentPort` (a patch panel port or adjacent switch interface) or a PDU
outlet's `PowerNode`, mirroring `PowerConnection`'s own real-FK-on-both-ends discipline.

Revision ID: 0023_equipment_instantiation
Revises: 0022_graphic_marker_delete_fix
Create Date: 2026-09-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_equipment_instantiation"
down_revision = "0022_graphic_marker_delete_fix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --------------------------------------------------------- Equipment snapshot column
    op.add_column("equipment", sa.Column("catalog_model_revision_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_equipment_catalog_model_revision_id_catalog_model_revision"),
        "equipment", "catalog_model_revision", ["catalog_model_revision_id"], ["id"], ondelete="RESTRICT",
    )
    op.create_index("ix_equipment_catalog_model_revision_id", "equipment", ["catalog_model_revision_id"])

    # -------------------------------------------------------------------- EquipmentPort
    op.create_table(
        "equipment_port",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("equipment_id", sa.Uuid(), nullable=False),
        sa.Column("network_port_template_id", sa.Uuid(), nullable=True),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("supported_speeds_mbps", postgresql.JSONB(), nullable=False),
        sa.Column("connector_type", sa.String(length=32), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="other"),
        sa.Column("side", sa.String(length=8), nullable=False),
        sa.Column("module_group", sa.String(length=64), nullable=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("media_type IN ('copper', 'fiber', 'other')", name=op.f("ck_equipment_port_media_type_allowed")),
        sa.CheckConstraint(
            "role IN ('uplink', 'access', 'management', 'stack', 'other')", name=op.f("ck_equipment_port_role_allowed")
        ),
        sa.CheckConstraint("side IN ('front', 'rear')", name=op.f("ck_equipment_port_side_allowed")),
        sa.ForeignKeyConstraint(
            ["equipment_id"], ["equipment.id"], name=op.f("fk_equipment_port_equipment_id_equipment"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["network_port_template_id"], ["network_port_template.id"],
            name=op.f("fk_equipment_port_network_port_template_id_network_port_template"), ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_port")),
        sa.UniqueConstraint("equipment_id", "stable_key", name="uq_equipment_port_equipment_id_stable_key"),
    )
    op.create_index("ix_equipment_port_equipment_id", "equipment_port", ["equipment_id"])

    # -------------------------------------------------------------- EquipmentPowerInlet
    op.create_table(
        "equipment_power_inlet",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("equipment_id", sa.Uuid(), nullable=False),
        sa.Column("power_supply_template_id", sa.Uuid(), nullable=True),
        sa.Column("power_node_id", sa.Uuid(), nullable=False),
        sa.Column("stable_key", sa.String(length=64), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("connector_type", sa.String(length=32), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["equipment_id"], ["equipment.id"], name=op.f("fk_equipment_power_inlet_equipment_id_equipment"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["power_supply_template_id"], ["power_supply_template.id"],
            name=op.f("fk_equipment_power_inlet_power_supply_template_id_power_supply_template"), ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["power_node_id"], ["power_node.id"], name=op.f("fk_equipment_power_inlet_power_node_id_power_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_equipment_power_inlet")),
        sa.UniqueConstraint("equipment_id", "stable_key", name="uq_equipment_power_inlet_equipment_id_stable_key"),
        sa.UniqueConstraint("power_node_id", name="uq_equipment_power_inlet_power_node_id"),
    )
    op.create_index("ix_equipment_power_inlet_equipment_id", "equipment_power_inlet", ["equipment_id"])

    # ------------------------------------------------------------------- PortConnection
    op.create_table(
        "port_connection",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_port_id", sa.Uuid(), nullable=False),
        sa.Column("target_port_id", sa.Uuid(), nullable=True),
        sa.Column("target_power_node_id", sa.Uuid(), nullable=True),
        sa.Column("cable_id", sa.String(length=64), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("source_port_id <> target_port_id", name=op.f("ck_port_connection_no_self_loop")),
        sa.CheckConstraint(
            "(target_port_id IS NOT NULL AND target_power_node_id IS NULL) OR "
            "(target_port_id IS NULL AND target_power_node_id IS NOT NULL)",
            name=op.f("ck_port_connection_target_exclusive"),
        ),
        sa.CheckConstraint("status IN ('active', 'planned', 'faulted')", name=op.f("ck_port_connection_status_allowed")),
        sa.ForeignKeyConstraint(
            ["source_port_id"], ["equipment_port.id"], name=op.f("fk_port_connection_source_port_id_equipment_port"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_port_id"], ["equipment_port.id"], name=op.f("fk_port_connection_target_port_id_equipment_port"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_power_node_id"], ["power_node.id"], name=op.f("fk_port_connection_target_power_node_id_power_node"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_port_connection")),
        sa.UniqueConstraint("source_port_id", name="uq_port_connection_source_port_id"),
    )


def downgrade() -> None:
    op.drop_table("port_connection")
    op.drop_index("ix_equipment_power_inlet_equipment_id", table_name="equipment_power_inlet")
    op.drop_table("equipment_power_inlet")
    op.drop_index("ix_equipment_port_equipment_id", table_name="equipment_port")
    op.drop_table("equipment_port")
    op.drop_index("ix_equipment_catalog_model_revision_id", table_name="equipment")
    op.drop_constraint(
        op.f("fk_equipment_catalog_model_revision_id_catalog_model_revision"), "equipment", type_="foreignkey"
    )
    op.drop_column("equipment", "catalog_model_revision_id")
