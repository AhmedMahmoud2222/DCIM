"""Phase 3: power topology + capacity domain (PDU/UPS/Generator/PowerPanel ManagedAsset
subtypes, PowerNode, PowerConnection, PDUOutlet, PowerCapacity) and its RBAC permission
additions (ARCHITECTURE_REVIEW.md §4b/§13/§13a/§14; PHASE3_GAP_ANALYSIS.md).

Purely additive: no existing Phase 1/2 table, column, or constraint is modified. Follows
migration 0004's own precedent — SQLAlchemy `op.create_table` for ordinary tables/columns/
FKs/simple CHECKs, raw `op.execute` SQL for the two partial unique indexes (duplicate-
active-edge prevention on power_connection; at-most-one-current-capacity-record per node)
that declarative mapping doesn't reach as cleanly, and the same idempotent RBAC seed
mechanism 0002/0004 already established.

Revision ID: 0006_phase3
Revises: 0005_correction
Create Date: 2026-09-17
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op

revision = "0006_phase3"
down_revision = "0005_correction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- ManagedAsset subtypes
    op.create_table(
        "pdu",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("protocol", sa.String(length=16), nullable=False),
        sa.Column("input_voltage", sa.Numeric(8, 2), nullable=True),
        sa.Column("rated_current_a", sa.Numeric(8, 2), nullable=True),
        sa.Column("outlet_count", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("protocol IN ('snmp', 'rest', 'none')", name=op.f("ck_pdu_protocol_allowed")),
        sa.ForeignKeyConstraint(["id"], ["managed_asset.id"], name=op.f("fk_pdu_id_managed_asset"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pdu")),
    )
    op.create_table(
        "ups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("capacity_kva", sa.Numeric(10, 2), nullable=True),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["id"], ["managed_asset.id"], name=op.f("fk_ups_id_managed_asset"), ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["room_id"], ["room.id"], name=op.f("fk_ups_room_id_room"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ups")),
    )
    op.create_table(
        "generator",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("capacity_kw", sa.Numeric(10, 2), nullable=True),
        sa.Column("fuel_type", sa.String(length=32), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"], ["managed_asset.id"], name=op.f("fk_generator_id_managed_asset"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_generator_site_id_site"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generator")),
    )
    op.create_table(
        "power_panel",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("capacity_kw", sa.Numeric(10, 2), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["id"], ["managed_asset.id"], name=op.f("fk_power_panel_id_managed_asset"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["room_id"], ["room.id"], name=op.f("fk_power_panel_room_id_room"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_power_panel")),
    )

    # ---------------------------------------------------------------- PowerNode
    op.create_table(
        "power_node",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("node_type", sa.String(length=32), nullable=False),
        sa.Column("managed_asset_id", sa.Uuid(), nullable=True),
        sa.Column("owning_asset_id", sa.Uuid(), nullable=True),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("retired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "node_type IN ('utility_intake', 'generator', 'ups', 'power_panel', 'power_circuit', 'pdu', "
            "'pdu_outlet', 'equipment_power_input')",
            name=op.f("ck_power_node_node_type_allowed"),
        ),
        sa.CheckConstraint(
            "(managed_asset_id IS NOT NULL OR owning_asset_id IS NOT NULL OR node_type = 'utility_intake') "
            "AND NOT (managed_asset_id IS NOT NULL AND owning_asset_id IS NOT NULL)",
            name=op.f("ck_power_node_asset_reference_exclusive"),
        ),
        sa.ForeignKeyConstraint(
            ["managed_asset_id"], ["managed_asset.id"], name=op.f("fk_power_node_managed_asset_id_managed_asset"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["owning_asset_id"], ["managed_asset.id"], name=op.f("fk_power_node_owning_asset_id_managed_asset"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_power_node")),
        sa.UniqueConstraint("managed_asset_id", name="uq_power_node_managed_asset_id"),
    )

    # ---------------------------------------------------------------- PDUOutlet
    op.create_table(
        "pdu_outlet",
        sa.Column("power_node_id", sa.Uuid(), nullable=False),
        sa.Column("pdu_asset_id", sa.Uuid(), nullable=False),
        sa.Column("outlet_number", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("state IN ('on', 'off', 'unknown')", name=op.f("ck_pdu_outlet_state_allowed")),
        sa.ForeignKeyConstraint(
            ["power_node_id"], ["power_node.id"], name=op.f("fk_pdu_outlet_power_node_id_power_node"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pdu_asset_id"], ["managed_asset.id"], name=op.f("fk_pdu_outlet_pdu_asset_id_managed_asset"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("power_node_id", name=op.f("pk_pdu_outlet")),
        sa.UniqueConstraint("pdu_asset_id", "outlet_number", name="uq_pdu_outlet_pdu_asset_id_outlet_number"),
    )

    # ---------------------------------------------------------------- PowerConnection
    op.create_table(
        "power_connection",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_node_id", sa.Uuid(), nullable=False),
        sa.Column("target_node_id", sa.Uuid(), nullable=False),
        sa.Column("connection_type", sa.String(length=16), nullable=False),
        sa.Column("feed_label", sa.String(length=8), nullable=False),
        sa.Column("phase", sa.String(length=8), nullable=True),
        sa.Column("voltage", sa.Numeric(8, 2), nullable=True),
        sa.Column("rated_current_a", sa.Numeric(8, 2), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("effective_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint("source_node_id <> target_node_id", name=op.f("ck_power_connection_no_self_loop")),
        sa.CheckConstraint(
            "connection_type IN ('feed', 'distribution')", name=op.f("ck_power_connection_connection_type_allowed")
        ),
        sa.CheckConstraint("feed_label IN ('A', 'B', 'single')", name=op.f("ck_power_connection_feed_label_allowed")),
        sa.CheckConstraint("phase IS NULL OR phase IN ('single', 'three')", name=op.f("ck_power_connection_phase_allowed")),
        sa.CheckConstraint(
            "status IN ('active', 'maintenance', 'fault')", name=op.f("ck_power_connection_status_allowed")
        ),
        sa.CheckConstraint("voltage IS NULL OR voltage > 0", name=op.f("ck_power_connection_voltage_positive")),
        sa.CheckConstraint(
            "rated_current_a IS NULL OR rated_current_a > 0", name=op.f("ck_power_connection_rated_current_a_positive")
        ),
        sa.ForeignKeyConstraint(
            ["source_node_id"], ["power_node.id"], name=op.f("fk_power_connection_source_node_id_power_node"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"], ["power_node.id"], name=op.f("fk_power_connection_target_node_id_power_node"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_power_connection")),
    )
    op.create_index("ix_power_connection_source_effective_to", "power_connection", ["source_node_id", "effective_to"])
    op.create_index("ix_power_connection_target_effective_to", "power_connection", ["target_node_id", "effective_to"])
    # Duplicate-active-edge prevention (master prompt §5): the same source->target edge
    # with the same feed_label cannot be opened twice while both are active. Two distinct
    # feed_labels (A and B) between the same pair of nodes remain allowed — that is the
    # legitimate redundant-topology case, not a duplicate.
    op.execute(
        "CREATE UNIQUE INDEX uq_power_connection_active_edge ON power_connection "
        "(source_node_id, target_node_id, feed_label) WHERE effective_to IS NULL"
    )

    # ---------------------------------------------------------------- PowerCapacity
    op.create_table(
        "power_capacity",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("power_node_id", sa.Uuid(), nullable=False),
        sa.Column("rated_capacity_kw", sa.Numeric(10, 3), nullable=True),
        sa.Column("configured_capacity_kw", sa.Numeric(10, 3), nullable=True),
        sa.Column("measured_load_kw", sa.Numeric(10, 3), nullable=True),
        sa.Column("warning_threshold_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("critical_threshold_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("redundancy_factor", sa.String(length=8), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("effective_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "rated_capacity_kw IS NULL OR rated_capacity_kw >= 0", name=op.f("ck_power_capacity_rated_capacity_kw_non_negative")
        ),
        sa.CheckConstraint(
            "configured_capacity_kw IS NULL OR configured_capacity_kw >= 0",
            name=op.f("ck_power_capacity_configured_capacity_kw_non_negative"),
        ),
        sa.CheckConstraint(
            "measured_load_kw IS NULL OR measured_load_kw >= 0", name=op.f("ck_power_capacity_measured_load_kw_non_negative")
        ),
        sa.CheckConstraint(
            "warning_threshold_pct IS NULL OR (warning_threshold_pct >= 0 AND warning_threshold_pct <= 100)",
            name=op.f("ck_power_capacity_warning_threshold_pct_range"),
        ),
        sa.CheckConstraint(
            "critical_threshold_pct IS NULL OR (critical_threshold_pct >= 0 AND critical_threshold_pct <= 100)",
            name=op.f("ck_power_capacity_critical_threshold_pct_range"),
        ),
        sa.CheckConstraint(
            "warning_threshold_pct IS NULL OR critical_threshold_pct IS NULL "
            "OR warning_threshold_pct <= critical_threshold_pct",
            name=op.f("ck_power_capacity_warning_le_critical"),
        ),
        sa.CheckConstraint(
            "redundancy_factor IS NULL OR redundancy_factor IN ('N', 'N+1', '2N', '2N+1')",
            name=op.f("ck_power_capacity_redundancy_factor_allowed"),
        ),
        sa.ForeignKeyConstraint(
            ["power_node_id"], ["power_node.id"], name=op.f("fk_power_capacity_power_node_id_power_node"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_power_capacity")),
    )
    op.create_index("ix_power_capacity_power_node_effective_to", "power_capacity", ["power_node_id", "effective_to"])
    # At most one *current* capacity record per node (temporal, same "current = NULL
    # effective_to" convention as RackPlacement/EquipmentPlacement/PowerConnection).
    op.execute(
        "CREATE UNIQUE INDEX uq_power_capacity_current_per_node ON power_capacity "
        "(power_node_id) WHERE effective_to IS NULL"
    )

    _seed_new_permissions()


def _seed_new_permissions() -> None:
    """Adds the Phase 3 permission codes (power:*/capacity:*/dashboard:read) to the
    existing RBAC tables without touching any Phase 1/2 permission/role/role_permission
    row — the exact same idempotent mechanism migration 0004 established, reading from
    the same `DEFAULT_ROLE_PERMISSIONS` single source of truth."""
    from app.application.rbac import DEFAULT_ROLE_PERMISSIONS

    bind = op.get_bind()

    all_codes = sorted({code for codes in DEFAULT_ROLE_PERMISSIONS.values() for code in codes})
    existing_permissions = {
        (row.resource, row.action): row.id
        for row in bind.execute(sa.text("SELECT id, resource, action FROM permission")).fetchall()
    }
    existing_roles = {row.name: row.id for row in bind.execute(sa.text("SELECT id, name FROM role")).fetchall()}
    existing_role_permissions = {
        (row.role_id, row.permission_id)
        for row in bind.execute(sa.text("SELECT role_id, permission_id FROM role_permission")).fetchall()
    }

    permission_ids: dict[tuple[str, str], _uuid.UUID] = dict(existing_permissions)
    for code in all_codes:
        resource, action = code.split(":")
        if (resource, action) in permission_ids:
            continue
        new_id = _uuid.uuid4()
        permission_ids[(resource, action)] = new_id
        bind.execute(
            sa.text(
                "INSERT INTO permission (id, resource, action, description) "
                "VALUES (:id, :resource, :action, :description)"
            ),
            {"id": new_id, "resource": resource, "action": action, "description": f"{action} on {resource}"},
        )

    for role_name, codes in DEFAULT_ROLE_PERMISSIONS.items():
        role_id = existing_roles.get(role_name)
        if role_id is None:
            continue
        for code in codes:
            resource, action = code.split(":")
            permission_id = permission_ids[(resource, action)]
            if (role_id, permission_id) in existing_role_permissions:
                continue
            bind.execute(
                sa.text("INSERT INTO role_permission (role_id, permission_id) VALUES (:role_id, :permission_id)"),
                {"role_id": role_id, "permission_id": permission_id},
            )
            existing_role_permissions.add((role_id, permission_id))


def downgrade() -> None:
    # RBAC rows are left in place on downgrade — same precedent as 0002/0004.
    op.execute("DROP INDEX IF EXISTS uq_power_capacity_current_per_node")
    op.drop_index("ix_power_capacity_power_node_effective_to", table_name="power_capacity")
    op.drop_table("power_capacity")
    op.execute("DROP INDEX IF EXISTS uq_power_connection_active_edge")
    op.drop_index("ix_power_connection_target_effective_to", table_name="power_connection")
    op.drop_index("ix_power_connection_source_effective_to", table_name="power_connection")
    op.drop_table("power_connection")
    op.drop_table("pdu_outlet")
    op.drop_table("power_node")
    op.drop_table("power_panel")
    op.drop_table("generator")
    op.drop_table("ups")
    op.drop_table("pdu")
