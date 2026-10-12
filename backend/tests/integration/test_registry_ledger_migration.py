"""PostgreSQL ledger migration and mutation/rollback gates in an isolated schema."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.application.registry_ledger import validate_registry_rows

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0053_registry_version_snapshot.py"
CONSUMERS = (
    "integration_metric_mapping", "integration_metric_mapping_revision",
    "telemetry_reading", "daily_telemetry_aggregate", "alarm_rule",
)


def run(connection, direction):
    spec = importlib.util.spec_from_file_location("ledger_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def prepare(connection):
    schema = "registry_ledger_test_" + uuid.uuid4().hex
    await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    await connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
    for table in CONSUMERS:
        await connection.execute(text(f"CREATE TABLE {table} (registry_version text)"))
        # Historical v1 and legacy rows must not block rollback or be rewritten.
        await connection.execute(text(f"INSERT INTO {table} VALUES ('1'), (NULL)"))
    await connection.run_sync(run, "upgrade")


async def test_fresh_seeded_and_populated_v1_roundtrip_and_append_only(_admin_engine):
    async with _admin_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await prepare(connection)
            result = await connection.execute(text(
                "SELECT version, canonical_text, content_hash FROM registry_version_snapshot"
            ))
            validate_registry_rows((row[0], row[1], row[2]) for row in result)
            for mutation in (
                "UPDATE registry_version_snapshot SET canonical_text = 'changed' WHERE version = '1'",
                "DELETE FROM registry_version_snapshot WHERE version = '1'",
                "TRUNCATE registry_version_snapshot",
                "INSERT INTO registry_version_snapshot SELECT * FROM registry_version_snapshot WHERE version = '1'",
            ):
                with pytest.raises(DBAPIError):
                    async with connection.begin_nested():
                        await connection.execute(text(mutation))
            await connection.run_sync(run, "downgrade")
            await connection.run_sync(run, "upgrade")
            assert await connection.scalar(text("SELECT count(*) FROM registry_version_snapshot")) == 2
            for table in CONSUMERS:
                assert await connection.scalar(text(f"SELECT count(*) FROM {table}")) == 2
        finally:
            await transaction.rollback()


@pytest.mark.parametrize("table", CONSUMERS)
async def test_populated_v2_guard_locks_consumers_and_preserves_evidence(_admin_engine, table):
    async with _admin_engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await prepare(connection)
            await connection.execute(text(f"INSERT INTO {table} VALUES ('2')"))
            with pytest.raises(RuntimeError, match="persisted registry-v2"):
                await connection.run_sync(run, "downgrade")
            assert await connection.scalar(text("SELECT count(*) FROM registry_version_snapshot")) == 2
            assert await connection.scalar(text(f"SELECT count(*) FROM {table} WHERE registry_version = '2'")) == 1
            locks = {row[0] for row in await connection.execute(text(
                "SELECT c.relname FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                "WHERE l.pid = pg_backend_pid() AND l.mode = 'AccessExclusiveLock' AND l.granted"
            ))}
            assert set(CONSUMERS) | {"registry_version_snapshot"} <= locks
        finally:
            await transaction.rollback()
