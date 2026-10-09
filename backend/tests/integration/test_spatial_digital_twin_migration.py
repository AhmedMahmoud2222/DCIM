"""Migration 0044 against a populated current-main schema: additive upgrade that preserves existing spatial and
import rows, DB-level invariants, refusal to downgrade while Issue #104 data exists, and a clean
downgrade / re-upgrade once it does not. Runs in one rolled-back transaction."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0044_spatial_digital_twin.py"


MIGRATION_0045 = Path(__file__).resolve().parents[2] / "migrations/versions/0045_cooling_thermal.py"


def _run_0045_downgrade(connection):
    """0045 (Issue #105) sits on top of 0044 and references floor_plan_calibration, so it must be undone first."""
    spec = importlib.util.spec_from_file_location("cooling_thermal_migration_for_0044", MIGRATION_0045)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        module.downgrade()


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("spatial_digital_twin_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _scalar(conn, sql, **params):
    return await conn.scalar(text(sql), params)


async def _seed_room(conn) -> uuid.UUID:
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
        return room.id


async def test_populated_upgrade_is_additive_enforces_invariants_and_refuses_destructive_downgrade(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            legacy_plan, new_plan = uuid.uuid4(), uuid.uuid4()
            room = await _seed_room(conn)
            second_room = await _seed_room(conn)
            user_id = await conn.scalar(text("SELECT id FROM app_user LIMIT 1")) or uuid.uuid4()
            if not await conn.scalar(text("SELECT 1 FROM app_user WHERE id = :i"), {"i": user_id}):
                await conn.execute(text("INSERT INTO app_user (id, email, full_name, password_hash, is_active) VALUES (:i, :e, 'M', 'x', true)"), {"i": user_id, "e": f"m-{user_id}@example.com"})

            # ---- current-main shape: downgrade the (empty, in this transaction) 0044 objects, then populate
            await conn.run_sync(_run_0045_downgrade)  # empty here: nothing from Issue #105 exists in this transaction
            await conn.run_sync(_run, "downgrade")
            layer, obj, job, cand = (uuid.uuid4() for _ in range(4))
            await conn.execute(text("INSERT INTO floor_plan (id, room_id, revision_number, status, version) VALUES (:i, :r, 1, 'draft', 1)"), {"i": legacy_plan, "r": room})
            await conn.execute(text("INSERT INTO spatial_layer (id, floor_plan_id, name, layer_type, z_order, visible_by_default) VALUES (:i, :f, 'Imported', 'imported', 0, true)"), {"i": layer, "f": legacy_plan})
            await conn.execute(text("INSERT INTO spatial_object (id, spatial_layer_id, object_type, geometry_type, x_mm, y_mm, width_mm, height_mm, rotation_deg, label, source, version) VALUES (:i, :l, 'rack', 'rect', 100, 200, 600, 1000, 0, 'legacy rack', 'imported', 1)"), {"i": obj, "l": layer})
            await conn.execute(text("INSERT INTO floor_plan_import_job (id, floor_plan_id, uploaded_by_user_id, status, original_filename, file_hash, file_size_bytes) VALUES (:i, :f, :u, 'parsed', 'old.svg', :h, 10)"), {"i": job, "f": legacy_plan, "u": user_id, "h": "a" * 64})
            await conn.execute(text("INSERT INTO floor_plan_import_candidate (id, job_id, raw_geometry, suggested_object_type, status) VALUES (:i, :j, CAST(:g AS jsonb), 'rack', 'pending')"), {"i": cand, "j": job, "g": '{"shape_type": "rect", "x": 1, "y": 2, "width": 3, "height": 4}'})
            await conn.execute(text("INSERT INTO floor_plan_import_diagnostics (id, job_id, source_format, parser_name, parser_version, objects_discovered, objects_classified, racks_detected, equipment_detected, unsupported_object_count, warnings, errors, ambiguous_count, rejected_count, confirmed_count) VALUES (gen_random_uuid(), :j, 'svg', 'svg_sanitizer', '1', 1, 1, 1, 0, 0, '[]'::jsonb, '[]'::jsonb, 0, 0, 0)"), {"j": job})
            before = tuple((await conn.execute(text("SELECT id, x_mm, y_mm, width_mm, height_mm, label, source FROM spatial_object WHERE id = :i"), {"i": obj})).one())

            # ---- upgrade over populated data
            await conn.run_sync(_run, "upgrade")
            assert tuple((await conn.execute(text("SELECT id, x_mm, y_mm, width_mm, height_mm, label, source FROM spatial_object WHERE id = :i"), {"i": obj})).one()) == before
            row = (await conn.execute(text("SELECT version, match_status, evidence, correction, correction_history, source_ref, ordinal, status FROM floor_plan_import_candidate WHERE id = :i"), {"i": cand})).one()
            assert tuple(row) == (1, "not_applicable", [], None, [], None, None, "pending")
            assert await _scalar(conn, "SELECT dedup_key FROM floor_plan_import_job WHERE id = :i", i=job) is None
            assert await _scalar(conn, "SELECT current_calibration_id FROM floor_plan WHERE id = :i", i=legacy_plan) is None
            assert await _scalar(conn, "SELECT candidate_count FROM floor_plan_import_diagnostics WHERE job_id = :j", j=job) == 0

            # ---- new invariants hold in the database itself
            await conn.execute(text("SAVEPOINT s"))
            for sql in (
                "INSERT INTO spatial_object (spatial_layer_id, object_type, geometry_type, x_mm, y_mm, rotation_deg, source, version) VALUES (:l, 'spaceship', 'rect', 0, 0, 0, 'imported', 1)",
                "UPDATE floor_plan_import_candidate SET match_status = 'maybe' WHERE id = :c",
                "UPDATE floor_plan_import_candidate SET version = 0 WHERE id = :c",
                "UPDATE floor_plan SET source_format = 'pdf' WHERE id = :f",
            ):
                with pytest.raises(Exception, match="violates check constraint"):
                    await conn.execute(text(sql), {"l": layer, "c": cand, "f": legacy_plan})
                await conn.execute(text("ROLLBACK TO SAVEPOINT s"))
            await conn.execute(text("INSERT INTO spatial_object (spatial_layer_id, object_type, geometry_type, x_mm, y_mm, rotation_deg, source, version) VALUES (:l, 'wall', 'path', 0, 0, 0, 'imported', 1)"), {"l": layer})
            await conn.execute(text("UPDATE floor_plan SET source_format = 'dxf' WHERE id = :f"), {"f": legacy_plan})

            # ---- Issue #104 data: refusal to downgrade while it exists
            await conn.execute(text("INSERT INTO floor_plan (id, room_id, revision_number, status, version) VALUES (:i, :r, 1, 'draft', 1)"), {"i": new_plan, "r": second_room})
            await conn.execute(text("INSERT INTO floor_plan_calibration (floor_plan_id, sequence, method, source_units, mm_per_unit, origin_x, origin_y, y_axis, confidence) VALUES (:f, 1, 'declared_units', 'mm', 1, 0, 0, 'up', 'high')"), {"f": new_plan})
            await conn.execute(text("SAVEPOINT d"))
            with pytest.raises(RuntimeError, match="calibrations"):
                await conn.run_sync(_run, "downgrade")
            await conn.execute(text("ROLLBACK TO SAVEPOINT d"))
            assert await _scalar(conn, "SELECT count(*) FROM floor_plan_calibration") == 1, "refusal left the data untouched"

            # removing the #104 data (the plan carrying it) and the legacy plan's 104-only markers allows the downgrade
            await conn.execute(text("DELETE FROM floor_plan WHERE id = :i"), {"i": new_plan})
            await conn.execute(text("DELETE FROM spatial_object WHERE object_type = 'wall'"))
            await conn.execute(text("UPDATE floor_plan SET source_format = NULL WHERE id = :f"), {"f": legacy_plan})
            await conn.run_sync(_run, "downgrade")
            assert await _scalar(conn, "SELECT count(*) FROM spatial_object WHERE id = :i", i=obj) == 1, "legacy rows survive the round trip"
            assert not await _scalar(conn, "SELECT to_regclass('floor_plan_calibration')")
            await conn.run_sync(_run, "upgrade")
            assert tuple((await conn.execute(text("SELECT id, x_mm, y_mm, width_mm, height_mm, label, source FROM spatial_object WHERE id = :i"), {"i": obj})).one()) == before
            assert await _scalar(conn, "SELECT to_regclass('floor_plan_calibration')")
        finally:
            await transaction.rollback()


async def test_single_alembic_head_and_one_migration_after_0043():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1  # still a single head; 0045 (Issue #105) now sits on top of 0044
    assert script.get_revision("0045_cooling_thermal").down_revision == "0044_spatial_digital_twin"
    assert script.get_revision("0044_spatial_digital_twin").down_revision == "0043_ops_correlation_itsm"
