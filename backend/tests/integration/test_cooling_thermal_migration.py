"""Migration 0045 against a populated current-main schema: additive upgrade preserving existing rows, database-level
invariants attacked with raw SQL, refusal to downgrade while Issue #105 data exists, and a clean downgrade / re-upgrade
once it does not. Runs in one rolled-back transaction."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0045_cooling_thermal.py"


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("cooling_thermal_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def scalar(conn, sql, **params):
    return await conn.scalar(text(sql), params)


async def seed_site_room(conn) -> tuple[uuid.UUID, uuid.UUID]:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domain.location.models import Building, City, Country, Floor, Organization, Room, Site

    tag = uuid.uuid4().hex[:8]
    async with AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint") as session:
        org = Organization(name=f"mig-{tag}")
        session.add(org)
        await session.flush()
        country = Country(organization_id=org.id, name="T")
        session.add(country)
        await session.flush()
        city = City(country_id=country.id, name="T")
        session.add(city)
        await session.flush()
        site = Site(city_id=city.id, code=f"S{tag}", name="S")
        session.add(site)
        await session.flush()
        building = Building(site_id=site.id, code="A", name="B")
        session.add(building)
        await session.flush()
        floor = Floor(building_id=building.id, name="F", level_number=1)
        session.add(floor)
        await session.flush()
        room = Room(floor_id=floor.id, code=f"R{tag}", name="R")
        session.add(room)
        await session.flush()
        await session.commit()
        return site.id, room.id


async def asset(conn, asset_type: str, status: str = "active") -> uuid.UUID:
    asset_id = uuid.uuid4()
    await conn.execute(
        text("INSERT INTO managed_asset (id, asset_type, asset_tag, lifecycle_status, external_ids) VALUES (:i, :t, :tag, :s, '{}'::jsonb)"),
        {"i": asset_id, "t": asset_type, "tag": f"T-{asset_id}", "s": status},
    )
    return asset_id


async def must_fail(conn, sql: str, match: str, **params):
    await conn.execute(text("SAVEPOINT attack"))
    with pytest.raises(Exception, match=match):
        await conn.execute(text(sql), params)
    await conn.execute(text("ROLLBACK TO SAVEPOINT attack"))


async def test_populated_upgrade_enforces_invariants_and_refuses_destructive_downgrade(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            site_a, room_a = await seed_site_room(conn)
            site_b, room_b = await seed_site_room(conn)

            # ---- current-main shape, populated: downgrade the empty 0045 objects and add legacy rows
            await conn.run_sync(_run, "downgrade")
            assert await scalar(conn, "SELECT count(*) FROM permission WHERE resource = 'cooling'") == 0
            legacy_sensor = await asset(conn, "sensor")
            legacy_equipment = await asset(conn, "equipment")
            await conn.execute(text("INSERT INTO equipment_placement (id, equipment_id, placement_type, room_id, effective_from, version) VALUES (gen_random_uuid(), :e, 'floor_standing', :r, now(), 1)"), {"e": legacy_equipment, "r": room_a})
            await must_fail(conn, "INSERT INTO managed_asset (id, asset_type, asset_tag, lifecycle_status, external_ids) VALUES (gen_random_uuid(), 'crac', 'pre', 'active', '{}'::jsonb)", "violates check constraint")
            before = tuple((await conn.execute(text("SELECT id, asset_type, lifecycle_status FROM managed_asset WHERE id = :i"), {"i": legacy_sensor})).one())

            # ---- upgrade over the populated schema
            await conn.run_sync(_run, "upgrade")
            assert tuple((await conn.execute(text("SELECT id, asset_type, lifecycle_status FROM managed_asset WHERE id = :i"), {"i": legacy_sensor})).one()) == before
            placement = (await conn.execute(text("SELECT x_mm, y_mm, position_calibration_id FROM equipment_placement WHERE equipment_id = :e"), {"e": legacy_equipment})).one()
            assert tuple(placement) == (None, None, None), "existing placements gain empty, nullable position columns"
            assert await scalar(conn, "SELECT count(*) FROM environmental_sensor") == 0  # a legacy bare sensor asset needs no backfill
            codes = {tuple(r) for r in (await conn.execute(text("SELECT p.action, r.name FROM permission p JOIN role_permission rp ON rp.permission_id = p.id JOIN role r ON r.id = rp.role_id WHERE p.resource = 'cooling'"))).all()}
            assert codes == {("read", n) for n in ("Administrator", "DCIM Manager", "Engineer", "Operator", "Viewer")} | {("manage", n) for n in ("Administrator", "DCIM Manager", "Engineer")}

            crah, crac, chiller, pdu = await asset(conn, "crah"), await asset(conn, "crac"), await asset(conn, "chiller"), await asset(conn, "pdu")
            sensor = await asset(conn, "sensor")
            group_b = uuid.uuid4()
            await conn.execute(text("INSERT INTO cooling_group (id, site_id, name) VALUES (:i, :s, 'B-group')"), {"i": group_b, "s": site_b})

            # ---- subtype / type integrity (raw SQL cannot bypass it)
            unit_sql = "INSERT INTO cooling_unit (id, unit_kind, site_id, name{extra_cols}) VALUES (:id, :kind, :site, 'u'{extra_vals})"
            def unit(extra_cols="", extra_vals=""):
                return unit_sql.format(extra_cols=extra_cols, extra_vals=extra_vals)

            await must_fail(conn, unit(), "foreign key", id=pdu, kind="crah", site=site_a)  # wrong ManagedAsset type
            await must_fail(conn, unit(), "foreign key", id=crac, kind="crah", site=site_a)  # kind disagrees with asset_type
            await must_fail(conn, unit(), "check constraint", id=crah, kind="boiler", site=site_a)
            await must_fail(conn, unit(", cooling_group_id", ", :g"), "foreign key", id=crah, kind="crah", site=site_a, g=group_b)  # group from another site
            for bad in (
                ", rated_cooling_capacity_kw) VALUES (:id, :kind, :site, 'u', 0)",
                ", rated_cooling_capacity_kw) VALUES (:id, :kind, :site, 'u', -3)",
                ", rated_cooling_capacity_kw, configured_cooling_capacity_kw) VALUES (:id, :kind, :site, 'u', 10, 20)",
                ", humidity_min_percent, humidity_max_percent) VALUES (:id, :kind, :site, 'u', 60, 40)",
                ", humidity_min_percent) VALUES (:id, :kind, :site, 'u', 120)",
                ", airflow_capacity_m3_s) VALUES (:id, :kind, :site, 'u', 0)",
                ", supply_direction_deg) VALUES (:id, :kind, :site, 'u', 360)",
                ", operating_status) VALUES (:id, :kind, :site, 'u', 'melting')",
            ):
                await must_fail(conn, "INSERT INTO cooling_unit (id, unit_kind, site_id, name" + bad, "check constraint", id=crah, kind="crah", site=site_a)
            await conn.execute(text("INSERT INTO cooling_unit (id, unit_kind, site_id, name) VALUES (:i, 'crah', :s, 'ok')"), {"i": crah, "s": site_a})
            assert await scalar(conn, "SELECT rated_cooling_capacity_kw FROM cooling_unit WHERE id = :i", i=crah) is None  # unknown stays NULL

            await must_fail(conn, "INSERT INTO environmental_sensor (id, site_id, name, sensor_kind) VALUES (:i, :s, 'x', 'temperature')", "foreign key", i=pdu, s=site_a)
            await must_fail(conn, "INSERT INTO environmental_sensor (id, site_id, name, sensor_kind) VALUES (:i, :s, 'x', 'thermocouple')", "check constraint", i=sensor, s=site_a)
            await must_fail(conn, "INSERT INTO environmental_sensor (id, site_id, name, sensor_kind, flow_direction_deg) VALUES (:i, :s, 'x', 'temperature', 10)", "check constraint", i=sensor, s=site_a)
            await conn.execute(text("INSERT INTO environmental_sensor (id, site_id, name, sensor_kind) VALUES (:i, :s, 'ok', 'temperature')"), {"i": sensor, "s": site_a})
            assert await scalar(conn, "SELECT asset_type FROM environmental_sensor WHERE id = :i", i=sensor) == "sensor"

            # ---- site consistency of placements and relations (triggers)
            await must_fail(conn, "INSERT INTO equipment_placement (id, equipment_id, placement_type, room_id, effective_from, version) VALUES (gen_random_uuid(), :e, 'floor_standing', :r, now(), 1)", "different site", e=crah, r=room_b)
            await must_fail(conn, "INSERT INTO equipment_placement (id, equipment_id, placement_type, room_id, effective_from, version) VALUES (gen_random_uuid(), :e, 'floor_standing', :r, now(), 1)", "different site", e=sensor, r=room_b)
            await must_fail(conn, "INSERT INTO equipment_placement (id, equipment_id, placement_type, room_id, effective_from, version, x_mm) VALUES (gen_random_uuid(), :e, 'floor_standing', :r, now(), 1, 5)", "position_pair", e=sensor, r=room_a)
            await conn.execute(text("INSERT INTO equipment_placement (id, equipment_id, placement_type, room_id, effective_from, version, x_mm, y_mm) VALUES (gen_random_uuid(), :e, 'floor_standing', :r, now(), 1, 5, 6)"), {"e": sensor, "r": room_a})

            # ---- zones
            zone_a, zone_b = uuid.uuid4(), uuid.uuid4()
            await conn.execute(text("INSERT INTO thermal_zone (id, room_id, name, zone_kind) VALUES (:i, :r, 'whole', 'served_zone')"), {"i": zone_a, "r": room_a})
            await conn.execute(text("INSERT INTO thermal_zone (id, room_id, name, zone_kind) VALUES (:i, :r, 'whole', 'served_zone')"), {"i": zone_b, "r": room_b})
            await must_fail(conn, "INSERT INTO thermal_zone (id, room_id, name, zone_kind) VALUES (gen_random_uuid(), :r, 'whole', 'served_zone')", "duplicate key", r=room_a)
            await must_fail(conn, "INSERT INTO thermal_zone (id, room_id, name, zone_kind) VALUES (gen_random_uuid(), :r, 'aisle', 'hot_aisle')", "geometry_required", r=room_a)
            await must_fail(conn, "INSERT INTO thermal_zone (id, room_id, name, zone_kind, containment) VALUES (gen_random_uuid(), :r, 'sz', 'served_zone', 'contained')", "containment_only_on_aisles", r=room_a)
            await must_fail(conn, "INSERT INTO thermal_zone (id, room_id, name, zone_kind, geometry_type, x_mm, y_mm, width_mm, height_mm) VALUES (gen_random_uuid(), :r, 'neg', 'hot_aisle', 'rect', 0, 0, -5, 10)", "geometry_shape_consistent", r=room_a)
            await must_fail(conn, "INSERT INTO thermal_zone (id, room_id, name, zone_kind, geometry_type, x_mm, y_mm, width_mm, height_mm, points) VALUES (gen_random_uuid(), :r, 'mixed', 'hot_aisle', 'rect', 0, 0, 5, 10, CAST('[[0,0],[1,1],[2,0]]' AS jsonb))", "geometry_shape_consistent", r=room_a)
            await must_fail(conn, "INSERT INTO thermal_zone (id, room_id, name, zone_kind, geometry_type) VALUES (gen_random_uuid(), :r, 'poly', 'hot_aisle', 'polygon')", "geometry_shape_consistent", r=room_a)
            await must_fail(conn, "INSERT INTO containment_element (thermal_zone_id, element_kind, x1_mm, y1_mm, x2_mm, y2_mm) VALUES (:z, 'boundary', 5, 5, 5, 5)", "check constraint", z=zone_a)

            await must_fail(conn, "INSERT INTO cooling_unit_zone (cooling_unit_id, thermal_zone_id, relation_kind) VALUES (:u, :z, 'serves')", "different sites", u=crah, z=zone_b)
            await must_fail(conn, "INSERT INTO cooling_unit_zone (cooling_unit_id, thermal_zone_id, relation_kind, semantics) VALUES (:u, :z, 'serves', 'guessed')", "check constraint", u=crah, z=zone_a)
            await conn.execute(text("INSERT INTO cooling_unit_zone (cooling_unit_id, thermal_zone_id, relation_kind) VALUES (:u, :z, 'serves')"), {"u": crah, "z": zone_a})
            await must_fail(conn, "INSERT INTO cooling_unit_zone (cooling_unit_id, thermal_zone_id, relation_kind) VALUES (:u, :z, 'serves')", "duplicate key", u=crah, z=zone_a)
            await must_fail(conn, "DELETE FROM thermal_zone WHERE id = :z", "foreign key", z=zone_a)  # a referenced zone cannot be deleted
            await must_fail(conn, "DELETE FROM cooling_unit WHERE id = :u", "foreign key", u=crah)
            await must_fail(conn, "UPDATE managed_asset SET lifecycle_status = 'decommissioned' WHERE id = :u", "still related to thermal zones", u=crah)
            await must_fail(conn, "UPDATE managed_asset SET lifecycle_status = 'removed' WHERE id = :u", "still related to thermal zones", u=crah)
            await conn.execute(text("UPDATE managed_asset SET lifecycle_status = 'maintenance' WHERE id = :u"), {"u": crah})  # other transitions are unaffected

            # ---- metric registry check
            integration = uuid.uuid4()
            await conn.execute(text("INSERT INTO integration (id, name, integration_type, target_host, config, poll_interval_seconds, enabled) VALUES (:i, :n, 'snmp', '192.0.2.9', '{}'::jsonb, 60, true)"), {"i": integration, "n": f"i-{integration}"})
            await must_fail(conn, "INSERT INTO integration_metric_mapping (id, integration_id, source_identifier, canonical_metric, unit, scale) VALUES (gen_random_uuid(), :i, 's', 'plasma_flux', 'x', 1)", "check constraint", i=integration)
            await conn.execute(text("INSERT INTO integration_metric_mapping (id, integration_id, managed_asset_id, source_identifier, canonical_metric, unit, scale) VALUES (gen_random_uuid(), :i, :a, 's', 'airflow_m3_s', 'm3/s', 1)"), {"i": integration, "a": sensor})

            # ---- downgrade is refused while #105 data exists, and leaves it untouched
            await conn.execute(text("SAVEPOINT d"))
            with pytest.raises(RuntimeError, match="Refusing to downgrade 0045_cooling_thermal"):
                await conn.run_sync(_run, "downgrade")
            await conn.execute(text("ROLLBACK TO SAVEPOINT d"))
            assert await scalar(conn, "SELECT count(*) FROM cooling_unit") == 1 and await scalar(conn, "SELECT count(*) FROM thermal_zone") == 2

            # ---- remove the #105 data deliberately, then a clean downgrade and re-upgrade
            await conn.execute(text("DELETE FROM integration_metric_mapping WHERE canonical_metric = 'airflow_m3_s'"))
            await conn.execute(text("DELETE FROM cooling_unit_zone"))
            await conn.execute(text("DELETE FROM equipment_placement WHERE equipment_id IN (:a, :b)"), {"a": sensor, "b": crah})
            await conn.execute(text("DELETE FROM thermal_zone"))
            await conn.execute(text("DELETE FROM environmental_sensor"))
            await conn.execute(text("DELETE FROM cooling_unit"))
            await conn.execute(text("DELETE FROM cooling_group"))
            await conn.execute(text("DELETE FROM managed_asset WHERE asset_type IN ('crac', 'crah', 'chiller')"))
            await conn.run_sync(_run, "downgrade")
            assert await scalar(conn, "SELECT count(*) FROM information_schema.tables WHERE table_name IN ('cooling_unit', 'thermal_zone', 'environmental_sensor', 'cooling_group', 'cooling_unit_zone', 'containment_element')") == 0
            assert await scalar(conn, "SELECT count(*) FROM permission WHERE resource = 'cooling'") == 0
            assert await scalar(conn, "SELECT count(*) FROM information_schema.columns WHERE table_name = 'equipment_placement' AND column_name IN ('x_mm', 'y_mm', 'position_calibration_id')") == 0
            assert await scalar(conn, "SELECT count(*) FROM managed_asset WHERE id = :i", i=legacy_sensor) == 1, "legacy rows survive the round trip"
            await conn.run_sync(_run, "upgrade")
            assert await scalar(conn, "SELECT count(*) FROM permission WHERE resource = 'cooling'") == 2
            assert await scalar(conn, "SELECT count(*) FROM managed_asset WHERE id = :i", i=legacy_sensor) == 1
        finally:
            await transaction.rollback()
