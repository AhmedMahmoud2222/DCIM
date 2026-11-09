"""PostgreSQL proof for the legacy alarm subject-key migration."""

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0058_alarm_subject_key_node_metric.py"


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("alarm_subject_key_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def test_populated_upgrade_and_downgrade_preserve_alarm_node_metric_identity(db_engine):
    integration_id, rule_id, alarm_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old_key = f"{integration_id}:node-01:temperature_c"

    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(
                text("INSERT INTO integration (id, name, integration_type, enabled, target_host, config, poll_interval_seconds, consecutive_failures, version) "
                     "VALUES (:i, 'subject-key-migration', 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)"),
                {"i": integration_id},
            )
            await conn.execute(
                text("INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, unit, enabled, name) "
                     "VALUES (:r, :i, 'temperature_c', 'threshold_high', 1, 'degC', true, 'subject-key-migration')"),
                {"r": rule_id, "i": integration_id},
            )
            await conn.execute(
                text("INSERT INTO alarm (id, rule_id, integration_id, subject_key, status, opened_at, last_value, details) "
                     "VALUES (:a, :r, :i, :key, 'ACTIVE', now(), 1, '{}'::jsonb)"),
                {"a": alarm_id, "r": rule_id, "i": integration_id, "key": old_key},
            )

            await conn.run_sync(_run, "upgrade")
            new_key = (await conn.execute(text("SELECT subject_key FROM alarm WHERE id=:a"), {"a": alarm_id})).scalar_one()
            assert new_key == "node-01:temperature_c"

            await conn.run_sync(_run, "downgrade")
            restored_key = (await conn.execute(text("SELECT subject_key FROM alarm WHERE id=:a"), {"a": alarm_id})).scalar_one()
            assert restored_key == old_key
        finally:
            await tx.rollback()
