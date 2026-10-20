"""Issue #128 / G3 migration 0047 on a populated database: untouched history, enforced numeral, guarded rollback."""

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0047_telemetry_source_text.py"


def _run_migration(connection, direction):
    spec = importlib.util.spec_from_file_location("telemetry_source_text", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _seed(conn):
    collector, integration, mapping = (uuid.uuid4() for _ in range(3))
    await conn.execute(text("""
        INSERT INTO collector (id, name, collector_type, status, secret_ciphertext, secret_rotated_at)
        VALUES (:id, :name, 'central', 'active', 'test', now())"""), {"id": collector, "name": f"g3-{collector}"})
    await conn.execute(text("""
        INSERT INTO integration (id, name, integration_type, enabled, target_host, config, poll_interval_seconds,
                                 consecutive_failures, version)
        VALUES (:id, :name, 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)"""), {"id": integration, "name": f"g3-{integration}"})
    await conn.execute(text("""
        INSERT INTO integration_metric_mapping (id, integration_id, source_identifier, canonical_metric, unit, scale, registry_version)
        VALUES (:id, :i, 's', 'power_kw', 'kW', 1, '1')"""), {"id": mapping, "i": integration})
    return collector, integration, mapping


async def _reading(conn, ids, key, text_value=None):
    collector, integration, mapping = ids
    columns = "raw_value_text, " if text_value is not None else ""
    values = ":t, " if text_value is not None else ""
    await conn.execute(text(f"""
        INSERT INTO telemetry_reading (id, collector_id, integration_id, mapping_id, external_identifier, series_key, dedup_key,
            metric, unit, value, raw_value, raw_unit, source_scale, registry_version, {columns}occurred_at, received_at, attributes)
        VALUES (gen_random_uuid(), :c, :i, :m, 'e', 'k', :key, 'power_kw', 'kW', 1.00000001, 1.00000001, 'kW', 1, '1', {values}
                now(), now(), '{{}}'::jsonb)"""), {"c": collector, "i": integration, "m": mapping, "key": key, "t": text_value})


async def test_populated_upgrade_leaves_history_untouched_and_downgrade_refuses_once_text_exists(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.run_sync(_run_migration, "downgrade")
            ids = await _seed(conn)
            await _reading(conn, ids, "legacy")
            before = tuple((await conn.execute(text("SELECT * FROM telemetry_reading WHERE dedup_key = 'legacy'"))).one())
            await conn.run_sync(_run_migration, "upgrade")
            after = tuple((await conn.execute(text("SELECT * FROM telemetry_reading WHERE dedup_key = 'legacy'"))).one())
            assert after[:-1] == before and after[-1] is None  # the one new column is appended and NULL
            assert await conn.scalar(text("SELECT raw_value FROM telemetry_reading WHERE dedup_key = 'legacy'")) == Decimal("1.00000001")  # unchanged
            await _reading(conn, ids, "new", "1.000000005")
            with pytest.raises(RuntimeError, match="Cannot downgrade 0047"):
                await conn.run_sync(_run_migration, "downgrade")
            assert await conn.scalar(text("SELECT raw_value_text FROM telemetry_reading WHERE dedup_key = 'new'")) == "1.000000005"
            await conn.execute(text("UPDATE telemetry_reading SET raw_value_text = NULL"))
            await conn.run_sync(_run_migration, "downgrade")
            assert await conn.scalar(text("SELECT count(*) FROM telemetry_reading")) == 2
            await conn.run_sync(_run_migration, "upgrade")
        finally:
            await transaction.rollback()


async def test_the_numeral_constraint_is_enforced_for_new_rows_even_though_it_was_added_not_valid(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            ids = await _seed(conn)
            assert await conn.scalar(text(
                "SELECT convalidated FROM pg_constraint WHERE conname = 'ck_telemetry_reading_raw_value_text_numeral'")) is False
            for bad in ("abc", "1" * 65, "", "NaN", "1.2.3"):
                with pytest.raises(Exception):  # noqa: B017  (a CHECK violation)
                    async with conn.begin_nested():
                        await _reading(conn, ids, f"bad-{bad[:5]}", bad)
            for good in ("1", "-0.0", "1E+3", "+.5", "0." + "1" * 62):
                async with conn.begin_nested():
                    await _reading(conn, ids, f"good-{good[:6]}", good)
        finally:
            await transaction.rollback()
