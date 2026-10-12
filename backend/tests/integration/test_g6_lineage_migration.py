"""G6 additive migration: populated history stays NULL; downgrade refuses evidence loss."""
import importlib.util
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / 'migrations/versions/0051_aggregate_lineage.py'


def run(connection, direction):
    spec = importlib.util.spec_from_file_location('aggregate_lineage', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    context = MigrationContext.configure(connection)
    with Operations.context(context), context.begin_transaction():
        getattr(module, direction)()


async def test_populated_upgrade_preserves_unknown_history_and_reruns_safely(db_engine):
    async with db_engine.connect() as conn:
        await conn.run_sync(run, 'downgrade')
        await conn.commit()
        integration = uuid.uuid4()
        await conn.execute(text("""INSERT INTO integration
            (id,name,integration_type,enabled,target_host,config,poll_interval_seconds,consecutive_failures,version)
            VALUES (:id,:name,'snmp',true,'192.0.2.1','{}'::jsonb,60,0,1)"""),
                           {'id': integration, 'name': f'g6-{integration}'})
        await conn.execute(text("""INSERT INTO daily_telemetry_aggregate
            (id,integration_id,external_identifier,series_key,metric,unit,day,
             average_value,minimum_value,maximum_value,sample_count)
            VALUES (gen_random_uuid(),:id,'e','g6-migration','power_kw','kW','2025-01-01',2,1,3,3)"""), {'id': integration})
        await conn.commit()
        try:
            await conn.run_sync(run, 'upgrade')
            await conn.commit()
            await conn.run_sync(run, 'upgrade')
            await conn.commit()
            row = (await conn.execute(text("""SELECT evidence_class,lineage_sample_count,first_compacted_at,
                raw_retention_days_at_compaction,average_value,sample_count FROM daily_telemetry_aggregate"""))).one()
            assert tuple(row[:4]) == (None, None, None, None)
            assert tuple(row[4:]) == (2, 3)
            assert await conn.scalar(text('SELECT count(*) FROM daily_telemetry_aggregate_lineage')) == 0
            assert await conn.scalar(text('SELECT resolved_hold_retention_days FROM monitoring_policy WHERE id=1')) == 365
            assert await conn.scalar(text("""SELECT bool_and(convalidated) FROM pg_constraint
                WHERE conrelid='daily_telemetry_aggregate'::regclass AND conname LIKE '%lineage_%'""")) is True
        finally:
            await conn.rollback()
            await conn.run_sync(run, 'upgrade')
            await conn.commit()


async def test_downgrade_refuses_summary_evidence_and_preserves_schema(db_engine):
    async with db_engine.connect() as conn:
        await conn.execute(text("""INSERT INTO integration
            (id,name,integration_type,enabled,target_host,config,poll_interval_seconds,consecutive_failures,version)
            VALUES (:id,:name,'snmp',true,'192.0.2.1','{}'::jsonb,60,0,1)"""),
                           {'id': (integration := uuid.uuid4()), 'name': f'g6-{integration}'})
        await conn.execute(text("""INSERT INTO daily_telemetry_aggregate
            (id,integration_id,external_identifier,series_key,metric,unit,day,
             average_value,minimum_value,maximum_value,sample_count,evidence_class,lineage_sample_count)
            VALUES (gen_random_uuid(),:id,'e','g6-downgrade','power_kw','kW','2025-01-01',2,1,3,3,'mixed',1)"""),
                           {'id': integration})
        with pytest.raises(RuntimeError, match='Cannot downgrade 0051'):
            await conn.run_sync(run, 'downgrade')
        assert await conn.scalar(text("SELECT to_regclass('daily_telemetry_aggregate_lineage') IS NOT NULL")) is True
        await conn.rollback()


class ProbingOp:
    """Inject an observation/failure after metadata DDL commits, before validation."""

    def __init__(self, real_op, probe):
        self.real, self.probe, self.done = real_op, probe, False

    def execute(self, statement, *args, **kwargs):
        if not self.done and "VALIDATE CONSTRAINT" in str(statement):
            self.done = True
            self.probe()
        return self.real.execute(statement, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.real, name)


def run_with_probe(connection, probe):
    spec = importlib.util.spec_from_file_location('aggregate_lineage_probe', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = ProbingOp(module.op, probe)
    context = MigrationContext.configure(connection)
    with Operations.context(context), context.begin_transaction():
        module.upgrade()


async def test_validation_releases_exclusive_metadata_lock_before_scan(db_engine):
    seen = {}

    def observe():
        dsn = os.environ['TEST_ADMIN_DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
        with psycopg.connect(dsn, autocommit=True) as other:
            other.execute("SET lock_timeout = '1s'")
            modes = {row[0] for row in other.execute(
                "SELECT mode FROM pg_locks WHERE relation='daily_telemetry_aggregate'::regclass "
                "AND granted AND pid<>pg_backend_pid()")}
            assert 'AccessExclusiveLock' not in modes
            other.execute('SELECT count(*) FROM daily_telemetry_aggregate').fetchone()
            other.execute('UPDATE daily_telemetry_aggregate SET sample_count=sample_count WHERE false')
            seen['read_and_write'] = True

    async with db_engine.connect() as conn:
        await conn.run_sync(run, 'downgrade')
        await conn.commit()
        try:
            await conn.run_sync(run_with_probe, observe)
            await conn.commit()
            assert seen['read_and_write']
        finally:
            await conn.rollback()
            await conn.run_sync(run, 'upgrade')
            await conn.commit()


async def test_interrupted_upgrade_reruns_after_committed_ddl(db_engine):
    def interrupt():
        raise RuntimeError('interrupted before validation')

    async with db_engine.connect() as conn:
        await conn.run_sync(run, 'downgrade')
        await conn.commit()
        try:
            with pytest.raises(RuntimeError, match='interrupted before validation'):
                await conn.run_sync(run_with_probe, interrupt)
            await conn.rollback()
            assert await conn.scalar(text("SELECT to_regclass('daily_telemetry_aggregate_lineage') IS NOT NULL")) is True
            await conn.commit()
            await conn.run_sync(run, 'upgrade')
            await conn.commit()
            rows = (await conn.execute(text("SELECT conname, convalidated FROM pg_constraint "
                "WHERE conrelid='daily_telemetry_aggregate'::regclass AND conname LIKE '%lineage_%'"))).all()
            assert len(rows) == 2 and all(row.convalidated for row in rows)
        finally:
            await conn.rollback()
            await conn.run_sync(run, 'upgrade')
            await conn.commit()
