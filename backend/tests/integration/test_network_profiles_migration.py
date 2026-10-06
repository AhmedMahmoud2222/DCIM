"""PostgreSQL proof for migration 0036: data-preserving upgrade, guarded downgrade and the
table constraints that keep profiles unambiguous."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0036_network_profiles.py"


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("network_profiles_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _insert_integration(conn, integration_id):
    await conn.execute(
        text("""
            INSERT INTO integration (id, name, integration_type, enabled, target_host, config,
                                     poll_interval_seconds, consecutive_failures, version)
            VALUES (:id, :name, 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)
        """),
        {"id": integration_id, "name": f"profile-migration-{integration_id}"},
    )


async def test_upgrade_preserves_existing_rows_and_downgrade_is_guarded(db_engine):
    integration_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.run_sync(_run, "downgrade")  # empty profile tables -> allowed
            assert await conn.scalar(text("SELECT to_regclass('vendor_profile')")) is None
            assert await conn.scalar(text("SELECT count(*) FROM permission WHERE resource = 'network_profile'")) == 0
            await _insert_integration(conn, integration_id)
            await conn.run_sync(_run, "upgrade")
            row = (await conn.execute(text("SELECT name, device_profile_id FROM integration WHERE id = :i"), {"i": integration_id})).one()
            assert row.device_profile_id is None and row.name.startswith("profile-migration-")
            assert await conn.scalar(text("SELECT count(*) FROM permission WHERE resource = 'network_profile'")) == 2
            managers = {
                r[0] for r in await conn.execute(text("""
                    SELECT r.name FROM role r JOIN role_permission rp ON rp.role_id = r.id
                    JOIN permission p ON p.id = rp.permission_id
                    WHERE p.resource = 'network_profile' AND p.action = 'manage'"""))
            }
            assert managers == {"Administrator", "DCIM Manager"}

            vendor_id = uuid.uuid4()
            await conn.execute(text("""
                INSERT INTO vendor_profile (id, code, name, sys_object_id_prefixes, supported_protocols, discovery_oids, neighbor_discovery)
                VALUES (:id, 'guarded', 'Guarded', '[]', '["snmp"]', '{}', '{}')"""), {"id": vendor_id})
            with pytest.raises(RuntimeError, match="Refusing to drop network profiles"):
                async with conn.begin_nested():
                    await conn.run_sync(_run, "downgrade")
        finally:
            await tx.rollback()


async def test_constraints_reject_ambiguous_or_malformed_rows(db_engine):
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            vendor_id, device_id = uuid.uuid4(), uuid.uuid4()
            insert_vendor = """
                INSERT INTO vendor_profile (id, code, name, sys_object_id_prefixes, supported_protocols, discovery_oids, neighbor_discovery {extra})
                VALUES (:id, :code, 'V', '[]', '[]', '{{}}', '{{}}' {values})"""
            await conn.execute(text(insert_vendor.format(extra="", values="")), {"id": vendor_id, "code": "vendor-one"})
            for code in ("vendor-one", "Bad Code"):
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(text(insert_vendor.format(extra="", values="")), {"id": uuid.uuid4(), "code": code})
            with pytest.raises(IntegrityError):  # retired status needs retired_at
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE vendor_profile SET status = 'retired' WHERE id = :id"), {"id": vendor_id})
            with pytest.raises(IntegrityError):  # prefixes must be a JSON array
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE vendor_profile SET sys_object_id_prefixes = '{}' WHERE id = :id"), {"id": vendor_id})

            insert_device = """
                INSERT INTO device_profile (id, vendor_profile_id, code, name, match_criteria, capabilities, interface_discovery, neighbor_behavior)
                VALUES (:id, :vendor, :code, 'D', '[]', '{}', '{}', '{}')"""
            await conn.execute(text(insert_device), {"id": device_id, "vendor": vendor_id, "code": "dev-one"})
            with pytest.raises(IntegrityError):  # unique (vendor, code)
                async with conn.begin_nested():
                    await conn.execute(text(insert_device), {"id": uuid.uuid4(), "vendor": vendor_id, "code": "dev-one"})
            with pytest.raises(IntegrityError):  # vendor in use cannot be deleted
                async with conn.begin_nested():
                    await conn.execute(text("DELETE FROM vendor_profile WHERE id = :id"), {"id": vendor_id})

            insert_mapping = """
                INSERT INTO profile_metric_mapping (id, vendor_profile_id, device_profile_id, oid, canonical_metric, unit)
                VALUES (:id, :vendor, :device, :oid, :metric, 'degC')"""
            for params in (
                {"vendor": vendor_id, "device": device_id, "oid": "1.3.6.1", "metric": "temperature_c"},  # two owners
                {"vendor": None, "device": None, "oid": "1.3.6.1", "metric": "temperature_c"},  # no owner
            ):
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(text(insert_mapping), {"id": uuid.uuid4(), **params})
            ok = {"vendor": vendor_id, "device": None}
            await conn.execute(text(insert_mapping), {"id": uuid.uuid4(), **ok, "oid": "1.3.6.1.4", "metric": "temperature_c"})
            for oid, metric in (("1.3.6.1.4", "load_percent"), ("1.3.6.1.5", "temperature_c")):  # dup OID, dup metric
                with pytest.raises(IntegrityError):
                    async with conn.begin_nested():
                        await conn.execute(text(insert_mapping), {"id": uuid.uuid4(), **ok, "oid": oid, "metric": metric})
            # The same OID/metric on a *different* owner is legal (device override).
            await conn.execute(text(insert_mapping), {"id": uuid.uuid4(), "vendor": None, "device": device_id,
                                                      "oid": "1.3.6.1.4", "metric": "temperature_c"})
        finally:
            await tx.rollback()
