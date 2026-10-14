"""G8 populated DDL rehearsal on dedicated disposable PostgreSQL only."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from tests.integration.test_unresolved_alarm_rules import seed


def load_migration():
    path = Path(__file__).resolve().parents[2] / "migrations/versions/0050_alarm_rule_resolution.py"
    spec = importlib.util.spec_from_file_location("g8_pg_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_populated_upgrade_preserves_legacy_threshold_and_empty_downgrade(db_session):
    _, _, rule = await seed(db_session)
    connection = await db_session.connection()
    migration = load_migration()

    def rehearse(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            # Dedicated test schema starts upgraded; reversible transactional DDL
            # exercises downgrade then upgrade around a populated legacy rule.
            migration.downgrade()
            migration.upgrade()

    await connection.run_sync(rehearse)
    row = (await db_session.execute(text(
        "SELECT threshold, unit, registry_version, unit_resolved_by, unit_resolved_at, "
        "unit_resolution_reason, blocked_notified_at FROM alarm_rule WHERE id=:id"
    ), {"id": rule.id})).one()
    assert row.threshold == rule.threshold
    assert tuple(row)[1:] == (None,) * 6
    await db_session.rollback()


@pytest.mark.parametrize("column,value", [
    ("blocked_notified_at", datetime.now(UTC)),
    ("unit_resolved_at", datetime.now(UTC)),
    ("unit_resolution_reason", "approved evidence"),
])
async def test_each_populated_evidence_column_refuses_downgrade(db_session, column, value):
    _, _, rule = await seed(db_session)
    await db_session.execute(text(f"UPDATE alarm_rule SET {column}=:value WHERE id=:id"), {"value": value, "id": rule.id})
    migration = load_migration()
    connection = await db_session.connection()

    def refuse(sync_connection):
        with Operations.context(MigrationContext.configure(sync_connection)):
            migration.downgrade()

    with pytest.raises(RuntimeError, match="resolution evidence"):
        await connection.run_sync(refuse)
    # RuntimeError is raised before dropping schema: rollback releases the lock.
    await db_session.rollback()
    columns = (await db_session.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name='alarm_rule' "
        "AND column_name IN ('unit_resolved_by','unit_resolved_at','unit_resolution_reason','blocked_notified_at')"
    ))).scalars().all()
    assert len(columns) == 4
