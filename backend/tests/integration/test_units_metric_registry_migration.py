"""Populated upgrade proof for current-main telemetry rows and raw provenance."""

import importlib.util
import uuid
from pathlib import Path

import pytest
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

            legacy_rule_id, ambiguous_rule_id = uuid.uuid4(), uuid.uuid4()
            await conn.execute(text("""
                INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, name)
                VALUES (:id, :integration_id, 'temperature_c', 'threshold_high', 30, 'legacy temperature')
            """), {"id": legacy_rule_id, "integration_id": integration_id})
            for unit in ("W", "kW"):
                await conn.execute(text("""
                    INSERT INTO integration_metric_mapping
                        (id, integration_id, source_identifier, canonical_metric, unit, scale)
                    VALUES (:id, :integration_id, :source, 'power_kw', :unit, 1)
                """), {"id": uuid.uuid4(), "integration_id": integration_id, "source": unit, "unit": unit})
            await conn.execute(text("""
                INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, name)
                VALUES (:id, :integration_id, 'power_kw', 'threshold_high', 1000, 'ambiguous legacy power')
            """), {"id": ambiguous_rule_id, "integration_id": integration_id})

            await conn.run_sync(_run_migration, "upgrade")

            after = (
                await conn.execute(
                    text("""
                        SELECT id, metric, unit, value, occurred_at, received_at, attributes,
                               raw_value, raw_unit, source_scale, registry_version
                        FROM telemetry_reading WHERE id = :id
                    """),
                    {"id": reading_id},
                )
            ).one()
            assert tuple(after[:7]) == tuple(before)
            assert tuple(after[7:]) == (None, None, None, None)
            assert await conn.scalar(
                text("SELECT registry_version FROM integration_metric_mapping WHERE id = :id"),
                {"id": mapping_id},
            ) is None

            assert tuple((await conn.execute(text(
                "SELECT threshold, unit, registry_version FROM alarm_rule WHERE id = :id"
            ), {"id": legacy_rule_id})).one()) == (30, "celsius", None)
            assert tuple((await conn.execute(text(
                "SELECT threshold, unit, registry_version FROM alarm_rule WHERE id = :id"
            ), {"id": ambiguous_rule_id})).one()) == (1000, None, None)

            new_reading_id = uuid.uuid4()
            await conn.execute(
                text("""
                    INSERT INTO telemetry_reading
                        (id, collector_id, integration_id, mapping_id, external_identifier,
                         series_key, dedup_key, metric, unit, value, raw_value, raw_unit,
                         source_scale, registry_version, occurred_at, received_at, attributes)
                    VALUES (:id, :collector_id, :integration_id, :mapping_id, 'sensor-2',
                            'new-series', 'new-dedup', 'temperature_c', 'degC', 25, 77, 'degF',
                            1, '1', now(), now(), '{}'::jsonb)
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
            with pytest.raises(RuntimeError, match="Cannot downgrade 0035"):
                await conn.run_sync(_run_migration, "downgrade")
            # The failed rollback leaves canonical values AND their provenance
            # intact. It must not produce an old-schema row with lost semantics.
            assert tuple((await conn.execute(text(
                "SELECT value, unit, raw_value, raw_unit, source_scale, registry_version "
                "FROM telemetry_reading WHERE id = :id"
            ), {"id": new_reading_id})).one()) == (25, "degC", 77, "degF", 1, "1")
            await conn.execute(text("DELETE FROM telemetry_reading WHERE id = :id"), {"id": new_reading_id})
            await conn.run_sync(_run_migration, "downgrade")
            assert await conn.scalar(text("SELECT threshold FROM alarm_rule WHERE id = :id"),
                                     {"id": legacy_rule_id}) == 30
            assert tuple((await conn.execute(text("SELECT value, unit FROM telemetry_reading WHERE id = :id"),
                                             {"id": reading_id})).one()) == (25.5, "celsius")
            await conn.run_sync(_run_migration, "upgrade")
            # Removed provenance cannot be reconstructed; no made-up raw evidence.
            assert await conn.scalar(text("SELECT raw_value FROM telemetry_reading WHERE id = :id"),
                                     {"id": reading_id}) is None
        finally:
            await transaction.rollback()


@pytest.mark.parametrize("registry_row", ["mapping", "rule", "reading", "aggregate", "raw_provenance"])
async def test_downgrade_refuses_each_registry_contract_before_dropping_metadata(db_engine, registry_row):
    """A canonical 1.5 kW threshold must never become an old-app 1.5 W threshold."""
    from decimal import Decimal

    from app.application.alarm_service import comparison_for, condition_matches
    from app.domain.alarm.models import AlarmRule
    from app.domain.telemetry.models import TelemetryReading

    rule = AlarmRule(metric="power_kw", rule_type="threshold_high", threshold=Decimal("1.5"),
                     unit="kW", registry_version="1")
    reading = TelemetryReading(metric="power_kw", value=Decimal("1.25"), unit="kW", registry_version="1",
                               raw_value=Decimal("1250"), raw_unit="W", source_scale=Decimal("1"))
    assert not condition_matches(rule.rule_type, rule.threshold, comparison_for(rule, reading)[0])
    # This is precisely the reinterpretation that rollback must prevent.
    assert condition_matches(rule.rule_type, rule.threshold, reading.raw_value)

    collector_id, integration_id, mapping_id, row_id = (uuid.uuid4() for _ in range(4))
    params = {"collector": collector_id, "integration": integration_id, "mapping": mapping_id, "row": row_id,
              "version": "1" if registry_row == "mapping" else None}
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.execute(text("""
                INSERT INTO collector (id, name, collector_type, status, secret_ciphertext, secret_rotated_at)
                VALUES (:collector, 'rollback collector', 'central', 'active', 'test', now())
            """), params)
            await conn.execute(text("""
                INSERT INTO integration (id, name, integration_type, enabled, target_host, config,
                                         poll_interval_seconds, consecutive_failures, version)
                VALUES (:integration, 'rollback integration', 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)
            """), params)
            await conn.execute(text("""
                INSERT INTO integration_metric_mapping
                    (id, integration_id, source_identifier, canonical_metric, unit, scale, registry_version)
                VALUES (:mapping, :integration, 'power', 'power_kw', 'W', 1, :version)
            """), params)
            if registry_row == "rule":
                await conn.execute(text("""
                    INSERT INTO alarm_rule (id, integration_id, metric, rule_type, threshold, name, unit, registry_version)
                    VALUES (:row, :integration, 'power_kw', 'threshold_high', 1.5, 'canonical power', 'kW', '1')
                """), params)
            elif registry_row in ("reading", "raw_provenance"):
                params["version"] = "1" if registry_row == "reading" else None
                await conn.execute(text("""
                    INSERT INTO telemetry_reading
                        (id, collector_id, integration_id, mapping_id, external_identifier, series_key, dedup_key,
                         metric, unit, value, raw_value, raw_unit, source_scale, registry_version,
                         occurred_at, received_at, attributes)
                    VALUES (:row, :collector, :integration, :mapping, 'power', 'canonical-power', 'canonical-power',
                            'power_kw', 'kW', 1.25, 1250, 'W', 1, :version, now(), now(), '{}'::jsonb)
                """), params)
            elif registry_row == "aggregate":
                await conn.execute(text("""
                    INSERT INTO daily_telemetry_aggregate
                        (id, integration_id, external_identifier, series_key, metric, unit, day,
                         average_value, minimum_value, maximum_value, sample_count, registry_version)
                    VALUES (:row, :integration, 'power', 'canonical-power', 'power_kw', 'kW', current_date,
                            1.25, 1.25, 1.25, 1, '1')
                """), params)
            with pytest.raises(RuntimeError, match="Cannot downgrade 0035"):
                await conn.run_sync(_run_migration, "downgrade")
            # All eight columns survive refusal, rather than partially dropping
            # the first table's metadata before discovering another live contract.
            assert await conn.scalar(text("""
                SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema()
                AND ((table_name = 'alarm_rule' AND column_name IN ('unit', 'registry_version'))
                    OR (table_name = 'integration_metric_mapping' AND column_name = 'registry_version')
                    OR (table_name = 'daily_telemetry_aggregate' AND column_name = 'registry_version')
                    OR (table_name = 'telemetry_reading'
                        AND column_name IN ('registry_version', 'raw_value', 'raw_unit', 'source_scale')))
            """)) == 8
            assert tuple((await conn.execute(text(
                "SELECT unit, scale, registry_version FROM integration_metric_mapping WHERE id = :mapping"
            ), params)).one()) == ("W", 1, "1" if registry_row == "mapping" else None)
            if registry_row == "rule":
                assert tuple((await conn.execute(text(
                    "SELECT threshold, unit, registry_version FROM alarm_rule WHERE id = :row"
                ), params)).one()) == (Decimal("1.5"), "kW", "1")
        finally:
            await transaction.rollback()
