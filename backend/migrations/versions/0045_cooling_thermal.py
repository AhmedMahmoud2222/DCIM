# ruff: noqa: E501
"""Issue #105: cooling model, environmental sensors, thermal zones and canonical airflow/pressure metrics.

Additive. New: cooling_group, cooling_unit (CRAC/CRAH/chiller ManagedAsset subtype), environmental_sensor
(sensor ManagedAsset subtype), thermal_zone (served zone / supply / return region / hot-cold aisle with optional
containment), containment_element, cooling_unit_zone, plus cooling:read / cooling:manage permissions.
Extended: managed_asset.asset_type allows crac / crah / chiller; integration_metric_mapping.canonical_metric
allows six cooling/environment metrics; equipment_placement gains nullable x_mm / y_mm /
position_calibration_id so placed sensors and cooling units keep their own coordinates per placement row.
No existing row changes.

Database-enforced invariants: a subtype row must reference a ManagedAsset of the matching asset_type
(composite FK to managed_asset(id, asset_type)); a cooling unit and its group share a site; a placed sensor or
cooling unit must be in a room of its own site; a unit-to-zone relationship must stay inside the unit's site; a cooling unit that is still related to a zone
cannot be moved to a decommissioned/removed lifecycle state (checked in the database, so the generic lifecycle
endpoint cannot bypass it).

Downgrade refuses while any Issue #105 data exists, because dropping the tables or narrowing the CHECK lists
would destroy it.

Revision ID: 0045_cooling_thermal
Revises: 0044_spatial_digital_twin
"""

import uuid as _uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = "0045_cooling_thermal"
down_revision = "0044_spatial_digital_twin"
branch_labels = None
depends_on = None

_OLD_ASSET_TYPES = ("rack", "equipment", "pdu", "ups", "generator", "power_panel", "sensor", "cable")
_NEW_ASSET_TYPES = (*_OLD_ASSET_TYPES, "crac", "crah", "chiller")
_OLD_METRICS = ("temperature_c", "humidity_percent", "power_kw", "load_percent", "availability")
_NEW_METRICS = (
    *_OLD_METRICS, "supply_air_temperature_c", "return_air_temperature_c", "airflow_m3_s", "airflow_velocity_m_s",
    "differential_pressure_pa", "cooling_output_kw",
)
_GRANTS = {
    "read": ("Administrator", "DCIM Manager", "Engineer", "Operator", "Viewer"),
    "manage": ("Administrator", "DCIM Manager", "Engineer"),
}


def _uuid_pk() -> sa.Column:
    return sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.func.now(), nullable=False),
    ]


def upgrade() -> None:
    op.drop_constraint(op.f("ck_managed_asset_asset_type_allowed"), "managed_asset", type_="check")
    op.create_check_constraint(op.f("ck_managed_asset_asset_type_allowed"), "managed_asset", f"asset_type IN {_NEW_ASSET_TYPES!r}")
    op.drop_constraint(op.f("ck_integration_metric_mapping_canonical_metric_allowed"), "integration_metric_mapping", type_="check")
    op.create_check_constraint(
        op.f("ck_integration_metric_mapping_canonical_metric_allowed"), "integration_metric_mapping", f"canonical_metric IN {_NEW_METRICS!r}"
    )

    # --- shared placement: direct room-local position for sensors / cooling units
    op.add_column("equipment_placement", sa.Column("x_mm", sa.Integer(), nullable=True))
    op.add_column("equipment_placement", sa.Column("y_mm", sa.Integer(), nullable=True))
    op.add_column("equipment_placement", sa.Column("position_calibration_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_equipment_placement_position_calibration_id_floor_plan_calibration"), "equipment_placement",
        "floor_plan_calibration", ["position_calibration_id"], ["id"], ondelete="SET NULL",
    )
    op.create_check_constraint(op.f("ck_equipment_placement_position_pair"), "equipment_placement", "(x_mm IS NULL) = (y_mm IS NULL)")
    op.create_index(
        "ix_equipment_placement_room_current", "equipment_placement", ["room_id"], postgresql_where=sa.text("effective_to IS NULL")
    )

    # --- cooling_group
    op.create_table(
        "cooling_group",
        _uuid_pk(),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("retired", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_cooling_group_site_id_site"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cooling_group")),
        sa.UniqueConstraint("site_id", "name", name="uq_cooling_group_site_name"),
        sa.UniqueConstraint("id", "site_id", name="uq_cooling_group_id_site"),
    )
    op.create_index("ix_cooling_group_site_id", "cooling_group", ["site_id"])

    # --- cooling_unit (CRAC / CRAH / chiller)
    op.create_table(
        "cooling_unit",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("unit_kind", sa.String(length=16), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("rated_cooling_capacity_kw", sa.Numeric(12, 3), nullable=True),
        sa.Column("configured_cooling_capacity_kw", sa.Numeric(12, 3), nullable=True),
        sa.Column("airflow_capacity_m3_s", sa.Numeric(12, 4), nullable=True),
        sa.Column("supply_air_target_c", sa.Numeric(6, 2), nullable=True),
        sa.Column("return_air_design_c", sa.Numeric(6, 2), nullable=True),
        sa.Column("humidity_min_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("humidity_max_percent", sa.Numeric(5, 2), nullable=True),
        sa.Column("supply_direction_deg", sa.SmallInteger(), nullable=True),
        sa.Column("operating_status", sa.String(length=16), server_default="unknown", nullable=False),
        sa.Column("cooling_group_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cooling_unit")),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_cooling_unit_site_id_site"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["id", "unit_kind"], ["managed_asset.id", "managed_asset.asset_type"], name="fk_cooling_unit_managed_asset_type", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["cooling_group_id", "site_id"], ["cooling_group.id", "cooling_group.site_id"], name="fk_cooling_unit_group_same_site"
        ),
        sa.CheckConstraint("unit_kind IN ('crac', 'crah', 'chiller')", name=op.f("ck_cooling_unit_unit_kind_allowed")),
        sa.CheckConstraint("operating_status IN ('online', 'standby', 'offline', 'fault', 'unknown')", name=op.f("ck_cooling_unit_operating_status_allowed")),
        sa.CheckConstraint("rated_cooling_capacity_kw IS NULL OR rated_cooling_capacity_kw > 0", name=op.f("ck_cooling_unit_rated_capacity_positive")),
        sa.CheckConstraint("configured_cooling_capacity_kw IS NULL OR configured_cooling_capacity_kw > 0", name=op.f("ck_cooling_unit_configured_capacity_positive")),
        sa.CheckConstraint(
            "configured_cooling_capacity_kw IS NULL OR rated_cooling_capacity_kw IS NULL OR configured_cooling_capacity_kw <= rated_cooling_capacity_kw",
            name=op.f("ck_cooling_unit_configured_not_above_rated"),
        ),
        sa.CheckConstraint("airflow_capacity_m3_s IS NULL OR airflow_capacity_m3_s > 0", name=op.f("ck_cooling_unit_airflow_capacity_positive")),
        sa.CheckConstraint("supply_air_target_c IS NULL OR supply_air_target_c BETWEEN -50 AND 100", name=op.f("ck_cooling_unit_supply_target_range")),
        sa.CheckConstraint("return_air_design_c IS NULL OR return_air_design_c BETWEEN -50 AND 100", name=op.f("ck_cooling_unit_return_design_range")),
        sa.CheckConstraint("humidity_min_percent IS NULL OR humidity_min_percent BETWEEN 0 AND 100", name=op.f("ck_cooling_unit_humidity_min_range")),
        sa.CheckConstraint("humidity_max_percent IS NULL OR humidity_max_percent BETWEEN 0 AND 100", name=op.f("ck_cooling_unit_humidity_max_range")),
        sa.CheckConstraint(
            "humidity_min_percent IS NULL OR humidity_max_percent IS NULL OR humidity_min_percent < humidity_max_percent",
            name=op.f("ck_cooling_unit_humidity_limits_ordered"),
        ),
        sa.CheckConstraint("supply_direction_deg IS NULL OR supply_direction_deg BETWEEN 0 AND 359", name=op.f("ck_cooling_unit_supply_direction_range")),
        sa.CheckConstraint("char_length(btrim(name)) > 0", name=op.f("ck_cooling_unit_name_not_blank")),
    )
    op.create_index("ix_cooling_unit_site", "cooling_unit", ["site_id"])
    op.create_index("ix_cooling_unit_group", "cooling_unit", ["cooling_group_id"])

    # --- environmental_sensor
    op.create_table(
        "environmental_sensor",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("asset_type", sa.String(length=32), sa.Computed("'sensor'", persisted=True), nullable=False),
        sa.Column("site_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("sensor_kind", sa.String(length=32), nullable=False),
        sa.Column("measurement_role", sa.String(length=16), server_default="ambient", nullable=False),
        sa.Column("elevation_mm", sa.Integer(), nullable=True),
        sa.Column("flow_direction_deg", sa.SmallInteger(), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_environmental_sensor")),
        sa.ForeignKeyConstraint(["site_id"], ["site.id"], name=op.f("fk_environmental_sensor_site_id_site"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["id", "asset_type"], ["managed_asset.id", "managed_asset.asset_type"], name="fk_environmental_sensor_managed_asset_type", ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "sensor_kind IN ('temperature', 'humidity', 'airflow', 'differential_pressure', 'combined')", name=op.f("ck_environmental_sensor_sensor_kind_allowed")
        ),
        sa.CheckConstraint(
            "measurement_role IN ('ambient', 'rack_inlet', 'rack_exhaust', 'supply_air', 'return_air', 'other')",
            name=op.f("ck_environmental_sensor_measurement_role_allowed"),
        ),
        sa.CheckConstraint("char_length(btrim(name)) > 0", name=op.f("ck_environmental_sensor_name_not_blank")),
        sa.CheckConstraint(
            "flow_direction_deg IS NULL OR (sensor_kind IN ('airflow', 'combined') AND flow_direction_deg BETWEEN 0 AND 359)",
            name=op.f("ck_environmental_sensor_flow_direction_valid"),
        ),
        sa.CheckConstraint("elevation_mm IS NULL OR elevation_mm >= 0", name=op.f("ck_environmental_sensor_elevation_nonnegative")),
    )
    op.create_index("ix_environmental_sensor_site", "environmental_sensor", ["site_id"])

    # --- thermal_zone / containment_element
    op.create_table(
        "thermal_zone",
        _uuid_pk(),
        sa.Column("room_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("zone_kind", sa.String(length=16), nullable=False),
        sa.Column("containment", sa.String(length=16), server_default="none", nullable=False),
        sa.Column("geometry_type", sa.String(length=16), nullable=True),
        sa.Column("x_mm", sa.Integer(), nullable=True),
        sa.Column("y_mm", sa.Integer(), nullable=True),
        sa.Column("width_mm", sa.Integer(), nullable=True),
        sa.Column("height_mm", sa.Integer(), nullable=True),
        sa.Column("points", postgresql.JSONB(), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("retired", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        *_timestamps(),
        sa.ForeignKeyConstraint(["room_id"], ["room.id"], name=op.f("fk_thermal_zone_room_id_room"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_thermal_zone")),
        sa.UniqueConstraint("room_id", "name", name="uq_thermal_zone_room_name"),
        sa.CheckConstraint(
            "zone_kind IN ('served_zone', 'supply_region', 'return_region', 'hot_aisle', 'cold_aisle')", name=op.f("ck_thermal_zone_zone_kind_allowed")
        ),
        sa.CheckConstraint("containment IN ('none', 'contained')", name=op.f("ck_thermal_zone_containment_allowed")),
        sa.CheckConstraint("geometry_type IS NULL OR geometry_type IN ('rect', 'polygon')", name=op.f("ck_thermal_zone_geometry_type_allowed")),
        sa.CheckConstraint("char_length(btrim(name)) > 0", name=op.f("ck_thermal_zone_name_not_blank")),
        sa.CheckConstraint("containment = 'none' OR zone_kind IN ('hot_aisle', 'cold_aisle')", name=op.f("ck_thermal_zone_containment_only_on_aisles")),
        sa.CheckConstraint("geometry_type IS NOT NULL OR zone_kind = 'served_zone'", name=op.f("ck_thermal_zone_geometry_required_except_served_zone")),
        sa.CheckConstraint(
            "geometry_type IS NULL OR (geometry_type = 'rect' AND x_mm IS NOT NULL AND y_mm IS NOT NULL AND width_mm > 0 AND height_mm > 0 "
            "AND points IS NULL) OR (geometry_type = 'polygon' AND points IS NOT NULL AND x_mm IS NULL AND y_mm IS NULL "
            "AND width_mm IS NULL AND height_mm IS NULL)",
            name=op.f("ck_thermal_zone_geometry_shape_consistent"),
        ),
    )
    op.create_index("ix_thermal_zone_room", "thermal_zone", ["room_id"])

    op.create_table(
        "containment_element",
        _uuid_pk(),
        sa.Column("thermal_zone_id", sa.Uuid(), nullable=False),
        sa.Column("element_kind", sa.String(length=16), nullable=False),
        sa.Column("x1_mm", sa.Integer(), nullable=False),
        sa.Column("y1_mm", sa.Integer(), nullable=False),
        sa.Column("x2_mm", sa.Integer(), nullable=False),
        sa.Column("y2_mm", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["thermal_zone_id"], ["thermal_zone.id"], name=op.f("fk_containment_element_thermal_zone_id_thermal_zone"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_containment_element")),
        sa.CheckConstraint("element_kind IN ('boundary', 'opening')", name=op.f("ck_containment_element_element_kind_allowed")),
        sa.CheckConstraint("x1_mm <> x2_mm OR y1_mm <> y2_mm", name=op.f("ck_containment_element_segment_not_degenerate")),
    )
    op.create_index("ix_containment_element_zone", "containment_element", ["thermal_zone_id"])

    # --- cooling_unit_zone
    op.create_table(
        "cooling_unit_zone",
        _uuid_pk(),
        sa.Column("cooling_unit_id", sa.Uuid(), nullable=False),
        sa.Column("thermal_zone_id", sa.Uuid(), nullable=False),
        sa.Column("relation_kind", sa.String(length=16), nullable=False),
        sa.Column("semantics", sa.String(length=16), server_default="configured", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["cooling_unit_id"], ["cooling_unit.id"], name=op.f("fk_cooling_unit_zone_cooling_unit_id_cooling_unit"), ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["thermal_zone_id"], ["thermal_zone.id"], name=op.f("fk_cooling_unit_zone_thermal_zone_id_thermal_zone"), ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cooling_unit_zone")),
        sa.UniqueConstraint("cooling_unit_id", "thermal_zone_id", "relation_kind", name="uq_cooling_unit_zone_relation"),
        sa.CheckConstraint("relation_kind IN ('serves', 'supplies', 'returns_from')", name=op.f("ck_cooling_unit_zone_relation_kind_allowed")),
        sa.CheckConstraint("semantics IN ('authoritative', 'configured', 'modelled')", name=op.f("ck_cooling_unit_zone_semantics_allowed")),
    )
    op.create_index("ix_cooling_unit_zone_zone", "cooling_unit_zone", ["thermal_zone_id"])

    # --- triggers: site consistency the CHECK/FK machinery cannot express
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_equipment_placement_cooling_site() RETURNS trigger AS $$
        DECLARE
            asset_site uuid;
            room_site uuid;
        BEGIN
            SELECT site_id INTO asset_site FROM cooling_unit WHERE id = NEW.equipment_id;
            IF asset_site IS NULL THEN
                SELECT site_id INTO asset_site FROM environmental_sensor WHERE id = NEW.equipment_id;
            END IF;
            IF asset_site IS NULL THEN
                RETURN NEW;
            END IF;
            SELECT b.site_id INTO room_site
              FROM room r JOIN floor f ON f.id = r.floor_id JOIN building b ON b.id = f.building_id
             WHERE r.id = NEW.room_id;
            IF room_site IS DISTINCT FROM asset_site THEN
                RAISE EXCEPTION 'cooling unit / environmental sensor % belongs to a different site than room %', NEW.equipment_id, NEW.room_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_equipment_placement_cooling_site BEFORE INSERT OR UPDATE OF room_id, equipment_id ON equipment_placement "
        "FOR EACH ROW EXECUTE FUNCTION fn_equipment_placement_cooling_site()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_cooling_unit_zone_same_site() RETURNS trigger AS $$
        DECLARE
            unit_site uuid;
            zone_site uuid;
            unit_status text;
            zone_retired boolean;
        BEGIN
            -- Share-lock the unit's asset row and the zone row until commit: a concurrent retire (an UPDATE of the
            -- asset's lifecycle_status, or of thermal_zone.retired) must wait for this relationship, and a retire
            -- that committed first is seen here. Without the locks a retire and a new relationship could both pass.
            SELECT lifecycle_status INTO unit_status FROM managed_asset WHERE id = NEW.cooling_unit_id FOR SHARE;
            IF unit_status IN ('decommissioned', 'removed') THEN
                RAISE EXCEPTION 'cooling unit % is retired and cannot gain zone relationships', NEW.cooling_unit_id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT retired INTO zone_retired FROM thermal_zone WHERE id = NEW.thermal_zone_id FOR SHARE;
            IF zone_retired THEN
                RAISE EXCEPTION 'thermal zone % is retired and cannot gain cooling relationships', NEW.thermal_zone_id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT site_id INTO unit_site FROM cooling_unit WHERE id = NEW.cooling_unit_id;
            SELECT b.site_id INTO zone_site
              FROM thermal_zone z JOIN room r ON r.id = z.room_id JOIN floor f ON f.id = r.floor_id JOIN building b ON b.id = f.building_id
             WHERE z.id = NEW.thermal_zone_id;
            IF unit_site IS DISTINCT FROM zone_site THEN
                RAISE EXCEPTION 'cooling unit % and thermal zone % are in different sites', NEW.cooling_unit_id, NEW.thermal_zone_id
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_cooling_unit_zone_same_site BEFORE INSERT OR UPDATE ON cooling_unit_zone "
        "FOR EACH ROW EXECUTE FUNCTION fn_cooling_unit_zone_same_site()"
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION fn_cooling_unit_retire_guard() RETURNS trigger AS $$
        BEGIN
            IF NEW.lifecycle_status IN ('decommissioned', 'removed') AND OLD.lifecycle_status IS DISTINCT FROM NEW.lifecycle_status
               AND EXISTS (SELECT 1 FROM cooling_unit_zone WHERE cooling_unit_id = NEW.id) THEN
                RAISE EXCEPTION 'cooling unit % is still related to thermal zones; remove the relationships before retiring it', NEW.id
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        "CREATE TRIGGER trg_cooling_unit_retire_guard BEFORE UPDATE OF lifecycle_status ON managed_asset "
        "FOR EACH ROW EXECUTE FUNCTION fn_cooling_unit_retire_guard()"
    )

    _seed_permissions()


def _seed_permissions() -> None:
    permission_table = sa.table(
        "permission", sa.column("id", UUID), sa.column("resource", sa.String), sa.column("action", sa.String), sa.column("description", sa.String)
    )
    role_permission_table = sa.table("role_permission", sa.column("role_id", UUID), sa.column("permission_id", UUID))
    role_table = sa.table("role", sa.column("id", UUID), sa.column("name", sa.String))
    permission_ids = {action: _uuid.uuid4() for action in _GRANTS}
    op.bulk_insert(
        permission_table,
        [{"id": permission_ids[a], "resource": "cooling", "action": a, "description": f"{a} on cooling"} for a in _GRANTS],
    )
    connection = op.get_bind()
    names = sorted({name for roles in _GRANTS.values() for name in roles})
    role_ids = dict(connection.execute(sa.select(role_table.c.name, role_table.c.id).where(role_table.c.name.in_(names))).all())
    missing = set(names) - set(role_ids)
    if missing:
        raise RuntimeError(f"cooling RBAC seed expects roles {names}; missing {sorted(missing)}")
    op.bulk_insert(
        role_permission_table,
        [{"role_id": role_ids[name], "permission_id": permission_ids[action]} for action, roles in _GRANTS.items() for name in roles],
    )


def downgrade() -> None:
    bind = op.get_bind()
    blockers = {
        "cooling units": "SELECT count(*) FROM cooling_unit",
        "cooling groups": "SELECT count(*) FROM cooling_group",
        "environmental sensors": "SELECT count(*) FROM environmental_sensor",
        "thermal zones": "SELECT count(*) FROM thermal_zone",
        "cooling assets": "SELECT count(*) FROM managed_asset WHERE asset_type IN ('crac', 'crah', 'chiller')",
        "cooling/environment metric mappings": f"SELECT count(*) FROM integration_metric_mapping WHERE canonical_metric NOT IN {_OLD_METRICS!r}",
        "sensor/cooling coordinates": "SELECT count(*) FROM equipment_placement WHERE x_mm IS NOT NULL OR y_mm IS NOT NULL OR position_calibration_id IS NOT NULL",
    }
    present = [label for label, sql in blockers.items() if bind.execute(sa.text(sql)).scalar_one()]
    if present:
        raise RuntimeError(
            "Refusing to downgrade 0045_cooling_thermal: it would destroy Issue #105 data (" + ", ".join(present) + "). Export or remove it first."
        )
    op.execute("DELETE FROM role_permission WHERE permission_id IN (SELECT id FROM permission WHERE resource = 'cooling')")
    op.execute("DELETE FROM permission WHERE resource = 'cooling'")
    op.execute("DROP TRIGGER IF EXISTS trg_cooling_unit_retire_guard ON managed_asset")
    op.execute("DROP FUNCTION IF EXISTS fn_cooling_unit_retire_guard()")
    op.execute("DROP TRIGGER IF EXISTS trg_cooling_unit_zone_same_site ON cooling_unit_zone")
    op.execute("DROP FUNCTION IF EXISTS fn_cooling_unit_zone_same_site()")
    op.execute("DROP TRIGGER IF EXISTS trg_equipment_placement_cooling_site ON equipment_placement")
    op.execute("DROP FUNCTION IF EXISTS fn_equipment_placement_cooling_site()")
    op.drop_table("cooling_unit_zone")
    op.drop_table("containment_element")
    op.drop_table("thermal_zone")
    op.drop_table("environmental_sensor")
    op.drop_table("cooling_unit")
    op.drop_table("cooling_group")
    op.drop_index("ix_equipment_placement_room_current", table_name="equipment_placement")
    op.drop_constraint(op.f("ck_equipment_placement_position_pair"), "equipment_placement", type_="check")
    op.drop_constraint(op.f("fk_equipment_placement_position_calibration_id_floor_plan_calibration"), "equipment_placement", type_="foreignkey")
    op.drop_column("equipment_placement", "position_calibration_id")
    op.drop_column("equipment_placement", "y_mm")
    op.drop_column("equipment_placement", "x_mm")
    op.drop_constraint(op.f("ck_integration_metric_mapping_canonical_metric_allowed"), "integration_metric_mapping", type_="check")
    op.create_check_constraint(
        op.f("ck_integration_metric_mapping_canonical_metric_allowed"), "integration_metric_mapping", f"canonical_metric IN {_OLD_METRICS!r}"
    )
    op.drop_constraint(op.f("ck_managed_asset_asset_type_allowed"), "managed_asset", type_="check")
    op.create_check_constraint(op.f("ck_managed_asset_asset_type_allowed"), "managed_asset", f"asset_type IN {_OLD_ASSET_TYPES!r}")
