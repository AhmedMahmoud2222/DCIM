"""Phase 8: Integrations + Collectors (ARCHITECTURE_REVIEW.md §18/§19/§20;
PHASE8_GAP_ANALYSIS.md). Purely additive: no existing Phase 1/2/3 table, column, or
constraint is modified. Follows migration 0006's own precedent -- SQLAlchemy
`op.create_table` for ordinary tables/columns/FKs/simple CHECKs, raw `op.execute` SQL
for the one partial unique index (`collector_assignment`'s "at most one current
assignment per integration", the exact same temporal pattern `power_capacity`/
`power_connection` already established) that declarative mapping doesn't reach as
cleanly, and the same idempotent RBAC seed mechanism 0004/0006 already established.

Revision ID: 0008_phase8
Revises: 0007_correction
Create Date: 2026-09-18
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_phase8"
down_revision = "0007_correction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------------- Collector
    op.create_table(
        "collector",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("collector_type", sa.String(length=16), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="registered"),
        sa.Column("version_string", sa.String(length=64), nullable=True),
        sa.Column("secret_ciphertext", sa.String(length=1000), nullable=False),
        sa.Column("secret_rotated_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("collector_type IN ('central', 'edge')", name=op.f("ck_collector_collector_type_allowed")),
        sa.CheckConstraint("status IN ('registered', 'active', 'disabled')", name=op.f("ck_collector_status_allowed")),
        sa.CheckConstraint(
            "(collector_type = 'central' AND site_id IS NULL) OR (collector_type = 'edge' AND site_id IS NOT NULL)",
            name=op.f("ck_collector_edge_requires_site_central_forbids_site"),
        ),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_collector_site_id_site"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector")),
        sa.UniqueConstraint("name", name=op.f("uq_collector_name")),
    )
    op.create_index("ix_collector_site_id", "collector", ["site_id"])

    # ------------------------------------------------------------ CollectorCapability
    op.create_table(
        "collector_capability",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("protocol_code", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["collector_id"], ["collector.id"], name=op.f("fk_collector_capability_collector_id_collector"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_capability")),
        sa.UniqueConstraint(
            "collector_id", "protocol_code", name=op.f("uq_collector_capability_collector_id_protocol_code")
        ),
    )
    op.create_index("ix_collector_capability_collector_id", "collector_capability", ["collector_id"])

    # -------------------------------------------------------------- CollectorHeartbeat
    op.create_table(
        "collector_heartbeat",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("ts", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("queue_depth", sa.Integer(), nullable=True),
        sa.Column("cpu_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("mem_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="ok"),
        sa.ForeignKeyConstraint(
            ["collector_id"], ["collector.id"], name=op.f("fk_collector_heartbeat_collector_id_collector"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_heartbeat")),
    )
    op.create_index("ix_collector_heartbeat_collector_ts", "collector_heartbeat", ["collector_id", "ts"])

    # ---------------------------------------------------------- CollectorRequestNonce
    op.create_table(
        "collector_request_nonce",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["collector_id"], ["collector.id"], name=op.f("fk_collector_request_nonce_collector_id_collector"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_request_nonce")),
        sa.UniqueConstraint(
            "collector_id", "nonce", name=op.f("uq_collector_request_nonce_collector_id_nonce")
        ),
    )
    op.create_index("ix_collector_request_nonce_collector_id", "collector_request_nonce", ["collector_id"])

    # -------------------------------------------------------------------- Integration
    op.create_table(
        "integration",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("integration_type", sa.String(length=16), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("target_host", sa.String(length=255), nullable=False),
        sa.Column("target_port", sa.Integer(), nullable=True),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("credential_ciphertext", sa.String(length=4000), nullable=True),
        sa.Column("poll_interval_seconds", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("last_poll_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "integration_type IN ('icmp', 'snmp', 'rest')", name=op.f("ck_integration_integration_type_allowed")
        ),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_integration_site_id_site"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_integration")),
        sa.UniqueConstraint("name", name=op.f("uq_integration_name")),
    )
    op.create_index("ix_integration_site_id", "integration", ["site_id"])

    # -------------------------------------------------------------- CollectorAssignment
    op.create_table(
        "collector_assignment",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("collector_id", sa.Uuid(), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("effective_to", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["collector_id"], ["collector.id"], name=op.f("fk_collector_assignment_collector_id_collector"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["integration.id"], name=op.f("fk_collector_assignment_integration_id_integration"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_collector_assignment")),
    )
    op.create_index("ix_collector_assignment_collector_id", "collector_assignment", ["collector_id"])
    # At most one *current* assignment per integration (temporal, same "current = NULL
    # effective_to" convention as RackPlacement/EquipmentPlacement/PowerConnection/
    # PowerCapacity) -- reassignment closes the current row and opens a new one in the
    # same transaction, never an in-place UPDATE that would destroy assignment history.
    op.execute(
        "CREATE UNIQUE INDEX uq_collector_assignment_current_per_integration ON collector_assignment "
        "(integration_id) WHERE effective_to IS NULL"
    )

    # ---------------------------------------------------------------- DiscoveredDevice
    op.create_table(
        "discovered_device",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("integration_id", sa.Uuid(), nullable=False),
        sa.Column("external_identifier", sa.String(length=255), nullable=False),
        sa.Column("discovered_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "raw_attributes", postgresql.JSONB(astext_type=sa.Text()), nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="new"),
        sa.Column("matched_managed_asset_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('new', 'reconciled', 'ignored')", name=op.f("ck_discovered_device_status_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["integration.id"], name=op.f("fk_discovered_device_integration_id_integration"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["matched_managed_asset_id"], ["managed_asset.id"],
            name=op.f("fk_discovered_device_matched_managed_asset_id_managed_asset"), ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovered_device")),
        sa.UniqueConstraint(
            "integration_id", "external_identifier", name=op.f("uq_discovered_device_integration_id_external_identifier")
        ),
    )
    op.create_index("ix_discovered_device_integration_id", "discovered_device", ["integration_id"])

    # -------------------------------------------------------------- ReconciliationDiff
    op.create_table(
        "reconciliation_diff",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("discovered_device_id", sa.Uuid(), nullable=False),
        sa.Column("diff_type", sa.String(length=32), nullable=False),
        sa.Column("field_name", sa.String(length=128), nullable=True),
        sa.Column("discovered_value", sa.String(length=2000), nullable=True),
        sa.Column("authoritative_value", sa.String(length=2000), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("decided_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.CheckConstraint(
            "diff_type IN ('new_device', 'attribute_mismatch', 'missing_in_discovery')",
            name=op.f("ck_reconciliation_diff_diff_type_allowed"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name=op.f("ck_reconciliation_diff_status_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["discovered_device_id"], ["discovered_device.id"],
            name=op.f("fk_reconciliation_diff_discovered_device_id_discovered_device"), ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"], ["app_user.id"], name=op.f("fk_reconciliation_diff_decided_by_user_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reconciliation_diff")),
    )
    op.create_index("ix_reconciliation_diff_discovered_device_id", "reconciliation_diff", ["discovered_device_id"])

    _seed_new_permissions()


def _seed_new_permissions() -> None:
    """Adds the Phase 8 permission codes (integration:*/collector:*/discovery:*) to the
    existing RBAC tables without touching any Phase 1/2/3 permission/role/role_permission
    row — the exact same idempotent mechanism migrations 0004/0006 already established,
    reading from the same `DEFAULT_ROLE_PERMISSIONS` single source of truth."""
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
    # RBAC rows are left in place on downgrade — same precedent as 0002/0004/0006.
    op.drop_index("ix_reconciliation_diff_discovered_device_id", table_name="reconciliation_diff")
    op.drop_table("reconciliation_diff")
    op.drop_index("ix_discovered_device_integration_id", table_name="discovered_device")
    op.drop_table("discovered_device")
    op.execute("DROP INDEX IF EXISTS uq_collector_assignment_current_per_integration")
    op.drop_index("ix_collector_assignment_collector_id", table_name="collector_assignment")
    op.drop_table("collector_assignment")
    op.drop_index("ix_integration_site_id", table_name="integration")
    op.drop_table("integration")
    op.drop_index("ix_collector_request_nonce_collector_id", table_name="collector_request_nonce")
    op.drop_table("collector_request_nonce")
    op.drop_index("ix_collector_heartbeat_collector_ts", table_name="collector_heartbeat")
    op.drop_table("collector_heartbeat")
    op.drop_index("ix_collector_capability_collector_id", table_name="collector_capability")
    op.drop_table("collector_capability")
    op.drop_index("ix_collector_site_id", table_name="collector")
    op.drop_table("collector")
