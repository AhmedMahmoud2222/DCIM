"""Issue #128 / G4-A migration 0048 on a populated database: untouched history, validated checks, guarded rollback."""

import importlib.util
import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import psycopg
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
        {"i": integration, "key": f"k-{day}", "day": date.fromisoformat(day), "s": total, "b": basis, "c": legacy_count})


async def _columns(conn):
    rows = await conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'daily_telemetry_aggregate'"))
    return {row[0] for row in rows}


def _psycopg_dsn() -> str:
    return os.environ["TEST_ADMIN_DATABASE_URL"].replace("postgresql+asyncpg://", "postgresql://")


class _ProbingOp:
    """Delegates to alembic's `op`, but first runs `probe` just before the first VALIDATE statement executes."""

    def __init__(self, real_op, probe):
        self._real, self._probe, self._done = real_op, probe, False

    def execute(self, statement, *args, **kwargs):
        if not self._done and "VALIDATE CONSTRAINT" in str(statement):
            self._done = True
            self._probe()
        return self._real.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _run_upgrade_with_commit_boundary(connection, probe):
    """Run the upgrade the way migrations/env.py does: inside begin_transaction(), so autocommit_block() really commits."""
    spec = importlib.util.spec_from_file_location("daily_aggregate_exact_sum_probe", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = _ProbingOp(module.op, probe)
    context = MigrationContext.configure(connection)
    with Operations.context(context), context.begin_transaction():
        module.upgrade()


def _observe_from_second_connection(table_pid_holder):
    """Everything a concurrent session sees at the moment the migration is about to scan the table."""
    with psycopg.connect(_psycopg_dsn(), autocommit=True) as other:
        other.execute("SET lock_timeout = '1s'")
        modes = {row[0] for row in other.execute(
            "SELECT mode FROM pg_locks WHERE relation = 'daily_telemetry_aggregate'::regclass AND granted "
            "AND pid <> pg_backend_pid()")}
        read_ok = other.execute("SELECT count(*) FROM daily_telemetry_aggregate").fetchone() is not None
        other.execute("UPDATE daily_telemetry_aggregate SET average_value = average_value WHERE false")
        write_ok = True
        table_pid_holder.update(modes=modes, read_ok=read_ok, write_ok=write_ok)


async def test_validation_runs_after_a_commit_so_no_exclusive_lock_blocks_readers_or_writers(db_engine):
    """Reviewer finding P2 (migration lock): VALIDATE must not run while ACCESS EXCLUSIVE from ADD COLUMN is still held."""
    seen: dict = {}
    async with db_engine.connect() as conn:
        await conn.run_sync(lambda c: _run_migration(c, "downgrade"))
        await conn.commit()
        integration = await _seed_integration(conn)
        await _aggregate(conn, integration, "2025-08-15")
        await conn.commit()
        try:
            await conn.run_sync(lambda c: _run_upgrade_with_commit_boundary(c, lambda: _observe_from_second_connection(seen)))
            await conn.commit()
        finally:
            await conn.rollback()
        assert seen["read_ok"] and seen["write_ok"]
        assert "AccessExclusiveLock" not in seen["modes"], seen["modes"]
        assert NEW_COLUMNS <= await _columns(conn)
        states = [row[0] for row in await conn.execute(text(
            "SELECT convalidated FROM pg_constraint WHERE conrelid = 'daily_telemetry_aggregate'::regclass "
            "AND conname LIKE 'ck_daily_telemetry_aggregate_%'"))]
        assert states == [True] * 4
        await conn.execute(text("DELETE FROM daily_telemetry_aggregate"))
        await conn.execute(text("DELETE FROM integration WHERE id = :i"), {"i": integration})
        await conn.commit()


async def test_an_interrupted_upgrade_can_be_run_again(db_engine):
    """The commit boundary can strand the columns without the revision; a second run must complete, not fail."""
    async with db_engine.connect() as conn:
        await conn.run_sync(lambda c: _run_upgrade_with_commit_boundary(c, lambda: None))  # already applied: a no-op re-run
        await conn.commit()
        assert NEW_COLUMNS <= await _columns(conn)
        count = await conn.scalar(text(
            "SELECT count(*) FROM pg_constraint WHERE conrelid = 'daily_telemetry_aggregate'::regclass "
            "AND conname LIKE 'ck_daily_telemetry_aggregate_%'"))
        assert count == 4


async def test_populated_downgrade_refuses_once_an_exact_sum_exists_and_history_is_untouched(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            integration = await _seed_integration(conn)
            await _aggregate(conn, integration, "2025-08-15")
            legacy = tuple((await conn.execute(text("SELECT * FROM daily_telemetry_aggregate"))).one())
            assert legacy[-3:] == (None, None, None)  # the new columns are NULL on a legacy row
            await _aggregate(conn, integration, "2025-08-16", basis="decimal_exact", total=Decimal("6"))
            with pytest.raises(RuntimeError, match="Cannot downgrade 0048"):
                await conn.run_sync(_run_migration, "downgrade")
            assert NEW_COLUMNS <= await _columns(conn)  # nothing was dropped
            await conn.execute(text(
                "UPDATE daily_telemetry_aggregate SET sum_value = NULL, arithmetic_basis = NULL, legacy_sample_count = NULL"))
            await conn.run_sync(_run_migration, "downgrade")
            assert not NEW_COLUMNS & await _columns(conn)
            assert await conn.scalar(text("SELECT count(*) FROM daily_telemetry_aggregate")) == 2
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
