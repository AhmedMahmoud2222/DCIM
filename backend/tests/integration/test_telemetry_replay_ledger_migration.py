"""Issue #128 / G4-R migration 0049 on a populated database: guarded rollback and a lock rehearsal. Synthetic data only."""

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from app.db.base import Base

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0049_telemetry_replay_ledger.py"
LEDGER_TABLES = {"telemetry_consumed_key", "telemetry_replay_ledger_state"}


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("telemetry_replay_ledger", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # target_metadata carries the naming convention exactly as migrations/env.py does (CI caught a doubled prefix without it).
    context = MigrationContext.configure(connection, opts={"target_metadata": Base.metadata})
    with Operations.context(context):
        getattr(module, direction)()


async def _tables(conn):
    return {row[0] for row in await conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"))}


async def _has_column(conn):
    return bool(await conn.scalar(text(
        "SELECT count(*) FROM information_schema.columns WHERE table_name = 'monitoring_policy' "
        "AND column_name = 'late_replay_horizon_days'")))


async def test_downgrade_refuses_with_tombstones_or_a_horizon_and_otherwise_round_trips(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("INSERT INTO telemetry_consumed_key (key_hash, consumed_day) "
                                    "VALUES (decode('00112233445566778899aabbccddeeff', 'hex'), '2026-01-01')"))
            with pytest.raises(RuntimeError, match="Cannot downgrade 0049"):
                await conn.run_sync(_run, "downgrade")
            assert LEDGER_TABLES <= await _tables(conn)
        finally:
            await transaction.rollback()
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("UPDATE monitoring_policy SET late_replay_horizon_days = 30"))
            with pytest.raises(RuntimeError, match="late_replay_horizon_days is set"):
                await conn.run_sync(_run, "downgrade")
            assert await _has_column(conn)
        finally:
            await transaction.rollback()
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("DELETE FROM telemetry_consumed_key"))
            await conn.execute(text("UPDATE monitoring_policy SET late_replay_horizon_days = NULL"))
            await conn.run_sync(_run, "downgrade")
            assert not LEDGER_TABLES & await _tables(conn) and not await _has_column(conn)
            await conn.run_sync(_run, "upgrade")
            assert LEDGER_TABLES <= await _tables(conn) and await _has_column(conn)
            assert await conn.scalar(text("SELECT count(*) FROM telemetry_replay_ledger_state")) == 1
            assert await conn.scalar(text("SELECT late_replay_horizon_days FROM monitoring_policy")) is None
            with pytest.raises(Exception):  # noqa: B017  (the CHECK: horizon must be >= 1)
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE monitoring_policy SET late_replay_horizon_days = 0"))
        finally:
            await transaction.rollback()


async def test_upgrade_lock_rehearsal_touches_no_large_table_and_leaves_existing_rows_alone(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            before = await conn.scalar(text("SELECT count(*) FROM daily_telemetry_aggregate"))
            await conn.run_sync(_run, "downgrade")
            await conn.run_sync(_run, "upgrade")
            held = {row[0] for row in await conn.execute(text(
                "SELECT c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "WHERE l.pid = pg_backend_pid() AND l.mode = 'AccessExclusiveLock' AND l.granted AND c.relkind = 'r'"))}
            assert held <= LEDGER_TABLES | {"monitoring_policy"}, held
            assert await conn.scalar(text("SELECT count(*) FROM daily_telemetry_aggregate")) == before
            assert await conn.scalar(text("SELECT count(*) FROM monitoring_policy")) == 1   # a one-row table is the only one altered
        finally:
            await transaction.rollback()
