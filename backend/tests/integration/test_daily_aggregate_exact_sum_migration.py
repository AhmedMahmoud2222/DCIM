"""Issue #128 / G4-A migration 0048 on a populated database: untouched history, validated checks, guarded rollback."""

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0048_daily_aggregate_exact_sum.py"
NEW_COLUMNS = {"sum_value", "arithmetic_basis", "legacy_sample_count"}


def _run_migration(connection, direction):
    spec = importlib.util.spec_from_file_location("daily_aggregate_exact_sum", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _seed_integration(conn):
    integration = uuid.uuid4()
    await conn.execute(text("""
        INSERT INTO integration (id, name, integration_type, enabled, target_host, config, poll_interval_seconds,
                                 consecutive_failures, version)
        VALUES (:id, :name, 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)"""), {"id": integration, "name": f"g4a-{integration}"})
    return integration


async def _aggregate(conn, integration, day, *, basis=None, total=None, legacy_count=None):
    await conn.execute(text("""
        INSERT INTO daily_telemetry_aggregate (id, integration_id, external_identifier, series_key, metric, unit, day,
            average_value, minimum_value, maximum_value, sample_count, registry_version{extra})
        VALUES (gen_random_uuid(), :i, 'e', :key, 'power_kw', 'kW', :day, 2, 1, 3, 3, '1'{values})""".format(
        extra=", sum_value, arithmetic_basis, legacy_sample_count" if basis else "",
        values=", :s, :b, :c" if basis else "")),
        {"i": integration, "key": f"k-{day}", "day": day, "s": total, "b": basis, "c": legacy_count})


async def _columns(conn):
    rows = await conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'daily_telemetry_aggregate'"))
    return {row[0] for row in rows}


async def test_populated_upgrade_leaves_history_untouched_and_downgrade_refuses_once_an_exact_sum_exists(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.run_sync(_run_migration, "downgrade")
            assert not NEW_COLUMNS & await _columns(conn)
            integration = await _seed_integration(conn)
            await _aggregate(conn, integration, "2025-08-15")
            before = tuple((await conn.execute(text("SELECT * FROM daily_telemetry_aggregate"))).one())
            await conn.run_sync(_run_migration, "upgrade")
            assert NEW_COLUMNS <= await _columns(conn)
            after = tuple((await conn.execute(text("SELECT * FROM daily_telemetry_aggregate"))).one())
            assert after[:len(before)] == before and after[len(before):] == (None, None, None)  # new columns appended, NULL
            # the four CHECKs were added NOT VALID and then validated
            states = [row[0] for row in await conn.execute(text(
                "SELECT convalidated FROM pg_constraint WHERE conrelid = 'daily_telemetry_aggregate'::regclass "
                "AND conname LIKE 'ck_daily_telemetry_aggregate_%'"))]
            assert states == [True] * 4
            await _aggregate(conn, integration, "2025-08-16", basis="decimal_exact", total=Decimal("6"))
            with pytest.raises(RuntimeError, match="Cannot downgrade 0048"):
                await conn.run_sync(_run_migration, "downgrade")
            assert NEW_COLUMNS <= await _columns(conn)  # nothing was dropped
            await conn.execute(text(
                "UPDATE daily_telemetry_aggregate SET sum_value = NULL, arithmetic_basis = NULL, legacy_sample_count = NULL"))
            await conn.run_sync(_run_migration, "downgrade")
            assert not NEW_COLUMNS & await _columns(conn)
            assert await conn.scalar(text("SELECT count(*) FROM daily_telemetry_aggregate")) == 2
            await conn.run_sync(_run_migration, "upgrade")
        finally:
            await transaction.rollback()


async def test_constraints_are_enforced_for_new_rows(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            integration = await _seed_integration(conn)
            for day, kwargs in (
                ("2025-01-01", {"basis": "decimal_exact", "total": Decimal("99")}),                         # above 3 * 3
                ("2025-01-02", {"basis": "legacy_seeded", "total": Decimal("6")}),                          # no legacy count
                ("2025-01-03", {"basis": "legacy_seeded", "total": Decimal("6"), "legacy_count": 4}),       # count > samples
            ):
                with pytest.raises(Exception):  # noqa: B017  (a CHECK violation)
                    async with conn.begin_nested():
                        await _aggregate(conn, integration, day, **kwargs)
            async with conn.begin_nested():
                await _aggregate(conn, integration, "2025-01-04", basis="legacy_seeded", total=Decimal("6"), legacy_count=2)
        finally:
            await transaction.rollback()
