"""PostgreSQL proof for migration 0059 (principal-scoped idempotency, issue #337): populated upgrade
backfills collector rows and stamps unattributable legacy rows, widens uniqueness to
(principal, key, endpoint); downgrade deduplicates cross-principal collisions; the cycle is repeatable."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0059_idempotency_principal_scope.py"


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("idempotency_principal_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


_OLD = """INSERT INTO idempotency_key (id, key, endpoint, request_hash, status, expires_at)
    VALUES (gen_random_uuid(), :key, :endpoint, 'h', 'completed', now() + interval '1 day')"""
_NEW = """INSERT INTO idempotency_key (id, principal, key, endpoint, request_hash, status, expires_at, updated_at)
    VALUES (gen_random_uuid(), :p, :key, 'POST:/x', 'h', :status, now() + interval '1 day', now() - make_interval(secs => :age))"""


async def _principal(conn, key):
    return await conn.scalar(text("SELECT principal FROM idempotency_key WHERE key = :k"), {"k": key})


async def test_upgrade_backfills_legacy_rows_and_widens_uniqueness(db_engine):
    collector = uuid.uuid4()
    ckey, ukey, odd = f"{collector}:dedup-1", str(uuid.uuid4()), "not-a-uuid:dedup-2"
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.run_sync(_run, "downgrade")
            assert await conn.scalar(text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'idempotency_key' AND column_name = 'principal'")) == 0
            await conn.execute(text(_OLD), {"key": ckey, "endpoint": "collector_ingest"})
            await conn.execute(text(_OLD), {"key": ukey, "endpoint": "POST:/equipment"})
            await conn.execute(text(_OLD), {"key": odd, "endpoint": "collector_ingest"})

            await conn.run_sync(_run, "upgrade")
            assert await _principal(conn, ckey) == f"collector:{collector}"
            assert await _principal(conn, ukey) == "legacy:unscoped"
            assert await _principal(conn, odd) == "legacy:unscoped"

            # No silent default: callers must always supply a principal.
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text(_OLD), {"key": str(uuid.uuid4()), "endpoint": "POST:/x"})

            # Same (key, endpoint) now coexists for different principals but not within one.
            k = str(uuid.uuid4())
            await conn.execute(text(_NEW), {"p": "user:A", "key": k, "status": "completed", "age": 0})
            await conn.execute(text(_NEW), {"p": "user:B", "key": k, "status": "completed", "age": 0})
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text(_NEW), {"p": "user:A", "key": k, "status": "completed", "age": 0})
        finally:
            await tx.rollback()


async def test_downgrade_dedups_cross_principal_collisions_and_reupgrade_works(db_engine):
    k = str(uuid.uuid4())
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.execute(text(_NEW), {"p": "user:A", "key": k, "status": "processing", "age": 10})
            await conn.execute(text(_NEW), {"p": "user:B", "key": k, "status": "completed", "age": 60})
            await conn.execute(text(_NEW), {"p": "user:C", "key": k, "status": "completed", "age": 5})

            await conn.run_sync(_run, "downgrade")
            # Exactly one survivor: completed beats processing, then most recently updated (C).
            assert await conn.scalar(text("SELECT count(*) FROM idempotency_key WHERE key = :k"), {"k": k}) == 1
            assert await conn.scalar(text("SELECT status FROM idempotency_key WHERE key = :k"), {"k": k}) == "completed"
            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text(_OLD), {"key": k, "endpoint": "POST:/x"})

            await conn.run_sync(_run, "upgrade")
            assert await _principal(conn, k) == "legacy:unscoped"
        finally:
            await tx.rollback()
