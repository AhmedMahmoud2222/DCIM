"""PostgreSQL proof for the legacy alarm subject-key migration."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0058_alarm_subject_key_node_metric.py"


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("alarm_subject_key_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def test_populated_upgrade_and_downgrade_preserve_alarm_node_metric_identity(db_engine):
    integration_id, second_integration_id = uuid.uuid4(), uuid.uuid4()
    rule_id, second_rule_id = uuid.uuid4(), uuid.uuid4()
    alarm_id, colon_alarm_id, new_format_alarm_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old_key = f"{integration_id}:node-01:temperature_c"
    colon_key = f"{integration_id}:leaf:01:temperature_c"
    already_new_key = "node-02:power_kw"

    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(
                text("INSERT INTO integration (id, name, integration_type, enabled, target_host, config, poll_interval_seconds, consecutive_failures, version) "
                     "VALUES (:i, 'subject-key-migration', 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1), "
                     "(:i2, 'subject-key-migration-2', 'snmp', true, '192.0.2.2', '{}'::jsonb, 60, 0, 1)"),
                {"i": integration_id, "i2": second_integration_id},
            )
            await conn.execute(
                text("INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, unit, enabled, name) "
                     "VALUES (:r, :i, 'temperature_c', 'threshold_high', 1, 'degC', true, 'subject-key-migration'), "
                     "(:r2, :i2, 'power_kw', 'threshold_high', 1, 'kW', true, 'subject-key-migration-2')"),
                {"r": rule_id, "i": integration_id, "r2": second_rule_id, "i2": second_integration_id},
            )
            await conn.execute(
                text("INSERT INTO alarm (id, rule_id, integration_id, subject_key, status, opened_at, last_value, details) "
                     "VALUES (:a, :r, :i, :key, 'ACTIVE', now(), 1, '{}'::jsonb), "
                     "(:a2, :r, :i, :key2, 'ACTIVE', now(), 1, '{}'::jsonb), "
                     "(:a3, :r2, :i2, :key3, 'ACTIVE', now(), 1, '{}'::jsonb)"),
                {"a": alarm_id, "r": rule_id, "i": integration_id, "key": old_key,
                 "a2": colon_alarm_id, "key2": colon_key, "a3": new_format_alarm_id,
                 "r2": second_rule_id, "i2": second_integration_id, "key3": already_new_key},
            )

            await conn.run_sync(_run, "upgrade")
            keys = dict((await conn.execute(text("SELECT id, subject_key FROM alarm"))).all())
            assert keys[alarm_id] == "node-01:temperature_c"
            assert keys[colon_alarm_id] == "leaf:01:temperature_c"
            assert keys[new_format_alarm_id] == already_new_key

            await conn.run_sync(_run, "downgrade")
            restored_keys = dict((await conn.execute(text("SELECT id, subject_key FROM alarm"))).all())
            assert restored_keys[alarm_id] == old_key
            assert restored_keys[colon_alarm_id] == colon_key
            assert restored_keys[new_format_alarm_id] == f"{second_integration_id}:{already_new_key}"

            long_key = "x" * 476
            await conn.execute(text("UPDATE alarm SET subject_key=:key WHERE id=:a"), {"key": long_key, "a": alarm_id})
            savepoint = await conn.begin_nested()
            with pytest.raises(DBAPIError, match="Cannot downgrade alarm subject keys longer than 475 characters"):
                await conn.run_sync(_run, "downgrade")
            await savepoint.rollback()
            unchanged_key = (await conn.execute(text("SELECT subject_key FROM alarm WHERE id=:a"), {"a": alarm_id})).scalar_one()
            assert unchanged_key == long_key
        finally:
            await tx.rollback()
