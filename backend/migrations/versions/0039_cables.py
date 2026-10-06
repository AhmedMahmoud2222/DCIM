"""Issue #101: first-class physical cables.

Additive only: `cable`, `cable_endpoint`, their integrity triggers and the `cable:*`
permission seed. Existing `port_connection` rows are neither read nor modified; a cable
only ever realizes a connection later, through the service. Downgrade refuses to discard
recorded cables (including removed-cable history).

Revision ID: 0039_cables
Revises: 0038_discovered_neighbors
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "0039_cables"
down_revision = "0038_discovered_neighbors"
branch_labels = None
depends_on = None

_TYPES = "('copper_utp', 'copper_stp', 'coax', 'fiber_sm', 'fiber_mm', 'dac', 'aoc', 'console', 'other')"
_GRANTS = {
    "read": ("Administrator", "DCIM Manager", "Engineer", "Operator", "Viewer"),
    "manage": ("Administrator", "DCIM Manager", "Engineer"),
}

_ENDPOINT_FUNCTION = """
CREATE FUNCTION cable_require_two_endpoints() RETURNS trigger AS $$
DECLARE
    target uuid;
    total int;
    ends_a int;
    ends_b int;
BEGIN
    IF TG_TABLE_NAME = 'cable' THEN
        target := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        target := OLD.cable_id;
    ELSE
        target := NEW.cable_id;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM cable WHERE id = target) THEN
        RETURN NULL;  -- the cable itself was deleted in this transaction
    END IF;
    SELECT count(*), count(*) FILTER (WHERE end_label = 'A'), count(*) FILTER (WHERE end_label = 'B')
      INTO total, ends_a, ends_b FROM cable_endpoint WHERE cable_id = target;
    IF total <> 2 OR ends_a <> 1 OR ends_b <> 1 THEN
        RAISE EXCEPTION 'cable % must have exactly one A and one B endpoint', target USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql
"""

_ENDPOINT_IMMUTABLE = """
CREATE FUNCTION cable_endpoint_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.cable_id <> OLD.cable_id OR NEW.equipment_port_id <> OLD.equipment_port_id OR NEW.end_label <> OLD.end_label THEN
        RAISE EXCEPTION 'cable endpoints cannot be re-terminated; remove the cable and record a new one'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

_LIFECYCLE_GUARD = """
CREATE FUNCTION cable_lifecycle_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status <> 'planned' THEN
            RAISE EXCEPTION 'installed and removed cable history cannot be deleted' USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'removed' AND (
        NEW.status <> OLD.status OR NEW.label <> OLD.label OR NEW.cable_type <> OLD.cable_type
        OR NEW.installed_at IS DISTINCT FROM OLD.installed_at OR NEW.removed_at IS DISTINCT FROM OLD.removed_at
        OR NEW.length_m IS DISTINCT FROM OLD.length_m OR NEW.source <> OLD.source
        OR NEW.route_metadata <> OLD.route_metadata OR NEW.notes IS DISTINCT FROM OLD.notes
        OR NEW.connector_a IS DISTINCT FROM OLD.connector_a OR NEW.connector_b IS DISTINCT FROM OLD.connector_b
    ) THEN
        RAISE EXCEPTION 'a removed cable is immutable history' USING ERRCODE = '23514';
    END IF;
    IF NEW.status <> OLD.status AND NOT (
        (OLD.status = 'planned' AND NEW.status IN ('installed', 'removed')) OR (OLD.status = 'installed' AND NEW.status = 'removed')
    ) THEN
        RAISE EXCEPTION 'illegal cable lifecycle transition % -> %', OLD.status, NEW.status USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "cable",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("label", sa.String(64), nullable=False),
        sa.Column("cable_type", sa.String(16), nullable=False),
        sa.Column("connector_a", sa.String(32)),
        sa.Column("connector_b", sa.String(32)),
        sa.Column("length_m", sa.Numeric(8, 2)),
        sa.Column("route_metadata", postgresql.JSONB, server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("status", sa.String(16), server_default="planned", nullable=False),
        sa.Column("is_live", sa.Boolean, sa.Computed("status IN ('planned', 'installed')", persisted=True), nullable=False),
        sa.Column("installed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("removed_at", postgresql.TIMESTAMP(timezone=True)),
        sa.Column("source", sa.String(24), server_default="manual", nullable=False),
        sa.Column("source_neighbor_id", UUID(as_uuid=True)),
        sa.Column("port_connection_id", UUID(as_uuid=True)),
        sa.Column("port_connection_created", sa.Boolean, server_default="false", nullable=False),
        sa.Column("notes", sa.String(2000)),
        sa.Column("created_by_user_id", UUID(as_uuid=True)),
        sa.Column("version", sa.Integer, server_default="1", nullable=False),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_cable"),
        sa.UniqueConstraint("id", "is_live", name="uq_cable_id_is_live"),
        sa.ForeignKeyConstraint(
            ["source_neighbor_id"], ["discovered_neighbor.id"], name="fk_cable_source_neighbor_id_discovered_neighbor",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["port_connection_id"], ["port_connection.id"], name="fk_cable_port_connection_id_port_connection",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["app_user.id"], name="fk_cable_created_by_user_id_app_user", ondelete="SET NULL"
        ),
        sa.CheckConstraint(f"cable_type IN {_TYPES}", name="ck_cable_cable_type_allowed"),
        sa.CheckConstraint("status IN ('planned', 'installed', 'removed')", name="ck_cable_status_allowed"),
        sa.CheckConstraint("source IN ('manual', 'discovery_confirmed', 'import')", name="ck_cable_source_allowed"),
        sa.CheckConstraint("length_m IS NULL OR length_m > 0", name="ck_cable_length_positive"),
        sa.CheckConstraint("jsonb_typeof(route_metadata) = 'object'", name="ck_cable_route_metadata_is_object"),
        sa.CheckConstraint("btrim(label) <> ''", name="ck_cable_label_not_blank"),
        sa.CheckConstraint(
            "(status = 'planned' AND installed_at IS NULL AND removed_at IS NULL) OR "
            "(status = 'installed' AND installed_at IS NOT NULL AND removed_at IS NULL) OR "
            "(status = 'removed' AND removed_at IS NOT NULL)",
            name="ck_cable_lifecycle_timestamps_match_status",
        ),
        sa.CheckConstraint(
            "installed_at IS NULL OR removed_at IS NULL OR removed_at >= installed_at", name="ck_cable_removed_after_installed"
        ),
        sa.CheckConstraint(
            "source_neighbor_id IS NULL OR source = 'discovery_confirmed'", name="ck_cable_neighbor_provenance_matches_source"
        ),
    )
    op.create_index("uq_cable_live_label", "cable", [sa.text("lower(label)")], unique=True, postgresql_where=sa.text("is_live"))
    op.create_index("ix_cable_status", "cable", ["status"])
    op.create_index("ix_cable_type", "cable", ["cable_type"])

    op.create_table(
        "cable_endpoint",
        sa.Column("id", UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("cable_id", UUID(as_uuid=True), nullable=False),
        sa.Column("is_live", sa.Boolean, nullable=False),
        sa.Column("end_label", sa.String(1), nullable=False),
        sa.Column("equipment_port_id", UUID(as_uuid=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_cable_endpoint"),
        sa.ForeignKeyConstraint(
            ["cable_id", "is_live"], ["cable.id", "cable.is_live"], name="fk_cable_endpoint_cable_liveness",
            onupdate="CASCADE", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["equipment_port_id"], ["equipment_port.id"], name="fk_cable_endpoint_equipment_port_id_equipment_port",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("cable_id", "end_label", name="uq_cable_endpoint_cable_end"),
        sa.UniqueConstraint("cable_id", "equipment_port_id", name="uq_cable_endpoint_cable_port"),
        sa.CheckConstraint("end_label IN ('A', 'B')", name="ck_cable_endpoint_end_allowed"),
    )
    op.create_index(
        "uq_cable_endpoint_live_port", "cable_endpoint", ["equipment_port_id"], unique=True, postgresql_where=sa.text("is_live")
    )
    op.create_index("ix_cable_endpoint_port", "cable_endpoint", ["equipment_port_id"])

    op.execute("""
        CREATE FUNCTION cable_endpoint_delete_guard() RETURNS trigger AS $
        BEGIN
            IF EXISTS (SELECT 1 FROM cable WHERE id = OLD.cable_id) THEN
                RAISE EXCEPTION 'cable endpoints cannot be deleted or replaced' USING ERRCODE = '23514';
            END IF;
            RETURN OLD;
        END;
        $ LANGUAGE plpgsql
    """)
    op.execute(
        "CREATE TRIGGER cable_endpoint_delete_guard BEFORE DELETE ON cable_endpoint "
        "FOR EACH ROW EXECUTE FUNCTION cable_endpoint_delete_guard()"
    )
    op.execute(_ENDPOINT_FUNCTION)
    op.execute(_ENDPOINT_IMMUTABLE)
    op.execute(_LIFECYCLE_GUARD)
    op.execute(
        "CREATE CONSTRAINT TRIGGER cable_two_endpoints AFTER INSERT ON cable DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION cable_require_two_endpoints()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER cable_endpoint_two_endpoints AFTER INSERT OR UPDATE OR DELETE ON cable_endpoint "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION cable_require_two_endpoints()"
    )
    op.execute(
        "CREATE TRIGGER cable_endpoint_immutable BEFORE UPDATE ON cable_endpoint "
        "FOR EACH ROW EXECUTE FUNCTION cable_endpoint_immutable()"
    )
    op.execute("CREATE TRIGGER cable_lifecycle_guard BEFORE UPDATE OR DELETE ON cable FOR EACH ROW EXECUTE FUNCTION cable_lifecycle_guard()")
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
        [{"id": permission_ids[a], "resource": "cable", "action": a, "description": f"{a} on cable"} for a in _GRANTS],
    )
    names = sorted({name for roles in _GRANTS.values() for name in roles})
    role_ids = dict(op.get_bind().execute(sa.select(role_table.c.name, role_table.c.id).where(role_table.c.name.in_(names))).all())
    missing = set(names) - set(role_ids)
    if missing:
        raise RuntimeError(f"cable RBAC seed expects roles {names}; missing {sorted(missing)}")
    op.bulk_insert(
        role_permission_table,
        [{"role_id": role_ids[n], "permission_id": permission_ids[a]} for a, roles in _GRANTS.items() for n in roles],
    )


def downgrade() -> None:
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM cable")):
        raise RuntimeError(
            "Refusing to drop cables: recorded cables (including removed-cable history) exist. "
            "Export them deliberately before downgrading."
        )
    op.execute("DELETE FROM role_permission WHERE permission_id IN (SELECT id FROM permission WHERE resource = 'cable')")
    op.execute("DELETE FROM permission WHERE resource = 'cable'")
    op.execute("DROP TRIGGER cable_lifecycle_guard ON cable")
    op.execute("DROP TRIGGER cable_endpoint_immutable ON cable_endpoint")
    op.execute("DROP TRIGGER cable_endpoint_two_endpoints ON cable_endpoint")
    op.execute("DROP TRIGGER cable_two_endpoints ON cable")
    op.execute("DROP TRIGGER cable_endpoint_delete_guard ON cable_endpoint")
    op.drop_table("cable_endpoint")
    op.drop_table("cable")
    op.execute("DROP FUNCTION cable_endpoint_delete_guard()")
    op.execute("DROP FUNCTION cable_lifecycle_guard()")
    op.execute("DROP FUNCTION cable_endpoint_immutable()")
    op.execute("DROP FUNCTION cable_require_two_endpoints()")
