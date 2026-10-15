"""Issue #145: `alembic check` schema-drift validation against a real PostgreSQL.

Runs the full migration chain on a genuinely fresh scratch database (never the shared
`dcim_test` database — same reasoning as test_migration_permission_seed_parity.py), then
proves all four acceptance behaviors at once:

1. `alembic check` passes on a correctly migrated database (no false drift).
2. Populated rows in multiple partitions survive a revision upgrade and repeated
   checks; partition bounds, index definitions/attachments, and constraints are
   unchanged, including empty partitions.
3. Genuine drift IS still detected: unexpected columns, local partition objects, and
   unattached tables make `alembic check` fail.
4. A foreign-schema attached partition cannot hide a same-named unattached table in
   the default schema.
"""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from migrations.env import (
    _missing_or_modified_parent_metadata_constraints,
    _normalize_check_constraint_def,
)

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


async def _audit_partition_snapshot(conn: asyncpg.Connection) -> dict[str, list[tuple]]:
    partitions = await conn.fetch(
        "SELECT parent.oid::text AS parent_oid, parent_ns.nspname AS parent_schema, "
        "parent.relname AS parent_name, child.oid::text AS child_oid, "
        "child_ns.nspname AS child_schema, child.relname AS child_name, "
        "pg_get_expr(child.relpartbound, child.oid) AS bound "
        "FROM pg_inherits i JOIN pg_class parent ON parent.oid = i.inhparent "
        "JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace "
        "JOIN pg_class child ON child.oid = i.inhrelid "
        "JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace "
        "WHERE parent.oid = 'public.audit_log'::regclass ORDER BY 1, 2, 3, 4"
    )
    indexes = await conn.fetch(
        "SELECT table_ns.nspname AS table_schema, tbl.relname AS table_name, "
        "idx.relname AS index_name, pg_get_indexdef(idx.oid) AS definition, "
        "COALESCE((SELECT array_agg(format('%I.%I', parent_ns.nspname, parent_idx.relname) "
        "ORDER BY parent_ns.nspname, parent_idx.relname) FROM pg_inherits ii "
        "JOIN pg_class parent_idx ON parent_idx.oid = ii.inhparent "
        "JOIN pg_namespace parent_ns ON parent_ns.oid = parent_idx.relnamespace "
        "WHERE ii.inhrelid = idx.oid), ARRAY[]::text[]) AS inherited_from "
        "FROM pg_index i JOIN pg_class tbl ON tbl.oid = i.indrelid "
        "JOIN pg_namespace table_ns ON table_ns.oid = tbl.relnamespace "
        "JOIN pg_class idx ON idx.oid = i.indexrelid "
        "WHERE tbl.oid = 'public.audit_log'::regclass OR tbl.oid IN "
        "(SELECT inhrelid FROM pg_inherits WHERE inhparent = 'public.audit_log'::regclass) "
        "ORDER BY 1, 2, 3"
    )
    constraints = await conn.fetch(
        "SELECT ns.nspname AS table_schema, tbl.relname AS table_name, con.conname, "
        "con.contype, pg_get_constraintdef(con.oid) AS definition, con.conparentid "
        "FROM pg_constraint con JOIN pg_class tbl ON tbl.oid = con.conrelid "
        "JOIN pg_namespace ns ON ns.oid = tbl.relnamespace "
        "WHERE tbl.oid = 'public.audit_log'::regclass OR tbl.oid IN "
        "(SELECT inhrelid FROM pg_inherits WHERE inhparent = 'public.audit_log'::regclass) "
        "ORDER BY 1, 2, 3"
    )
    records = await conn.fetch(
        "SELECT to_jsonb(row_data)::text FROM public.audit_log AS row_data "
        "ORDER BY timestamp, audit_id"
    )
    return {
        "partitions": [tuple(row) for row in partitions],
        "indexes": [tuple(row) for row in indexes],
        "constraints": [tuple(row) for row in constraints],
        "records": [tuple(row) for row in records],
    }


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
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    finally:
        await admin.close()


async def test_alembic_check_passes_and_preserves_audit_partitions():
    db_name = f"dcim_drift_{uuid.uuid4().hex[:8]}"
    app_url = _url_for_db(APP_DATABASE_URL, db_name)
    admin_dsn = _asyncpg_dsn(_url_for_db(ADMIN_DATABASE_URL, db_name))
    try:
        await _create_scratch_database(db_name)
        # Start from a real previously migrated base, then populate it before upgrading
        # the final revision. This exercises the existing-database upgrade path rather
        # than only checking an empty database immediately after a fresh install.
        _assert_alembic_ok(
            _run_alembic(app_url, "upgrade", "0048_daily_aggregate_exact_sum"),
            "upgrade",
            "0048_daily_aggregate_exact_sum",
        )
        conn = await asyncpg.connect(admin_dsn)
        try:
            await conn.execute(
                "INSERT INTO audit_log (timestamp, action, entity_type, source, result) "
                "VALUES (now(), 'drift_test.now', 'rack', 'api', 'success')"
            )
            await conn.execute(
                "INSERT INTO audit_log (timestamp, action, entity_type, source, result) "
                "VALUES (date_trunc('month', now()) + interval '1 month' + interval '10 days', "
                "'drift_test.next_month', 'rack', 'api', 'success')"
            )
            await conn.execute(
                "INSERT INTO audit_log (timestamp, action, entity_type, source, result) "
                "VALUES (date_trunc('month', now()) + interval '6 months', "
                "'drift_test.default', 'rack', 'api', 'success')"
            )
            routed = await conn.fetch(
                "SELECT DISTINCT tableoid::regclass::text AS partition FROM public.audit_log"
            )
            assert len(routed) >= 3, f"expected rows in >=3 distinct partitions, got {routed}"
            before_upgrade = await _audit_partition_snapshot(conn)
        finally:
            await conn.close()

        # 1. Upgrade a populated existing DB to head, then check repeatedly.
        _assert_alembic_ok(_run_alembic(app_url, "upgrade", "head"), "upgrade", "head")
        _assert_alembic_ok(_run_alembic(app_url, "check"), "check")
        _assert_alembic_ok(_run_alembic(app_url, "check"), "check")

        conn = await asyncpg.connect(admin_dsn)
        try:
            after_upgrade = await _audit_partition_snapshot(conn)
            assert after_upgrade == before_upgrade, "upgrade/check changed audit partition schema or records"

            # 2. Genuine schema drift must still be detected.
            await conn.execute("ALTER TABLE managed_asset ADD COLUMN intentional_drift text")
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0, "alembic check failed to detect an unexpected column"
            assert "intentional_drift" in result.stderr + result.stdout
            await conn.execute("ALTER TABLE managed_asset DROP COLUMN intentional_drift")

            # 3. A bare-name collision across schemas must not hide default-schema drift.
            await conn.execute("CREATE SCHEMA pr147_probe")
            await conn.execute(
                "CREATE TABLE pr147_probe.audit_log_review_collision PARTITION OF public.audit_log "
                "FOR VALUES FROM ('2099-01-01') TO ('2099-02-01')"
            )
            await conn.execute("CREATE TABLE public.audit_log_review_collision (id uuid)")
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0, "foreign-schema partition name hid a default-schema table"
            assert "audit_log_review_collision" in result.stderr + result.stdout
            await conn.execute("DROP TABLE public.audit_log_review_collision")
            await conn.execute(
                "ALTER TABLE public.audit_log DETACH PARTITION pr147_probe.audit_log_review_collision"
            )
            await conn.execute("DROP SCHEMA pr147_probe CASCADE")

            # 4. Local indexes/constraints on a valid attached partition are not
            #    inherited parent artifacts, so the partition guard must reject them.
            child = next(
                (row for row in before_upgrade["partitions"] if row[5] != "audit_log_default"),
                None,
            )
            assert child is not None
            child_schema, child_name = child[4], child[5]
            qualified_child = await conn.fetchval(
                "SELECT format('%I.%I', $1::text, $2::text)", child_schema, child_name
            )
            await conn.execute(f"CREATE INDEX pr147_local_index ON {qualified_child} (action)")
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0
            assert "pr147_local_index" in result.stderr + result.stdout
            qualified_index = await conn.fetchval(
                "SELECT format('%I.%I', $1::text, $2::text)", child_schema, "pr147_local_index"
            )
            await conn.execute(f"DROP INDEX {qualified_index}")

            await conn.execute(
                f"ALTER TABLE {qualified_child} ADD CONSTRAINT pr147_local_check CHECK (action <> 'blocked')"
            )
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0
            assert "pr147_local_check" in result.stderr + result.stdout
            await conn.execute(f"ALTER TABLE {qualified_child} DROP CONSTRAINT pr147_local_check")

            # 5. Missing declared parent indexes/constraints remain ordinary drift.
            await conn.execute("DROP INDEX public.ix_audit_log_actor")
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0
            assert "ix_audit_log_actor" in result.stderr + result.stdout
            await conn.execute(
                "CREATE INDEX ix_audit_log_actor ON public.audit_log (actor_user_id, timestamp)"
            )

            check_name = await conn.fetchval(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'public.audit_log'::regclass AND conname LIKE '%result_allowed'"
            )
            assert check_name
            drop_check = await conn.fetchval(
                "SELECT format('ALTER TABLE public.audit_log DROP CONSTRAINT %I', $1::text)", check_name
            )
            await conn.execute(drop_check)
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0
            assert "result_allowed" in result.stderr + result.stdout
            add_check = await conn.fetchval(
                "SELECT format('ALTER TABLE public.audit_log ADD CONSTRAINT %I "
                "CHECK (result IN (''success'', ''failure''))', $1::text)",
                check_name,
            )
            await conn.execute(add_check)

            # 5b. Same-name parent constraint with modified expression must be detected as drift.
            await conn.execute(drop_check)
            mod_check = await conn.fetchval(
                "SELECT format('ALTER TABLE public.audit_log ADD CONSTRAINT %I "
                "CHECK (result = ''success'')', $1::text)",
                check_name,
            )
            await conn.execute(mod_check)
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0, "alembic check failed to detect modified CHECK expression"
            assert "result_allowed" in result.stderr + result.stdout or "modified parent constraint" in result.stderr + result.stdout
            await conn.execute(drop_check)
            await conn.execute(add_check)

            # 5c. Parent CHECK validation must run even when parent has zero attached child partitions.
            detached_children = []
            for child_row in before_upgrade["partitions"]:
                child_rel = await conn.fetchval(
                    "SELECT format('%I.%I', $1::text, $2::text)", child_row[4], child_row[5]
                )
                await conn.execute(f"ALTER TABLE public.audit_log DETACH PARTITION {child_rel}")
                detached_children.append((child_rel, child_row[6]))

            try:
                # On empty parent with 0 children, missing parent constraint is still flagged.
                await conn.execute(drop_check)
                result = _run_alembic(app_url, "check")
                assert result.returncode != 0, "alembic check failed to detect missing CHECK on childless parent"
                assert "result_allowed" in result.stderr + result.stdout or "missing parent constraint" in result.stderr + result.stdout
                await conn.execute(add_check)
            finally:
                for child_rel, bound in detached_children:
                    await conn.execute(f"ALTER TABLE public.audit_log ATTACH PARTITION {child_rel} {bound}")

            # 6. A detached former partition is now an ordinary table and must no
            #    longer be excluded by the partition filter.
            await conn.execute(
                "CREATE TABLE public.audit_log_2099_01 PARTITION OF public.audit_log "
                "FOR VALUES FROM ('2099-01-01') TO ('2099-02-01')"
            )
            await conn.execute("ALTER TABLE public.audit_log DETACH PARTITION public.audit_log_2099_01")
            result = _run_alembic(app_url, "check")
            assert result.returncode != 0, "alembic check failed to flag a detached former partition"
            assert "audit_log_2099_01" in result.stderr + result.stdout
            await conn.execute(
                "ALTER TABLE public.audit_log ATTACH PARTITION public.audit_log_2099_01 "
                "FOR VALUES FROM ('2099-01-01') TO ('2099-02-01')"
            )
            await conn.execute("ALTER TABLE public.audit_log DETACH PARTITION public.audit_log_2099_01")
            await conn.execute("DROP TABLE public.audit_log_2099_01")
        finally:
            await conn.close()

        _assert_alembic_ok(_run_alembic(app_url, "check"), "check")
        conn = await asyncpg.connect(admin_dsn)
        try:
            assert await _audit_partition_snapshot(conn) == before_upgrade
        finally:
            await conn.close()
    finally:
        await _drop_scratch_database(db_name)


async def test_normalization_negative_and_probe_isolation():
    db_name = f"dcim_norm_{uuid.uuid4().hex[:8]}"
    app_url = _url_for_db(APP_DATABASE_URL, db_name)
    sync_url = app_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    await _create_scratch_database(db_name)

    engine = None
    try:
        _assert_alembic_ok(_run_alembic(app_url, "upgrade", "head"), "upgrade", "head")

        engine = create_engine(sync_url)
        with engine.connect() as conn:
            # 1. Normalization exception on invalid/malformed SQL expression must fail closed
            with pytest.raises(RuntimeError, match="Failed to normalize CHECK constraint"):
                _normalize_check_constraint_def(
                    conn, "public", "audit_log", "ck_invalid", "non_existent_column > 0"
                )

            # 2. Connection and transaction state remain healthy after a normalization failure
            with conn.begin_nested():
                test_val = conn.execute(text("SELECT 1")).scalar()
                assert test_val == 1

            # 3. Repeated invocations and probe-name collisions do not cause lingering table conflicts
            norm1 = _normalize_check_constraint_def(
                conn, "public", "audit_log", "result_allowed", "result IN ('success', 'failure')"
            )
            norm2 = _normalize_check_constraint_def(
                conn, "public", "audit_log", "result_allowed", "result IN ('success', 'failure')"
            )
            assert norm1 == norm2
            assert "success" in norm1

            # 4. Unexpected parent CHECK constraint is returned in drift list
            conn.execute(
                text(
                    "ALTER TABLE public.audit_log ADD CONSTRAINT ck_unexpected_probe CHECK (source <> 'invalid')"
                )
            )
            conn.commit()

            drift = _missing_or_modified_parent_metadata_constraints(conn, {"audit_log"})
            assert any("unexpected parent constraint public.audit_log.ck_unexpected_probe" in d for d in drift)

            conn.execute(
                text("ALTER TABLE public.audit_log DROP CONSTRAINT ck_unexpected_probe")
            )
            conn.commit()

            # Ensure DB passes validation clean after cleanup
            clean_drift = _missing_or_modified_parent_metadata_constraints(conn, {"audit_log"})
            assert clean_drift == []
    finally:
        if engine is not None:
            engine.dispose()
        await _drop_scratch_database(db_name)
