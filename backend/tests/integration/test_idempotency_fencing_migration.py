"""PostgreSQL proof for migration 0041 (idempotency claim fencing): populated upgrade, downgrade and re-upgrade keep
existing rows, give them generation 1, and the CHECK constraint rejects a non-positive generation."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0041_idempotency_claim_fencing.py"


def _run(connection, direction):
    spec = importlib.util.spec_from_file_location("idempotency_fencing_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


_INSERT = """
    INSERT INTO idempotency_key (id, key, endpoint, request_hash, status, response_status, response_body, expires_at)
    VALUES (:id, :key, 'POST:/migration-test', :hash, :status, :rs, CAST(:rb AS jsonb), now() + interval '1 day')"""


async def _seed(conn):
    done, busy = str(uuid.uuid4()), str(uuid.uuid4())
    await conn.execute(text(_INSERT), {"id": uuid.uuid4(), "key": done, "hash": "h1", "status": "completed", "rs": 201, "rb": '{"ok": true}'})
    await conn.execute(text(_INSERT), {"id": uuid.uuid4(), "key": busy, "hash": "h2", "status": "processing", "rs": None, "rb": None})
    return done, busy


async def _state(conn, key):
    return tuple((await conn.execute(text(
        "SELECT status, response_status, response_body FROM idempotency_key WHERE key = :k"), {"k": key})).one())


async def test_populated_upgrade_downgrade_and_reupgrade_preserve_existing_rows(db_engine):
    async with db_engine.connect() as conn:
        tx = await conn.begin()
        try:
            await conn.run_sync(_run, "downgrade")
            assert await conn.scalar(text(
                "SELECT count(*) FROM information_schema.columns WHERE table_name = 'idempotency_key' AND column_name = 'claim_generation'")) == 0
            done, busy = await _seed(conn)

            await conn.run_sync(_run, "upgrade")
            for key, expected in ((done, ("completed", 201, {"ok": True})), (busy, ("processing", None, None))):
                assert await _state(conn, key) == expected
                assert await conn.scalar(text("SELECT claim_generation FROM idempotency_key WHERE key = :k"), {"k": key}) == 1

            with pytest.raises(IntegrityError):
                async with conn.begin_nested():
                    await conn.execute(text("UPDATE idempotency_key SET claim_generation = 0 WHERE key = :k"), {"k": busy})

            await conn.run_sync(_run, "downgrade")
            assert await _state(conn, done) == ("completed", 201, {"ok": True})
            assert await _state(conn, busy) == ("processing", None, None)
            await conn.run_sync(_run, "upgrade")
            assert await conn.scalar(text("SELECT claim_generation FROM idempotency_key WHERE key = :k"), {"k": busy}) == 1
            assert await _state(conn, done) == ("completed", 201, {"ok": True})
        finally:
            await tx.rollback()
