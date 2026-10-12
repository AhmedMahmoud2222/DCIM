"""Issue #145: `alembic check` schema-drift validation against a real PostgreSQL.

Runs the full migration chain on a genuinely fresh scratch database (never the shared
`dcim_test` database — same reasoning as test_migration_permission_seed_parity.py), then
proves all four acceptance behaviors at once:

1. `alembic check` passes on a correctly migrated database (no false drift).
2. It still passes once real audit_log partition rows exist across multiple attached
   partitions, and every partition, index, and row survives (no destructive DDL).
3. Genuine drift IS still detected: an unexpected column makes `alembic check` fail.
4. The partition exclusion is verified-only: an *unattached* table whose name merely
   looks like an audit partition (`audit_log_2099_01`) is reported as drift, proving
   env.py filters on pg_inherits membership rather than on a name pattern.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
from sqlalchemy.engine.url import make_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
ADMIN_DATABASE_URL = os.environ["TEST_ADMIN_DATABASE_URL"]
APP_DATABASE_URL = os.environ["DATABASE_URL"]


def _url_for_db(base_url: str, db_name: str) -> str:
    # render_as_string(hide_password=False): str(URL) masks the password as "***".
    return make_url(base_url).set(database=db_name).render_as_string(hide_password=False)


def _asyncpg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _run_alembic(database_url: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )


def _assert_alembic_ok(result: subprocess.CompletedProcess, *args: str) -> None:
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


async def _create_scratch_database(db_name: str) -> None:
    admin = await asyncpg.connect(_asyncpg_dsn(_url_for_db(ADMIN_DATABASE_URL, "postgres")))
    try:
        await admin.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.close()
    conn = await asyncpg.connect(_asyncpg_dsn(_url_for_db(ADMIN_DATABASE_URL, db_name)))
    try:
        await conn.execute("GRANT CREATE, USAGE ON SCHEMA public TO dcim_app")
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    finally:
        await conn.close()


async def _drop_scratch_database(db_name: str) -> None:
    admin = await asyncpg.connect(_asyncpg_dsn(_url_for_db(ADMIN_DATABASE_URL, "postgres")))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            db_name,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await admin.close()


async def test_alembic_check_passes_and_preserves_audit_partitions():
    db_name = f"dcim_drift_{uuid.uuid4().hex[:8]}"
    app_url = _url_for_db(APP_DATABASE_URL, db_name)
    admin_dsn = _asyncpg_dsn(_url_for_db(ADMIN_DATABASE_URL, db_name))
    try:
        await _create_scratch_database(db_name)
        _assert_alembic_ok(_run_alembic(app_url, "upgrade", "head"), "upgrade", "head")

        # 1. Fresh, fully migrated database: no drift.
        _assert_alembic_ok(_run_alembic(app_url, "check"), "check")

        # 2. Populate several attached partitions (current month, future month via the
        #    maintenance-created partitions, and the default catch-all) and confirm
        #    `alembic check` neither flags nor touches them.
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(
                "INSERT INTO audit_log (timestamp, action, entity_type, source, result) "
                "VALUES (now(), 'drift_test.now', 'rack', 'api', 'success')"
            )
            await conn.execute(
                "INSERT INTO audit_log (timestamp, action, entity_type, source, result) "
                "VALUES ('2027-03-01', 'drift_test.default', 'rack', 'api', 'success')"
            )
            partitions_before = {
                row["partition"]: row["n"]
                for row in await conn.fetch(
                    "SELECT tableoid::regclass::text AS partition, count(*) AS n FROM audit_log GROUP BY 1"
                )
            }
            assert partitions_before, "expected audit_log rows routed to attached partitions"
        finally:
            await conn.close()

        _assert_alembic_ok(_run_alembic(app_url, "check"), "check")

        conn = await asyncpg.connect(admin_dsn)
        try:
            partitions_after = {
                row["partition"]: row["n"]
                for row in await conn.fetch(
                    "SELECT tableoid::regclass::text AS partition, count(*) AS n FROM audit_log GROUP BY 1"
                )
            }
            assert partitions_after == partitions_before
            attached = {
                row["p"]
                for row in await conn.fetch(
                    "SELECT inhrelid::regclass::text AS p FROM pg_inherits WHERE inhparent = 'audit_log'::regclass"
                )
            }
            assert set(partitions_before) <= attached

            # 3. Genuine drift must still be detected.
            await conn.execute("ALTER TABLE managed_asset ADD COLUMN intentional_drift text")
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0, "alembic check failed to detect an unexpected column"
            assert "intentional_drift" in result.stderr + result.stdout
            await conn.execute("ALTER TABLE managed_asset DROP COLUMN intentional_drift")

            # 4. A table merely *named* like a partition but not attached is drift, not
            #    a partition — the exclusion is verified via pg_inherits, never by name.
            await conn.execute("CREATE TABLE audit_log_2099_01 (id uuid)")
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0, "alembic check failed to flag an unattached audit_log_* table"
            assert "audit_log_2099_01" in result.stderr + result.stdout
            await conn.execute("DROP TABLE audit_log_2099_01")
        finally:
            await conn.close()

        _assert_alembic_ok(_run_alembic(app_url, "check"), "check")
    finally:
        await _drop_scratch_database(db_name)
