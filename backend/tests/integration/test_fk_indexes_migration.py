"""Migration 0057 (FK indexes): real-PostgreSQL regression tests for collision handling and recovery.

Each scenario runs `alembic` as a subprocess against a genuinely separate scratch database (never the shared
`dcim_test` database; same reasoning as test_alembic_schema_drift.py). Scenario databases are cloned from one
populated 0056 template (`CREATE DATABASE ... TEMPLATE`) so they start in milliseconds instead of replaying the chain.

Covered, per PR #194 review (Codex P1): a valid same-name index with the wrong definition must fail closed instead of
being stamped as done; an unrelated invalid index must never be dropped; a matching interrupted artifact is rebuilt; a
partially completed upgrade is retried to completion; same-name objects in another schema are ignored; downgrade leaves
non-owned same-name objects alone; offline SQL generation is rejected with a clear message.
"""

import asyncio
import importlib.util
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.engine.url import make_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
ADMIN_DATABASE_URL = os.environ["TEST_ADMIN_DATABASE_URL"]
APP_DATABASE_URL = os.environ["DATABASE_URL"]

_spec = importlib.util.spec_from_file_location("m0057", BACKEND_DIR / "migrations" / "versions" / "0057_fk_indexes.py")
_migration = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_migration)
FK_INDEXES: list[tuple[str, str]] = _migration.FK_INDEXES
NAMES = [f"ix_{t}_{c}" for t, c in FK_INDEXES]
FIRST_TABLE, FIRST_COLUMN = FK_INDEXES[0]
FIRST_NAME = NAMES[0]
PREVIOUS = "0056_pdu_vertical_placement"
HEAD = "0057_fk_indexes"
TELEMETRY_ROWS = 5000


def _url_for_db(base_url: str, db_name: str) -> str:
    return make_url(base_url).set(database=db_name).render_as_string(hide_password=False)


def _dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _run(coro):
    return asyncio.run(coro)


async def _admin(db: str, sql: str, *args):
    conn = await asyncpg.connect(_dsn(_url_for_db(ADMIN_DATABASE_URL, db)))
    try:
        if sql.lstrip().upper().startswith(("SELECT", "WITH")):
            return await conn.fetch(sql, *args)
        return await conn.execute(sql, *args)
    finally:
        await conn.close()


def q(db: str, sql: str, *args):
    return _run(_admin(db, sql, *args))


def alembic(db: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": _url_for_db(APP_DATABASE_URL, db)},
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )


def alembic_ok(db: str, *args: str) -> subprocess.CompletedProcess:
    r = alembic(db, *args)
    assert r.returncode == 0, f"alembic {' '.join(args)} failed:\n{r.stdout}\n{r.stderr}"
    return r


async def _create_db(name: str, template: str | None = None) -> None:
    admin = await asyncpg.connect(_dsn(_url_for_db(ADMIN_DATABASE_URL, "postgres")))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else ""))
    finally:
        await admin.close()
    if template is None:
        conn = await asyncpg.connect(_dsn(_url_for_db(ADMIN_DATABASE_URL, name)))
        try:
            await conn.execute("GRANT CREATE, USAGE ON SCHEMA public TO dcim_app")
            for ext in ('"uuid-ossp"', "pgcrypto", "btree_gist"):
                await conn.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        finally:
            await conn.close()


async def _drop_db(name: str) -> None:
    admin = await asyncpg.connect(_dsn(_url_for_db(ADMIN_DATABASE_URL, "postgres")))
    try:
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()", name
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await admin.close()


@pytest.fixture(scope="module")
def template_0056():
    """A populated database at revision 0056, cloned (not rebuilt) for every scenario."""
    name = f"dcim_fk_tpl_{uuid.uuid4().hex[:8]}"
    _run(_create_db(name))
    try:
        alembic_ok(name, "upgrade", PREVIOUS)
        # Populate the table the migration matters most for. FK triggers are bypassed (replica role, set inside
        # _populate_telemetry) because the point is row volume, not the referenced graph; CHECK constraints still apply.
        _run(_populate_telemetry(name))
        yield name
    finally:
        _run(_drop_db(name))


async def _populate_telemetry(db: str) -> None:
    conn = await asyncpg.connect(_dsn(_url_for_db(ADMIN_DATABASE_URL, db)))
    try:
        await conn.execute("SET session_replication_role = replica")
        await conn.execute(
            "INSERT INTO telemetry_reading (id, collector_id, integration_id, mapping_id, external_identifier, dedup_key,"
            " metric, unit, value, occurred_at, received_at, series_key) "
            "SELECT gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), 'x', 'd' || g, 'm', 'W', g,"
            " now(), now(), 's' || g FROM generate_series(1, $1) g",
            TELEMETRY_ROWS,
        )
    finally:
        await conn.close()


@pytest.fixture
def db(template_0056):
    name = f"dcim_fk_{uuid.uuid4().hex[:8]}"
    _run(_create_db(name, template=template_0056))
    try:
        yield name
    finally:
        _run(_drop_db(name))


def version(db: str) -> str:
    return q(db, "SELECT version_num FROM alembic_version")[0]["version_num"]


def index_rows(db: str, names=NAMES, schema: str = "public"):
    rows = q(
        db,
        "SELECT c.relname AS name, c.oid::int AS oid, t.relname AS tbl, a.attname AS col, am.amname AS method, "
        "i.indisvalid AS valid, i.indisready AS ready, i.indisunique AS uniq, i.indpred IS NULL AS nopred, "
        "i.indnatts AS natts, pg_get_indexdef(c.oid) AS def "
        "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid "
        "JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_am am ON am.oid = c.relam "
        "LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0] "
        "WHERE n.nspname = $2 AND c.relname = ANY($1)",
        list(names),
        schema,
    )
    return {r["name"]: r for r in rows}


def assert_all_ours_valid(db: str) -> dict:
    rows = index_rows(db)
    assert sorted(rows) == sorted(NAMES), f"missing: {sorted(set(NAMES) - set(rows))}"
    for (table, column), name in zip(FK_INDEXES, NAMES, strict=True):
        r = rows[name]
        assert (r["tbl"], r["col"], r["method"], r["natts"]) == (table, column, "btree", 1), r["def"]
        assert r["valid"] and r["ready"] and not r["uniq"] and r["nopred"], r["def"]
    return rows


# --- happy paths -------------------------------------------------------------------------------------------------


def test_fresh_empty_database_upgrade_downgrade_roundtrip_and_no_drift():
    name = f"dcim_fk_fresh_{uuid.uuid4().hex[:8]}"
    _run(_create_db(name))
    try:
        alembic_ok(name, "upgrade", "head")
        assert version(name) == HEAD
        assert_all_ours_valid(name)
        alembic_ok(name, "check")
        alembic_ok(name, "downgrade", "-1")
        assert version(name) == PREVIOUS and index_rows(name) == {}
        alembic_ok(name, "upgrade", "head")
        assert_all_ours_valid(name)
        alembic_ok(name, "check")
    finally:
        _run(_drop_db(name))


def test_populated_upgrade_from_0056_keeps_rows_and_index_is_usable(db):
    assert version(db) == PREVIOUS
    assert q(db, "SELECT count(*) AS n FROM telemetry_reading")[0]["n"] == TELEMETRY_ROWS
    alembic_ok(db, "upgrade", "head")
    assert version(db) == HEAD
    assert_all_ours_valid(db)
    assert q(db, "SELECT count(*) AS n FROM telemetry_reading")[0]["n"] == TELEMETRY_ROWS
    alembic_ok(db, "check")
    alembic_ok(db, "downgrade", "-1")
    assert index_rows(db) == {}
    alembic_ok(db, "upgrade", "head")
    assert_all_ours_valid(db)


# --- valid wrong-definition collisions fail closed -------------------------------------------------------------------

WRONG_DEFINITIONS = {
    "wrong_column": f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} (id)",
    "hash_method": f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} USING hash ({FIRST_COLUMN})",
    "partial_predicate": f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN}) WHERE {FIRST_COLUMN} IS NOT NULL",
    "unique": f"CREATE UNIQUE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN})",
    "composite": f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN}, id)",
    "descending": f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN} DESC)",
    "expression": f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ((({FIRST_COLUMN})::text))",
    "other_table": f"CREATE INDEX {FIRST_NAME} ON alarm (id)",
    "exclusion_constraint": (
        f"ALTER TABLE {FIRST_TABLE} ADD CONSTRAINT {FIRST_NAME} EXCLUDE USING btree ({FIRST_COLUMN} WITH =)"
    ),
}


@pytest.mark.parametrize("variant", sorted(WRONG_DEFINITIONS))
def test_valid_wrong_definition_index_fails_closed_and_is_untouched(db, variant):
    q(db, WRONG_DEFINITIONS[variant])
    before = index_rows(db, [FIRST_NAME])[FIRST_NAME]
    r = alembic(db, "upgrade", "head")
    assert r.returncode != 0
    assert FIRST_NAME in r.stderr + r.stdout and "Refusing" in r.stderr + r.stdout
    assert version(db) == PREVIOUS  # not stamped
    after = index_rows(db)
    assert set(after) == {FIRST_NAME}, "plan-first: nothing may be built when any collision exists"
    assert (after[FIRST_NAME]["oid"], after[FIRST_NAME]["def"]) == (before["oid"], before["def"])


# --- unrelated invalid / non-index objects are never dropped --------------------------------------------------------


def _mark_invalid(db: str, name: str, *, ready: bool = True) -> None:
    q(
        db,
        f"UPDATE pg_index SET indisvalid = false, indisready = {str(ready).lower()} "
        "WHERE indexrelid = ('public.' || $1)::regclass",
        name,
    )


@pytest.mark.parametrize("variant", ["other_table", "same_table_wrong_column"])
def test_unrelated_invalid_index_is_not_dropped(db, variant):
    q(db, f"CREATE INDEX {FIRST_NAME} ON {'alarm' if variant == 'other_table' else FIRST_TABLE} (id)")
    _mark_invalid(db, FIRST_NAME)
    before = index_rows(db, [FIRST_NAME])[FIRST_NAME]
    r = alembic(db, "upgrade", "head")
    assert r.returncode != 0 and "Refusing" in r.stderr + r.stdout
    after = index_rows(db, [FIRST_NAME])[FIRST_NAME]
    assert after["oid"] == before["oid"] and after["valid"] is False
    assert version(db) == PREVIOUS


def test_non_index_relation_with_the_index_name_is_not_dropped(db):
    q(db, f"CREATE TABLE {NAMES[3]} (x int)")
    r = alembic(db, "upgrade", "head")
    assert r.returncode != 0 and "Refusing" in r.stderr + r.stdout
    assert q(db, "SELECT to_regclass($1) AS r", f"public.{NAMES[3]}")[0]["r"] is not None
    assert version(db) == PREVIOUS


def test_missing_or_partitioned_target_table_fails_clearly(db):
    q(db, f"ALTER TABLE {FIRST_TABLE} RENAME TO {FIRST_TABLE}_gone")
    r = alembic(db, "upgrade", "head")
    assert r.returncode != 0 and "does not exist" in r.stderr + r.stdout
    assert version(db) == PREVIOUS


# --- matching interrupted artifacts are rebuilt ----------------------------------------------------------------------


@pytest.mark.parametrize("ready", [True, False], ids=["invalid_ready", "invalid_not_ready"])
def test_matching_invalid_artifact_is_dropped_and_rebuilt(db, ready):
    q(db, f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN})")
    _mark_invalid(db, FIRST_NAME, ready=ready)
    old_oid = index_rows(db, [FIRST_NAME])[FIRST_NAME]["oid"]
    alembic_ok(db, "upgrade", "head")
    rows = assert_all_ours_valid(db)
    assert rows[FIRST_NAME]["oid"] != old_oid, "the interrupted artifact must have been rebuilt, not reused"
    assert version(db) == HEAD


def _start_blocked_concurrent_build(db: str):
    """Run CREATE INDEX CONCURRENTLY for FIRST_NAME in another session, stalled behind an open transaction.

    Session A holds a transaction with an xid, so session B's build waits for it after creating the (invalid) index and
    shows up in pg_stat_progress_create_index. Returns a callable that releases A and waits for B to finish.
    """
    import threading

    ready, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    async def main():
        dsn = _dsn(_url_for_db(ADMIN_DATABASE_URL, db))
        a, b = await asyncpg.connect(dsn), await asyncpg.connect(dsn)
        try:
            tx = a.transaction()
            await tx.start()
            await a.execute("SELECT txid_current()")
            build = asyncio.ensure_future(
                b.execute(f"CREATE INDEX CONCURRENTLY {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN})")
            )
            for _ in range(100):
                n = await a.fetchval("SELECT count(*) FROM pg_stat_progress_create_index")
                if n:
                    break
                await asyncio.sleep(0.1)
            ready.set()
            while not release.is_set():
                await asyncio.sleep(0.05)
            await tx.rollback()
            await build
        except BaseException as exc:  # surfaced through the returned callable
            errors.append(exc)
            ready.set()
        finally:
            await a.close()
            await b.close()

    thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    thread.start()
    assert ready.wait(30), "concurrent build never appeared in pg_stat_progress_create_index"

    def stop():
        release.set()
        thread.join(60)
        assert not errors, errors

    return stop


def test_invalid_index_of_an_active_concurrent_build_is_not_dropped_or_rebuilt(db):
    stop = _start_blocked_concurrent_build(db)
    try:
        row = index_rows(db, [FIRST_NAME])[FIRST_NAME]
        assert row["valid"] is False, "precondition: the in-flight build exposes an invalid index"
        r = alembic(db, "upgrade", "head")
        assert r.returncode != 0 and "possibly active" in r.stderr + r.stdout
        assert version(db) == PREVIOUS
        assert index_rows(db, [FIRST_NAME])[FIRST_NAME]["oid"] == row["oid"]
    finally:
        stop()
    assert index_rows(db, [FIRST_NAME])[FIRST_NAME]["valid"] is True, "the other session's build completes untouched"


def test_matching_valid_index_is_reused_not_rebuilt(db):
    q(db, f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN})")
    old_oid = index_rows(db, [FIRST_NAME])[FIRST_NAME]["oid"]
    alembic_ok(db, "upgrade", "head")
    assert assert_all_ours_valid(db)[FIRST_NAME]["oid"] == old_oid


# --- schema resolution -----------------------------------------------------------------------------------------------


def test_same_name_object_in_another_schema_is_ignored_and_left_alone(db):
    q(db, "CREATE SCHEMA other")
    q(db, f"CREATE TABLE other.{FIRST_TABLE} ({FIRST_COLUMN} uuid)")
    q(db, f"CREATE INDEX {FIRST_NAME} ON other.{FIRST_TABLE} ({FIRST_COLUMN})")
    q(db, f"UPDATE pg_index SET indisvalid = false WHERE indexrelid = 'other.{FIRST_NAME}'::regclass")
    before = index_rows(db, [FIRST_NAME], schema="other")[FIRST_NAME]
    alembic_ok(db, "upgrade", "head")
    assert_all_ours_valid(db)
    after = index_rows(db, [FIRST_NAME], schema="other")[FIRST_NAME]
    assert (after["oid"], after["valid"]) == (before["oid"], False)


# --- partially completed upgrade -------------------------------------------------------------------------------------


def test_partially_completed_upgrade_is_retried_to_completion_and_reuses_built_indexes(db):
    # Fail the build part-way: a later table is owned by someone else, so CREATE INDEX there is refused after the
    # earlier indexes were already built and committed (each index is its own autocommit statement).
    failing_table, _ = next(item for item in FK_INDEXES if item[0] == "generator")
    q(db, f"ALTER TABLE {failing_table} OWNER TO postgres")
    r = alembic(db, "upgrade", "head")
    assert r.returncode != 0 and "must be owner" in (r.stderr + r.stdout)
    assert version(db) == PREVIOUS
    partial = index_rows(db)
    failing_pos = [t for t, _ in FK_INDEXES].index(failing_table)
    built = {n for n in NAMES[:failing_pos]}
    assert built and set(partial) == built, "indexes before the failure are kept; later ones are not built"
    assert all(r_["valid"] and r_["ready"] for r_ in partial.values())

    q(db, f"ALTER TABLE {failing_table} OWNER TO dcim_app")
    alembic_ok(db, "upgrade", "head")
    final = assert_all_ours_valid(db)
    assert all(final[n]["oid"] == partial[n]["oid"] for n in built), "already-built valid indexes must be reused"
    assert version(db) == HEAD
    alembic_ok(db, "check")


# --- downgrade and offline ---------------------------------------------------------------------------------------------


def test_downgrade_drops_only_our_indexes_and_leaves_unrelated_same_name_object(db):
    alembic_ok(db, "upgrade", "head")
    victim = NAMES[7]
    victim_table, _ = FK_INDEXES[7]
    q(db, f"DROP INDEX {victim}")
    q(db, f"CREATE INDEX {victim} ON {victim_table} (id)")
    foreign_oid = index_rows(db, [victim])[victim]["oid"]
    alembic_ok(db, "downgrade", "-1")
    assert version(db) == PREVIOUS
    assert set(index_rows(db)) == {victim}
    assert index_rows(db)[victim]["oid"] == foreign_oid


def test_offline_sql_generation_is_rejected_with_a_clear_message(db):
    r = alembic(db, "upgrade", f"{PREVIOUS}:{HEAD}", "--sql")
    assert r.returncode != 0
    assert "offline" in (r.stderr + r.stdout).lower()
