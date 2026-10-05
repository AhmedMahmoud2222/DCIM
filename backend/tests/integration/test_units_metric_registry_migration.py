"""Populated upgrade proof for current-main telemetry rows and raw provenance."""

import importlib.util
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0035_units_metric_registry.py"


def _run_migration(connection, direction):
    spec = importlib.util.spec_from_file_location("units_metric_registry", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def test_populated_current_main_upgrade_preserves_history_and_adds_honest_provenance(db_engine):
    collector_id, integration_id, mapping_id, reading_id = (uuid.uuid4() for _ in range(4))
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(
                text("""
                    INSERT INTO collector
                        (id, name, collector_type, status, secret_ciphertext, secret_rotated_at)
                    VALUES (:id, :name, 'central', 'active', 'test', now())
                """),
                {"id": collector_id, "name": f"units-migration-{collector_id}"},
            )
            await conn.execute(
                text("""
                    INSERT INTO integration
                        (id, name, integration_type, enabled, target_host, config,
                         poll_interval_seconds, consecutive_failures, version)
                    VALUES (:id, :name, 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)
                """),
                {"id": integration_id, "name": f"units-migration-{integration_id}"},
            )
            await conn.run_sync(_run_migration, "downgrade")
            await conn.execute(
                text("""
                    INSERT INTO integration_metric_mapping
                        (id, integration_id, source_identifier, canonical_metric, unit, scale)
                    VALUES (:id, :integration_id, 'sensor.temp', 'temperature_c', 'celsius', 1)
                """),
                {"id": mapping_id, "integration_id": integration_id},
            )
            await conn.execute(
                text("""
                    INSERT INTO telemetry_reading
                        (id, collector_id, integration_id, mapping_id, external_identifier,
                         series_key, dedup_key, metric, unit, value, occurred_at, received_at, attributes)
                    VALUES (:id, :collector_id, :integration_id, :mapping_id, 'sensor-1',
                            'historical-series', 'historical-dedup', 'temperature_c', 'celsius',
                            25.5, now(), now(), '{"source":"current-main"}'::jsonb)
                """),
                {
                    "id": reading_id,
                    "collector_id": collector_id,
                    "integration_id": integration_id,
                    "mapping_id": mapping_id,
                },
            )
            before = (
                await conn.execute(
                    text("""
                        SELECT id, metric, unit, value, occurred_at, received_at, attributes
                        FROM telemetry_reading WHERE id = :id
                    """),
                    {"id": reading_id},
                )
            ).one()

            await conn.run_sync(_run_migration, "upgrade")

            after = (
                await conn.execute(
                    text("""
                        SELECT id, metric, unit, value, occurred_at, received_at, attributes,
                               raw_value, raw_unit, registry_version
                        FROM telemetry_reading WHERE id = :id
                    """),
                    {"id": reading_id},
                )
            ).one()
            assert tuple(after[:7]) == tuple(before)
            assert tuple(after[7:]) == (None, None, "1")
            assert (
                await conn.scalar(
                    text("SELECT registry_version FROM integration_metric_mapping WHERE id = :id"),
                    {"id": mapping_id},
                )
                == "1"
            )

            new_reading_id = uuid.uuid4()
            await conn.execute(
                text("""
                    INSERT INTO telemetry_reading
                        (id, collector_id, integration_id, mapping_id, external_identifier,
                         series_key, dedup_key, metric, unit, value, raw_value, raw_unit,
                         registry_version, occurred_at, received_at, attributes)
                    VALUES (:id, :collector_id, :integration_id, :mapping_id, 'sensor-2',
                            'new-series', 'new-dedup', 'temperature_c', 'degC', 25, 77, 'degF',
                            '1', now(), now(), '{}'::jsonb)
                """),
                {
                    "id": new_reading_id,
                    "collector_id": collector_id,
                    "integration_id": integration_id,
                    "mapping_id": mapping_id,
                },
            )
            assert tuple(
                (
                    await conn.execute(
                        text("SELECT value, unit, raw_value, raw_unit FROM telemetry_reading WHERE id = :id"),
                        {"id": new_reading_id},
                    )
                ).one()
            ) == (25, "degC", 77, "degF")
        finally:
            await transaction.rollback()
