"""Issue #101: LLDP/CDP neighbor evidence.

Additive only: one new table. Existing `port_connection`/`discovered_device` rows are not
read, rewritten or reinterpreted. Downgrade refuses to discard collected evidence that an
operator has already confirmed or rejected.

Revision ID: 0038_discovered_neighbors
Revises: 0037_network_profiles
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "0038_discovered_neighbors"
down_revision = "0037_network_profiles"
branch_labels = None
depends_on = None

_STATES = "('unmatched', 'ambiguous', 'proposed', 'conflict', 'confirmed', 'rejected')"


def upgrade() -> None:
    op.create_table(
        "discovered_neighbor",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("integration_id", UUID(as_uuid=True), nullable=False),
        sa.Column("source_collector_id", UUID(as_uuid=True)),
        sa.Column("protocol", sa.String(8), nullable=False),
        sa.Column("identity_key", sa.String(64), nullable=False),
        sa.Column("scan_id", sa.String(64)),
        sa.Column("first_seen_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("last_seen_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", sa.String(8), server_default="active", nullable=False),
        sa.Column("local_port_name", sa.String(255)),
        sa.Column("local_port_ref", sa.String(64)),
        sa.Column("remote_chassis_ident", sa.String(255), nullable=False),
        sa.Column("remote_chassis_subtype", sa.String(32)),
        sa.Column("remote_port_ident", sa.String(255), nullable=False),
        sa.Column("remote_port_subtype", sa.String(32)),
        sa.Column("remote_port_description", sa.String(255)),
        sa.Column("remote_system_name", sa.String(255)),
        sa.Column("remote_system_description", sa.String(512)),
        sa.Column("remote_platform", sa.String(255)),
        sa.Column("remote_management_address", sa.String(64)),
        sa.Column("capabilities", postgresql.JSONB, nullable=False),
        sa.Column("native_vlan", sa.Integer),
        sa.Column("ttl_seconds", sa.Integer),
        sa.Column("raw_evidence", postgresql.JSONB, nullable=False),
        sa.Column("reconciliation_state", sa.String(16), server_default="unmatched", nullable=False),
        sa.Column("match_evidence", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("local_port_id", UUID(as_uuid=True)),
        sa.Column("remote_port_id", UUID(as_uuid=True)),
        sa.Column("decided_by_user_id", UUID(as_uuid=True)),
        sa.Column("decided_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("decision_reason", sa.String(1000)),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_discovered_neighbor"),
        sa.ForeignKeyConstraint(
            ["integration_id"], ["integration.id"], name="fk_discovered_neighbor_integration_id_integration", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_collector_id"], ["collector.id"], name="fk_discovered_neighbor_source_collector_id_collector",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["local_port_id"], ["equipment_port.id"], name="fk_discovered_neighbor_local_port_id_equipment_port",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["remote_port_id"], ["equipment_port.id"], name="fk_discovered_neighbor_remote_port_id_equipment_port",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by_user_id"], ["app_user.id"], name="fk_discovered_neighbor_decided_by_user_id_app_user",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint("identity_key", name="uq_discovered_neighbor_identity_key"),
        sa.CheckConstraint("protocol IN ('lldp', 'cdp')", name="ck_discovered_neighbor_protocol_allowed"),
        sa.CheckConstraint("status IN ('active', 'stale')", name="ck_discovered_neighbor_status_allowed"),
        sa.CheckConstraint(f"reconciliation_state IN {_STATES}", name="ck_discovered_neighbor_reconciliation_state_allowed"),
        sa.CheckConstraint(
            "local_port_name IS NOT NULL OR local_port_ref IS NOT NULL", name="ck_discovered_neighbor_local_port_identified"
        ),
        sa.CheckConstraint("jsonb_typeof(capabilities) = 'array'", name="ck_discovered_neighbor_capabilities_is_array"),
        sa.CheckConstraint("jsonb_typeof(raw_evidence) = 'object'", name="ck_discovered_neighbor_raw_evidence_is_object"),
        sa.CheckConstraint("jsonb_typeof(match_evidence) = 'object'", name="ck_discovered_neighbor_match_evidence_is_object"),
        sa.CheckConstraint("last_seen_at >= first_seen_at", name="ck_discovered_neighbor_seen_order"),
        sa.CheckConstraint(
            "local_port_id IS NULL OR remote_port_id IS NULL OR local_port_id <> remote_port_id",
            name="ck_discovered_neighbor_distinct_linked_ports",
        ),
    )
    op.create_index("ix_discovered_neighbor_integration_status", "discovered_neighbor", ["integration_id", "status"])
    op.create_index("ix_discovered_neighbor_state", "discovered_neighbor", ["reconciliation_state", "status"])
    op.create_index("ix_discovered_neighbor_local_port", "discovered_neighbor", ["local_port_id"])
    op.create_index("ix_discovered_neighbor_remote_port", "discovered_neighbor", ["remote_port_id"])


def downgrade() -> None:
    decided = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM discovered_neighbor WHERE reconciliation_state IN ('confirmed', 'rejected')")
    )
    if decided:
        raise RuntimeError(
            "Refusing to drop discovered_neighbor: operator-confirmed or rejected neighbor decisions exist. "
            "Export them deliberately before downgrading."
        )
    op.drop_table("discovered_neighbor")
