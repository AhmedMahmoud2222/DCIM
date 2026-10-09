"""Issue #128 / G1 migration 0046 on a populated database: honest seed, untouched history, guarded rollback."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0046_mapping_revisions.py"


def _run_migration(connection, direction):
    spec = importlib.util.spec_from_file_location("mapping_revisions", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _seed(conn):
    collector_id, integration_id, legacy_mapping, versioned_mapping, reading_id = (uuid.uuid4() for _ in range(5))
    await conn.execute(text("""
        INSERT INTO collector (id, name, collector_type, status, secret_ciphertext, secret_rotated_at)
        VALUES (:id, :name, 'central', 'active', 'test', now())
    """), {"id": collector_id, "name": f"rev-migration-{collector_id}"})
    await conn.execute(text("""
        INSERT INTO integration (id, name, integration_type, enabled, target_host, config,
                                 poll_interval_seconds, consecutive_failures, version)
        VALUES (:id, :name, 'snmp', true, '192.0.2.1', '{}'::jsonb, 60, 0, 1)
    """), {"id": integration_id, "name": f"rev-migration-{integration_id}"})
    for mapping_id, source, unit, scale, version in (
        (legacy_mapping, "legacy", "celsius", 10, None), (versioned_mapping, "versioned", "degF", 1, "1"),
    ):
        await conn.execute(text("""
            INSERT INTO integration_metric_mapping
                (id, integration_id, source_identifier, canonical_metric, unit, scale, registry_version)
            VALUES (:id, :integration_id, :source, 'temperature_c', :unit, :scale, :version)
        """), {"id": mapping_id, "integration_id": integration_id, "source": source, "unit": unit, "scale": scale,
               "version": version})
    await conn.execute(text("""
        INSERT INTO telemetry_reading
            (id, collector_id, integration_id, mapping_id, external_identifier, series_key, dedup_key, metric, unit,
             value, raw_value, raw_unit, source_scale, registry_version, occurred_at, received_at, attributes)
        VALUES (:id, :collector_id, :integration_id, :mapping_id, 'sensor-1', 'hist', 'hist', 'temperature_c', 'degC',
                20, 68, 'degF', 1, '1', now() - interval '30 days', now() - interval '30 days', '{}'::jsonb)
    """), {"id": reading_id, "collector_id": collector_id, "integration_id": integration_id, "mapping_id": versioned_mapping})
    return SimpleNamespaceIds(collector_id, integration_id, legacy_mapping, versioned_mapping, reading_id)


class SimpleNamespaceIds:
    def __init__(self, collector, integration, legacy, versioned, reading):
        self.collector, self.integration, self.legacy, self.versioned, self.reading = collector, integration, legacy, versioned, reading


async def test_populated_upgrade_seeds_one_honestly_labelled_revision_per_mapping_and_leaves_history_alone(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.run_sync(_run_migration, "downgrade")
            ids = await _seed(conn)
            before = tuple((await conn.execute(text("SELECT * FROM telemetry_reading WHERE id = :i"), {"i": ids.reading})).one())
            await conn.run_sync(_run_migration, "upgrade")

            revisions = (await conn.execute(text("""
                SELECT mapping_id, revision, provenance, source_unit, source_scale, registry_version, length(conversion_hash),
                       effective_from >= now() - interval '1 minute' AS effective_at_migration_time
                FROM integration_metric_mapping_revision ORDER BY source_identifier
            """))).all()
            assert [(r.revision, r.provenance, r.source_unit, float(r.source_scale), r.registry_version, r[6], r.effective_at_migration_time)
                    for r in revisions] == [
                (1, "backfilled_from_current", "degF", 1.0, "1", 64, True),
                (1, "backfilled_from_current", "celsius", 10.0, None, 64, True),
            ]
            # Every mapping points at its seed revision; nothing about earlier history is claimed.
            assert await conn.scalar(text("""
                SELECT count(*) FROM integration_metric_mapping m
                JOIN integration_metric_mapping_revision r ON r.id = m.current_revision_id AND r.mapping_id = m.id
            """)) == 2
            after = tuple((await conn.execute(text("SELECT * FROM telemetry_reading WHERE id = :i"), {"i": ids.reading})).one())
            assert after[:len(before)] == before  # the two new columns are appended; nothing existing changed
            assert after[len(before):] == (None, None)
            row = (await conn.execute(text(
                "SELECT value, unit, raw_value, raw_unit, mapping_revision_id, contract_evidence FROM telemetry_reading WHERE id = :i"
            ), {"i": ids.reading})).one()
            assert tuple(row) == (20, "degC", 68, "degF", None, None)  # unverified legacy: no revision, no evidence

            # The seed alone is derived data, so rollback is lossless and allowed...
            await conn.run_sync(_run_migration, "downgrade")
            assert await conn.scalar(text("SELECT value FROM telemetry_reading WHERE id = :i"), {"i": ids.reading}) == 20
            await conn.run_sync(_run_migration, "upgrade")
        finally:
            await transaction.rollback()


async def test_downgrade_refuses_once_any_non_seed_evidence_exists(db_engine):
    for evidence in ("authored_revision", "second_revision", "pinned_reading", "hold"):
        async with db_engine.connect() as conn:
            transaction = await conn.begin()
            try:
                await conn.run_sync(_run_migration, "downgrade")
                ids = await _seed(conn)
                await conn.run_sync(_run_migration, "upgrade")
                seed = await conn.scalar(text(
                    "SELECT current_revision_id FROM integration_metric_mapping WHERE id = :i"), {"i": ids.versioned})
                if evidence in {"authored_revision", "second_revision"}:
                    # An authored revision 1 needs a mapping that has none yet; a second revision goes on a seeded one.
                    target = ids.versioned if evidence == "second_revision" else ids.legacy
                    number = 2 if evidence == "second_revision" else 1
                    if evidence == "authored_revision":
                        await conn.execute(text("UPDATE integration_metric_mapping SET current_revision_id = NULL WHERE id = :m"), {"m": target})
                        await conn.execute(text("ALTER TABLE integration_metric_mapping_revision DISABLE TRIGGER trg_mapping_revision_append_only"))
                        await conn.execute(text("DELETE FROM integration_metric_mapping_revision WHERE mapping_id = :m"), {"m": target})
                        await conn.execute(text("ALTER TABLE integration_metric_mapping_revision ENABLE TRIGGER trg_mapping_revision_append_only"))
                    await conn.execute(text("""
                        INSERT INTO integration_metric_mapping_revision
                            (id, mapping_id, integration_id, source_identifier, revision, canonical_metric, source_unit,
                             source_scale, registry_version, conversion_hash, provenance, effective_from, created_at)
                        VALUES (gen_random_uuid(), :m, :i, 'x', :n, 'temperature_c', 'degC', 1, '1', repeat('a', 64),
                                'authored', now(), now())
                    """), {"m": target, "i": ids.integration, "n": number})
                elif evidence == "pinned_reading":
                    await conn.execute(text(
                        "UPDATE telemetry_reading SET mapping_revision_id = :r, contract_evidence = 'pinned' WHERE id = :i"
                    ), {"r": seed, "i": ids.reading})
                else:
                    await conn.execute(text("""
                        INSERT INTO telemetry_contract_hold
                            (id, collector_id, integration_id, source_identifier, external_identifier, dedup_key, occurred_at,
                             value_text, attributes, reason, status, attempts, first_held_at, last_held_at)
                        VALUES (gen_random_uuid(), :c, :i, 'versioned', 'x', 'k', now(), '1', '{}'::jsonb,
                                'MULTIPLE_REVISIONS', 'held', 1, now(), now())
                    """), {"c": ids.collector, "i": ids.integration})
                with pytest.raises(RuntimeError, match="Cannot downgrade 0046"):
                    await conn.run_sync(_run_migration, "downgrade")
            finally:
                await transaction.rollback()


async def test_the_database_enforces_the_reading_evidence_pairing_and_revision_foreign_key(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            ids = await _seed(conn)
            await conn.run_sync(_run_migration, "downgrade")
            await conn.run_sync(_run_migration, "upgrade")
            for statement in (
                "UPDATE telemetry_reading SET contract_evidence = 'pinned' WHERE id = :i",
                "UPDATE telemetry_reading SET contract_evidence = 'made_up', mapping_revision_id = (SELECT current_revision_id FROM integration_metric_mapping WHERE id = :m) WHERE id = :i",
                "UPDATE telemetry_reading SET contract_evidence = 'pinned', mapping_revision_id = gen_random_uuid() WHERE id = :i",
            ):
                async with conn.begin_nested():
                    with pytest.raises(Exception):  # noqa: B017  (check, check and FK violations)
                        await conn.execute(text(statement), {"i": ids.reading, "m": ids.versioned})
        finally:
            await transaction.rollback()
