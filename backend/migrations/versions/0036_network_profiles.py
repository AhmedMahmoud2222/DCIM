"""Issue #101: vendor/device profiles, profile metric mappings, integration profile binding.

Purely additive: new tables, one nullable FK on `integration`, one nullable JSONB column on
`discovered_device`, and the `network_profile:read|manage` permission seed. No existing row
is rewritten. Downgrade refuses to drop profile data that is still in use.

Revision ID: 0036_network_profiles
Revises: 0035_units_metric_registry
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "0036_network_profiles"
down_revision = "0035_units_metric_registry"
branch_labels = None
depends_on = None

_STATUSES = "('active', 'retired')"
_CODE = "^[a-z0-9][a-z0-9._-]{1,63}$"
_DEVICE_CLASSES = "('switch', 'router', 'firewall', 'pdu', 'ups', 'server', 'storage', 'sensor', 'generic')"

_GRANTS = {
    "read": ("Administrator", "DCIM Manager", "Engineer", "Operator", "Viewer"),
    "manage": ("Administrator", "DCIM Manager"),
}


def upgrade() -> None:
    op.create_table(
        "vendor_profile",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("sys_object_id_prefixes", postgresql.JSONB, nullable=False),
        sa.Column("supported_protocols", postgresql.JSONB, nullable=False),
        sa.Column("discovery_oids", postgresql.JSONB, nullable=False),
        sa.Column("neighbor_discovery", postgresql.JSONB, nullable=False),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
        sa.Column("retired_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_vendor_profile"),
        sa.UniqueConstraint("code", name="uq_vendor_profile_code"),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_vendor_profile_status_allowed"),
        sa.CheckConstraint(f"code ~ '{_CODE}'", name="ck_vendor_profile_code_format"),
        sa.CheckConstraint("jsonb_typeof(sys_object_id_prefixes) = 'array'", name="ck_vendor_profile_prefixes_is_array"),
        sa.CheckConstraint("jsonb_typeof(supported_protocols) = 'array'", name="ck_vendor_profile_protocols_is_array"),
        sa.CheckConstraint("jsonb_typeof(discovery_oids) = 'object'", name="ck_vendor_profile_discovery_oids_is_object"),
        sa.CheckConstraint(
            "jsonb_typeof(neighbor_discovery) = 'object'", name="ck_vendor_profile_neighbor_discovery_is_object"
        ),
        sa.CheckConstraint("(status = 'retired') = (retired_at IS NOT NULL)", name="ck_vendor_profile_retired_at_matches_status"),
        sa.CheckConstraint("version >= 1", name="ck_vendor_profile_version_positive"),
    )

    op.create_table(
        "device_profile",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("vendor_profile_id", UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.String(1000)),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("device_class", sa.String(16), server_default="generic", nullable=False),
        sa.Column("match_criteria", postgresql.JSONB, nullable=False),
        sa.Column("firmware_min", sa.String(64)),
        sa.Column("firmware_max", sa.String(64)),
        sa.Column("priority", sa.Integer, server_default="0", nullable=False),
        sa.Column("capabilities", postgresql.JSONB, nullable=False),
        sa.Column("interface_discovery", postgresql.JSONB, nullable=False),
        sa.Column("neighbor_behavior", postgresql.JSONB, nullable=False),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
        sa.Column("retired_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_device_profile"),
        sa.ForeignKeyConstraint(
            ["vendor_profile_id"], ["vendor_profile.id"], name="fk_device_profile_vendor_profile_id_vendor_profile",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("vendor_profile_id", "code", name="uq_device_profile_vendor_code"),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_device_profile_status_allowed"),
        sa.CheckConstraint(f"device_class IN {_DEVICE_CLASSES}", name="ck_device_profile_device_class_allowed"),
        sa.CheckConstraint(f"code ~ '{_CODE}'", name="ck_device_profile_code_format"),
        sa.CheckConstraint("jsonb_typeof(match_criteria) = 'array'", name="ck_device_profile_match_criteria_is_array"),
        sa.CheckConstraint("jsonb_typeof(capabilities) = 'object'", name="ck_device_profile_capabilities_is_object"),
        sa.CheckConstraint(
            "jsonb_typeof(interface_discovery) = 'object'", name="ck_device_profile_interface_discovery_is_object"
        ),
        sa.CheckConstraint("jsonb_typeof(neighbor_behavior) = 'object'", name="ck_device_profile_neighbor_behavior_is_object"),
        sa.CheckConstraint("(status = 'retired') = (retired_at IS NOT NULL)", name="ck_device_profile_retired_at_matches_status"),
        sa.CheckConstraint("version >= 1", name="ck_device_profile_version_positive"),
    )
    op.create_index("ix_device_profile_vendor_status", "device_profile", ["vendor_profile_id", "status"])

    op.create_table(
        "profile_metric_mapping",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("vendor_profile_id", UUID(as_uuid=True)),
        sa.Column("device_profile_id", UUID(as_uuid=True)),
        sa.Column("oid", sa.String(255), nullable=False),
        sa.Column("canonical_metric", sa.String(64), nullable=False),
        sa.Column("unit", sa.String(32), nullable=False),
        sa.Column("scale", sa.Numeric(18, 8), server_default="1", nullable=False),
        sa.Column("value_type", sa.String(16), server_default="gauge", nullable=False),
        sa.Column("description", sa.String(255)),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_profile_metric_mapping"),
        sa.ForeignKeyConstraint(
            ["vendor_profile_id"], ["vendor_profile.id"],
            name="fk_profile_metric_mapping_vendor_profile_id_vendor_profile", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["device_profile_id"], ["device_profile.id"],
            name="fk_profile_metric_mapping_device_profile_id_device_profile", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "(vendor_profile_id IS NOT NULL AND device_profile_id IS NULL) OR "
            "(vendor_profile_id IS NULL AND device_profile_id IS NOT NULL)",
            name="ck_profile_metric_mapping_single_owner",
        ),
        sa.CheckConstraint(
            "value_type IN ('gauge', 'counter', 'string')", name="ck_profile_metric_mapping_value_type_allowed"
        ),
        sa.CheckConstraint("scale <> 0", name="ck_profile_metric_mapping_scale_nonzero"),
    )
    for owner in ("vendor", "device"):
        op.create_index(
            f"uq_profile_metric_mapping_{owner}_oid", "profile_metric_mapping", [f"{owner}_profile_id", "oid"],
            unique=True, postgresql_where=sa.text(f"{owner}_profile_id IS NOT NULL"),
        )
        op.create_index(
            f"uq_profile_metric_mapping_{owner}_metric", "profile_metric_mapping",
            [f"{owner}_profile_id", "canonical_metric"], unique=True,
            postgresql_where=sa.text(f"{owner}_profile_id IS NOT NULL"),
        )

    op.add_column("integration", sa.Column("device_profile_id", UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_integration_device_profile_id_device_profile", "integration", "device_profile", ["device_profile_id"], ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_integration_device_profile_id", "integration", ["device_profile_id"])
    op.add_column("discovered_device", sa.Column("profile_match", postgresql.JSONB))

    _seed_permissions()


def _seed_permissions() -> None:
    permission_table = sa.table(
        "permission", sa.column("id", UUID), sa.column("resource", sa.String), sa.column("action", sa.String),
        sa.column("description", sa.String),
    )
    role_permission_table = sa.table("role_permission", sa.column("role_id", UUID), sa.column("permission_id", UUID))
    role_table = sa.table("role", sa.column("id", UUID), sa.column("name", sa.String))
    permission_ids = {action: _uuid.uuid4() for action in _GRANTS}
    op.bulk_insert(
        permission_table,
        [
            {"id": permission_ids[action], "resource": "network_profile", "action": action,
             "description": f"{action} on network_profile"}
            for action in _GRANTS
        ],
    )
    connection = op.get_bind()
    names = sorted({name for roles in _GRANTS.values() for name in roles})
    role_ids = dict(connection.execute(sa.select(role_table.c.name, role_table.c.id).where(role_table.c.name.in_(names))).all())
    missing = set(names) - set(role_ids)
    if missing:
        raise RuntimeError(f"network_profile RBAC seed expects roles {names}; missing {sorted(missing)}")
    op.bulk_insert(
        role_permission_table,
        [{"role_id": role_ids[name], "permission_id": permission_ids[action]} for action, roles in _GRANTS.items() for name in roles],
    )


def downgrade() -> None:
    connection = op.get_bind()
    in_use = connection.scalar(
        sa.text(
            "SELECT (SELECT count(*) FROM integration WHERE device_profile_id IS NOT NULL) "
            "+ (SELECT count(*) FROM vendor_profile)"
        )
    )
    if in_use:
        raise RuntimeError(
            "Refusing to drop network profiles: vendor/device profiles exist or integrations are bound to them. "
            "Export and remove them deliberately before downgrading."
        )
    op.execute("DELETE FROM role_permission WHERE permission_id IN (SELECT id FROM permission WHERE resource = 'network_profile')")
    op.execute("DELETE FROM permission WHERE resource = 'network_profile'")
    op.drop_column("discovered_device", "profile_match")
    op.drop_index("ix_integration_device_profile_id", table_name="integration")
    op.drop_constraint("fk_integration_device_profile_id_device_profile", "integration", type_="foreignkey")
    op.drop_column("integration", "device_profile_id")
    op.drop_table("profile_metric_mapping")
    op.drop_index("ix_device_profile_vendor_status", table_name="device_profile")
    op.drop_table("device_profile")
    op.drop_table("vendor_profile")
