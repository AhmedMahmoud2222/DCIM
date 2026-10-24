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
import contextlib
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


def alembic(db: str, *args: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": _url_for_db(APP_DATABASE_URL, db), **(env_overrides or {})},
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


_INDEX_ROWS_SQL = (
    "SELECT c.relname AS name, c.oid::int AS oid, t.relname AS tbl, a.attname AS col, am.amname AS method, "
    "i.indisvalid AS valid, i.indisready AS ready, i.indisunique AS uniq, i.indpred IS NULL AS nopred, "
    "i.indnatts AS natts, pg_get_indexdef(c.oid) AS def "
    "FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid "
    "JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_am am ON am.oid = c.relam "
    "LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = i.indkey[0] "
    "WHERE n.nspname = $2 AND c.relname = ANY($1)"
)


async def aindex_rows(db: str, names=NAMES, schema: str = "public"):
    rows = await _admin(db, _INDEX_ROWS_SQL, list(names), schema)
    return {r["name"]: r for r in rows}


def index_rows(db: str, names=NAMES, schema: str = "public"):
    return _run(aindex_rows(db, names, schema))


def assert_all_ours_valid(db: str) -> dict:
    rows = index_rows(db)
    assert sorted(rows) == sorted(NAMES), f"missing: {sorted(set(NAMES) - set(rows))}"
    for (table, column), name in zip(FK_INDEXES, NAMES, strict=True):
        r = rows[name]
        assert (r["tbl"], r["col"], r["method"], r["natts"]) == (table, column, "btree", 1), r["def"]
        assert r["valid"] and r["ready"] and not r["uniq"] and r["nopred"], r["def"]
    return rows


def test_fk_index_migration_and_orm_metadata_are_exactly_in_parity():
    import importlib

    from app.db.base import Base

    importlib.import_module("app.db.models")

    assert len(FK_INDEXES) == 22
    for (table_name, column_name), index_name in zip(FK_INDEXES, NAMES, strict=True):
        table = Base.metadata.tables[table_name]
        index = next((item for item in table.indexes if item.name == index_name), None)
        assert index is not None, f"{index_name} missing from ORM metadata"
        assert [column.name for column in index.columns] == [column_name]
        assert not index.unique


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
        a, b = await asyncpg.connect(dsn), await asyncpg.connect(dsn, command_timeout=50)
        build = None
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
            else:
                raise AssertionError("concurrent build did not enter pg_stat_progress_create_index")
            ready.set()
            try:
                await asyncio.wait_for(asyncio.to_thread(release.wait), timeout=45)
            except TimeoutError as exc:
                raise AssertionError("test did not release the blocked build within 45 seconds") from exc
            await tx.rollback()
            await build
        except BaseException as exc:  # surfaced through the returned callable
            errors.append(exc)
            ready.set()
        finally:
            if not release.is_set():
                release.set()
            if build is not None and not build.done():
                build.cancel()
                await asyncio.gather(build, return_exceptions=True)
            await a.close()
            await b.close()

    thread = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
    thread.start()
    if not ready.wait(30):
        release.set()
        thread.join(60)
        raise AssertionError("concurrent build never appeared in pg_stat_progress_create_index")

    def stop():
        release.set()
        thread.join(60)
        assert not thread.is_alive(), "blocked build worker did not stop within 60 seconds"
        assert not errors, errors

    return stop


def test_invalid_index_of_an_active_concurrent_build_is_not_dropped_or_rebuilt(db):
    stop = _start_blocked_concurrent_build(db)
    try:
        row = index_rows(db, [FIRST_NAME])[FIRST_NAME]
        assert row["valid"] is False, "precondition: the in-flight build exposes an invalid index"
        r = alembic(
            db,
            "upgrade",
            "head",
            env_overrides={"PGOPTIONS": "-c lock_timeout=3s -c statement_timeout=8s"},
        )
        assert r.returncode != 0 and "possibly active" in r.stderr + r.stdout
        assert version(db) == PREVIOUS
        assert index_rows(db, [FIRST_NAME])[FIRST_NAME]["oid"] == row["oid"]
    finally:
        stop()
    assert index_rows(db, [FIRST_NAME])[FIRST_NAME]["valid"] is True, "the other session's build completes untouched"


@pytest.mark.asyncio
async def test_simultaneous_migration_invocation_fails_fast_under_session_lock(db):
    """A second 0057 invocation cannot plan/build while the first owns the session lock."""
    conn = await asyncpg.connect(_dsn(_url_for_db(ADMIN_DATABASE_URL, db)))
    try:
        assert await conn.fetchval("SELECT pg_try_advisory_lock(1940057, 1)")
        r = alembic(
            db,
            "upgrade",
            "head",
            env_overrides={"PGOPTIONS": "-c lock_timeout=3s -c statement_timeout=8s"},
        )
        assert r.returncode != 0 and "another migration invocation is active" in r.stderr + r.stdout
        assert await conn.fetchval("SELECT version_num FROM alembic_version") == PREVIOUS
        assert await conn.fetchval("SELECT to_regclass($1)", FIRST_NAME) is None
    finally:
        try:
            assert await conn.fetchval("SELECT pg_advisory_unlock(1940057, 1)")
        finally:
            await conn.close()
    alembic_ok(db, "upgrade", "head")
    assert (await _admin(db, "SELECT version_num FROM alembic_version"))[0]["version_num"] == HEAD


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


def test_downgrade_leaves_constraint_owned_same_name_index_without_partial_failure(db):
    alembic_ok(db, "upgrade", "head")
    victim = FIRST_NAME
    q(db, f"DROP INDEX {victim}")
    q(db, f"ALTER TABLE {FIRST_TABLE} ADD CONSTRAINT {victim} EXCLUDE USING btree ({FIRST_COLUMN} WITH =)")
    constrained_oid = index_rows(db, [victim])[victim]["oid"]

    alembic_ok(db, "downgrade", "-1")

    assert version(db) == PREVIOUS
    assert set(index_rows(db)) == {victim}
    assert index_rows(db, [victim])[victim]["oid"] == constrained_oid


def test_offline_sql_generation_is_rejected_with_a_clear_message(db):
    r = alembic(db, "upgrade", f"{PREVIOUS}:{HEAD}", "--sql")
    assert r.returncode != 0
    assert "offline" in (r.stderr + r.stdout).lower()


# --- deterministic multi-session scenarios (Codex re-review: TOCTOU, downgrade atomicity, lock lifetime) -----------------
#
# Every barrier below is a real PostgreSQL lock, never a sleep: the test waits (bounded) until the alembic backend is
# observably blocked at the exact step under test, performs the competing action from another session, then releases it.
# If the migration reaches the end without blocking, or never finishes, the test fails by ASSERTION with the output of the
# alembic process (processes are always killed on exit), so a regression cannot show up as a hang.

BARRIER_KEY = 777057
LATE_TABLE = "generator"  # built late in FK_INDEXES order, so earlier indexes already exist when a build is held up here
STEP_TIMEOUT = 90


async def _connect(db: str, **kwargs) -> asyncpg.Connection:
    return await asyncpg.connect(_dsn(_url_for_db(ADMIN_DATABASE_URL, db)), command_timeout=60, **kwargs)


async def _spawn_alembic(db: str, *args: str, env_overrides: dict[str, str] | None = None):
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *args,
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": _url_for_db(APP_DATABASE_URL, db), **(env_overrides or {})},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


async def _finish(proc, timeout: float = STEP_TIMEOUT) -> tuple[int, str]:
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        raise AssertionError(
            f"alembic did not finish within {timeout}s:\n{out.decode()}\n{err.decode()}"
        ) from None
    return proc.returncode, out.decode() + err.decode()


async def _wait_until(condition, proc, what: str, timeout: float = STEP_TIMEOUT) -> None:
    """Poll `condition()` until true. Fails by assertion if alembic exits first or the deadline passes."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if await condition():
            return
        if proc.returncode is not None:
            out, err = await proc.communicate()
            raise AssertionError(
                f"alembic exited (rc={proc.returncode}) before reaching: {what}\n{out.decode()}\n{err.decode()}"
            )
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for: {what}")
        await asyncio.sleep(0.1)


async def _reap(*procs) -> None:
    for proc in procs:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.communicate()


def _waiting_on_lock(db: str, query_like: str):
    async def check() -> bool:
        n = await _admin(
            db,
            "SELECT count(*) AS n FROM pg_stat_activity WHERE datname = $1 AND wait_event_type = 'Lock' "
            "AND query ILIKE $2",
            db,
            query_like,
        )
        return n[0]["n"] > 0

    return check


def _stamp_barrier_waiting(db: str):
    async def check() -> bool:
        n = await _admin(
            db,
            "SELECT count(*) AS n FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
            "AND classid = 0 AND objid = $1 AND objsubid = 1",
            BARRIER_KEY,
        )
        return n[0]["n"] > 0

    return check


_STAMP_TRIGGER_SQL = f"""
CREATE FUNCTION stamp_barrier() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_lock({BARRIER_KEY});   -- blocks until the test releases it
    PERFORM pg_advisory_unlock({BARRIER_KEY});
    RETURN NEW;
END $$;
CREATE TRIGGER stamp_barrier BEFORE UPDATE ON alembic_version FOR EACH ROW EXECUTE FUNCTION stamp_barrier();
"""


@contextlib.asynccontextmanager
async def paused_at_barrier(db: str, setup_sql: str, *alembic_args: str):
    """Run alembic and hold it exactly where `setup_sql`'s trigger blocks on the test-held advisory lock.

    Yields (proc, release). `release()` lets the blocked statement proceed. The pause is exact and timing-independent.
    """
    await _admin(db, setup_sql)
    gate = await _connect(db)
    proc = None
    try:
        await gate.fetchval("SELECT pg_advisory_lock($1)", BARRIER_KEY)
        proc = await _spawn_alembic(db, *alembic_args)
        await _wait_until(_stamp_barrier_waiting(db), proc, "the barrier trigger")

        async def release() -> None:
            await gate.fetchval("SELECT pg_advisory_unlock($1)", BARRIER_KEY)

        yield proc, release
    finally:
        await _reap(proc)
        await gate.close()


def paused_at_version_stamp(db: str):
    """`alembic upgrade head`, held at the `alembic_version` update (after upgrade() returned)."""
    return paused_at_barrier(db, _STAMP_TRIGGER_SQL, "upgrade", "head")


_DROP_INDEX_BARRIER_SQL = f"""
CREATE FUNCTION drop_index_barrier() RETURNS event_trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_advisory_lock({BARRIER_KEY});
    PERFORM pg_advisory_unlock({BARRIER_KEY});
END $$;
CREATE EVENT TRIGGER drop_index_barrier ON ddl_command_start WHEN TAG IN ('DROP INDEX')
    EXECUTE FUNCTION drop_index_barrier();
"""


def paused_before_drop_index(db: str):
    """`alembic downgrade -1`, held after its inspection and just before the DROP INDEX statement starts."""
    return paused_at_barrier(db, _DROP_INDEX_BARRIER_SQL, "downgrade", "-1")


SPARE_NAME = "ix_spare_wrong"
DISPLACED_NAME = "ix_spare_displaced"


async def _try_rename_swap(db: str, victim: str) -> bool:
    """Swap a pre-created wrong index into `victim`'s name via two renames. True if the swap went through.

    ALTER INDEX ... RENAME needs only a SHARE UPDATE EXCLUSIVE lock on the index itself, not on its table, so table-level
    locks alone do not stop it. A short lock_timeout turns "blocked" into a clean, bounded LockNotAvailableError.
    """
    other = await _connect(db)
    try:
        await other.execute("SET lock_timeout = '1500ms'")
        tx = other.transaction()
        await tx.start()
        try:
            await other.execute(f"ALTER INDEX {victim} RENAME TO {DISPLACED_NAME}")
            await other.execute(f"ALTER INDEX {SPARE_NAME} RENAME TO {victim}")
            await tx.commit()
            return True
        except asyncpg.exceptions.LockNotAvailableError:
            await tx.rollback()
            return False
    finally:
        await other.close()


# -- P1: TOCTOU -------------------------------------------------------------------------------------------------------


def test_replacing_a_reused_index_mid_run_is_caught_before_the_stamp(db):
    """Plan says REUSE (no DDL), then another session swaps the index for a wrong one. 0057 must not be stamped."""

    async def scenario():
        await _admin(db, f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN})")
        blocker = await _connect(db)
        proc = None
        try:
            tx = blocker.transaction()
            await tx.start()
            await blocker.execute(f"LOCK TABLE {LATE_TABLE} IN SHARE UPDATE EXCLUSIVE MODE")
            proc = await _spawn_alembic(db, "upgrade", "head")
            # alembic is now parked inside the build phase: the plan is old, the reused index is not locked.
            await _wait_until(
                _waiting_on_lock(db, f"CREATE INDEX CONCURRENTLY%{LATE_TABLE}%"), proc, f"build of {LATE_TABLE} to block"
            )
            await _admin(db, f"DROP INDEX {FIRST_NAME}")
            await _admin(db, f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} (id)")
            await tx.rollback()
            return await _finish(proc)
        finally:
            await _reap(proc)
            await blocker.close()

    rc, out = _run(scenario())
    assert rc != 0, f"0057 was stamped although the reused index was replaced by a wrong one:\n{out}"
    assert "Refusing to stamp" in out and FIRST_NAME in out
    assert version(db) == PREVIOUS
    replacement = index_rows(db, [FIRST_NAME])[FIRST_NAME]
    assert "(id)" in replacement["def"], "the replacement must be left exactly as the other session created it"


def test_replacing_an_interrupted_artifact_before_the_drop_never_drops_the_replacement(db):
    """Plan says REBUILD, then another session replaces the artifact. The drop must act on the planned object only."""

    async def scenario():
        await _admin(db, f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN})")
        await _admin(
            db,
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid = ('public.' || $1)::regclass",
            FIRST_NAME,
        )
        holder = await _connect(db)
        proc = None
        try:
            tx = holder.transaction()
            await tx.start()
            await holder.execute(f"SELECT 1 FROM {FIRST_TABLE} LIMIT 1")  # ACCESS SHARE: does not look like a build
            proc = await _spawn_alembic(db, "upgrade", "head")
            # alembic planned REBUILD and is queued for ACCESS EXCLUSIVE behind our read lock: the plan is now stale.
            await _wait_until(
                _waiting_on_lock(db, "LOCK TABLE%ACCESS EXCLUSIVE%"), proc, "the ACCESS EXCLUSIVE lock request to queue"
            )
            await holder.execute(f"DROP INDEX {FIRST_NAME}")
            await holder.execute(f"CREATE INDEX {FIRST_NAME} ON alarm (id)")
            await tx.commit()
            return await _finish(proc)
        finally:
            await _reap(proc)
            await holder.close()

    rc, out = _run(scenario())
    assert rc != 0 and "changed while the migration was running" in out, out
    assert version(db) == PREVIOUS
    rows = index_rows(db)
    assert set(rows) == {FIRST_NAME}, "nothing may be built once the plan is found stale"
    survivor = rows[FIRST_NAME]
    assert (survivor["tbl"], survivor["valid"]) == ("alarm", True), "the unrelated replacement must survive untouched"


def test_index_cannot_be_replaced_between_final_verification_and_the_stamp(db):
    """With alembic parked at the version update, the verified indexes are pinned: DDL on those tables must wait."""

    async def scenario():
        async with paused_at_version_stamp(db) as (proc, release):
            other = await _connect(db)
            try:
                await other.execute("SET lock_timeout = '1500ms'")
                try:
                    await other.execute(f"DROP INDEX {FIRST_NAME}")
                except asyncpg.exceptions.LockNotAvailableError:
                    swapped = False
                else:
                    swapped = True
                pinned = await _admin(
                    db,
                    "SELECT count(DISTINCT c.relname) AS n FROM pg_locks l JOIN pg_class c ON c.oid = l.relation "
                    "WHERE l.locktype = 'relation' AND l.mode = 'ShareUpdateExclusiveLock' AND l.granted "
                    "AND c.relname = ANY($1) AND l.pid <> pg_backend_pid()",
                    sorted({t for t, _ in FK_INDEXES}),
                )
            finally:
                await other.close()
            await release()
            rc, out = await _finish(proc)
            return swapped, pinned[0]["n"], rc, out

    swapped, pinned_tables, rc, out = _run(scenario())
    assert not swapped, "an index was dropped between the final verification and the version stamp"
    assert pinned_tables == len({t for t, _ in FK_INDEXES}), "every target table must be pinned until the stamp commits"
    assert rc == 0, out
    assert version(db) == HEAD
    assert_all_ours_valid(db)


# -- P2: advisory lock lifetime ---------------------------------------------------------------------------------------


def test_second_runner_fails_fast_while_the_first_is_parked_at_the_version_stamp(db):
    """The lock must outlive upgrade(): the first runner has returned from 0057 but has not yet stamped it."""

    async def scenario():
        async with paused_at_version_stamp(db) as (proc, release):
            second = await _spawn_alembic(db, "upgrade", "head")
            try:
                second_rc, second_out = await _finish(second, timeout=60)
            finally:
                await _reap(second)
            await release()
            rc, out = await _finish(proc)
            return second_rc, second_out, rc, out

    second_rc, second_out, rc, out = _run(scenario())
    assert second_rc != 0 and "another migration invocation is active" in second_out, second_out
    assert rc == 0, out
    assert version(db) == HEAD
    assert_all_ours_valid(db)
    alembic_ok(db, "check")


def test_failed_run_releases_the_advisory_lock_and_the_next_run_succeeds(db):
    q(db, WRONG_DEFINITIONS["wrong_column"])
    r = alembic(db, "upgrade", "head")
    assert r.returncode != 0 and "Refusing" in r.stderr + r.stdout

    async def lock_is_free():
        conn = await _connect(db)
        try:
            return await conn.fetchval("SELECT pg_try_advisory_lock(1940057, 1)")
        finally:
            await conn.close()

    assert _run(lock_is_free()), "a failed run must not leave the migration advisory lock held"
    q(db, f"DROP INDEX {FIRST_NAME}")
    alembic_ok(db, "upgrade", "head")
    assert version(db) == HEAD


async def test_release_migration_locks_releases_every_stacked_acquisition(db):
    from sqlalchemy.ext.asyncio import create_async_engine

    from migrations.migration_lock import acquire_migration_lock, release_migration_locks

    engine = create_async_engine(_url_for_db(APP_DATABASE_URL, db))
    other = await _connect(db)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(lambda c: (acquire_migration_lock(c), acquire_migration_lock(c)))
            assert not await other.fetchval("SELECT pg_try_advisory_lock(1940057, 1)"), "held while acquired"
            await conn.run_sync(release_migration_locks)
            assert await other.fetchval("SELECT pg_try_advisory_lock(1940057, 1)"), "both acquisitions released"
    finally:
        await other.close()
        await engine.dispose()


# -- P2: downgrade atomicity ------------------------------------------------------------------------------------------

DOWNGRADE_FAILURE_TABLES = {
    "dropped_first": FK_INDEXES[-1][0],
    "dropped_in_the_middle": FK_INDEXES[len(FK_INDEXES) // 2][0],
    "dropped_last": FK_INDEXES[0][0],
}


@pytest.mark.parametrize("position", sorted(DOWNGRADE_FAILURE_TABLES))
def test_failed_downgrade_leaves_version_and_all_22_indexes_unchanged(db, position):
    """Inject a failure at a chosen position of the drop sequence (table owned by someone else)."""
    alembic_ok(db, "upgrade", "head")
    before = assert_all_ours_valid(db)
    failing_table = DOWNGRADE_FAILURE_TABLES[position]
    q(db, f"ALTER TABLE {failing_table} OWNER TO postgres")

    r = alembic(db, "downgrade", "-1")

    assert r.returncode != 0, "the downgrade was expected to fail"
    assert version(db) == HEAD, "a failed downgrade must leave 0057 stamped"
    after = index_rows(db)
    assert sorted(after) == sorted(NAMES), f"indexes were removed by a failed downgrade: {sorted(set(NAMES) - set(after))}"
    assert {n: after[n]["oid"] for n in NAMES} == {n: before[n]["oid"] for n in NAMES}, "indexes must be untouched"

    q(db, f"ALTER TABLE {failing_table} OWNER TO dcim_app")
    alembic_ok(db, "downgrade", "-1")
    assert version(db) == PREVIOUS and index_rows(db) == {}


def test_downgrade_that_cannot_get_its_locks_changes_nothing(db):
    """A reader holding a lock on a late table makes the lock wait time out; nothing may be dropped or re-stamped."""
    alembic_ok(db, "upgrade", "head")
    before = assert_all_ours_valid(db)

    async def scenario():
        holder = await _connect(db)
        proc = None
        try:
            tx = holder.transaction()
            await tx.start()
            await holder.execute(f"SELECT 1 FROM {LATE_TABLE} LIMIT 1")
            proc = await _spawn_alembic(db, "downgrade", "-1")
            result = await _finish(proc)  # bounded: the migration applies its own 5s lock_timeout
            await tx.rollback()
            return result
        finally:
            await _reap(proc)
            await holder.close()

    rc, out = _run(scenario())
    assert rc != 0 and "lock timeout" in out.lower(), out
    assert version(db) == HEAD
    after = index_rows(db)
    assert {n: after[n]["oid"] for n in NAMES} == {n: before[n]["oid"] for n in NAMES}
    alembic_ok(db, "downgrade", "-1")
    assert version(db) == PREVIOUS and index_rows(db) == {}


# -- P1 (Codex re-review of bfacdf8): index rename races ---------------------------------------------------------------


def test_rename_swap_between_final_verification_and_the_stamp_is_blocked(db):
    """Table locks do not stop ALTER INDEX RENAME. The verified INDEX objects themselves must be pinned until the stamp."""
    q(db, f"CREATE INDEX {SPARE_NAME} ON {FIRST_TABLE} (id)")  # a wrong-definition index waiting in the wings

    async def scenario():
        async with paused_at_version_stamp(db) as (proc, release):
            swapped = await _try_rename_swap(db, FIRST_NAME)
            await release()
            rc, out = await _finish(proc)
            return swapped, rc, out

    swapped, rc, out = _run(scenario())
    assert not swapped, "a wrong index was renamed into the verified name after verification and before the stamp"
    assert rc == 0, out
    assert version(db) == HEAD
    assert_all_ours_valid(db)
    assert index_rows(db, [SPARE_NAME])[SPARE_NAME]["col"] == "id", "the spare index must be untouched"


def test_rename_swap_between_downgrade_inspection_and_drop_is_blocked(db):
    """The downgrade must not drop an unrelated index that was renamed into one of our names after inspection."""
    alembic_ok(db, "upgrade", "head")
    q(db, f"CREATE INDEX {SPARE_NAME} ON {FIRST_TABLE} (id)")

    async def scenario():
        async with paused_before_drop_index(db) as (proc, release):
            swapped = await _try_rename_swap(db, FIRST_NAME)
            await release()
            rc, out = await _finish(proc)
            return swapped, rc, out

    swapped, rc, out = _run(scenario())
    assert not swapped, "a wrong index was renamed into one of our names between inspection and DROP INDEX"
    assert rc == 0, out
    assert version(db) == PREVIOUS
    leftovers = index_rows(db, NAMES + [SPARE_NAME, DISPLACED_NAME])
    assert set(leftovers) == {SPARE_NAME}, f"only the unrelated spare index may remain, found {sorted(leftovers)}"
    assert leftovers[SPARE_NAME]["col"] == "id"


def test_pinning_indexes_cannot_deadlock_into_a_half_applied_state(db):
    """A session holding the last index and asking for the first (opposite order) must resolve, not hang or corrupt."""
    alembic_ok(db, "upgrade", "head")
    q(db, "UPDATE alembic_version SET version_num = $1", PREVIOUS)  # all 22 exist and are valid; only the stamp is missing
    first, last = sorted(NAMES)[0], sorted(NAMES)[-1]

    async def scenario():
        holder = await _connect(db)
        proc = None
        try:
            tx = holder.transaction()
            await tx.start()
            await holder.execute(f"COMMENT ON INDEX {last} IS NULL")  # SHARE UPDATE EXCLUSIVE on the last index
            proc = await _spawn_alembic(db, "upgrade", "head")
            await _wait_until(
                _waiting_on_lock(db, "COMMENT ON INDEX%"), proc, "the migration to queue behind the held index"
            )
            try:
                await holder.execute(f"COMMENT ON INDEX {first} IS NULL")  # opposite order -> a genuine lock cycle
            except asyncpg.exceptions.DeadlockDetectedError:
                pass
            await tx.rollback()
            return await _finish(proc)
        finally:
            await _reap(proc)
            await holder.close()

    rc, out = _run(scenario())  # bounded: PostgreSQL's deadlock detector breaks the cycle within ~1s
    after = index_rows(db)
    assert sorted(after) == sorted(NAMES), "a deadlock must never lose an index"
    assert version(db) == (HEAD if rc == 0 else PREVIOUS), f"stamp must match the outcome (rc={rc}): {out}"
    if rc != 0:
        assert "deadlock" in out.lower() or "lock timeout" in out.lower(), out
        alembic_ok(db, "upgrade", "head")  # a retry completes
    assert version(db) == HEAD
    assert_all_ours_valid(db)


def test_pinning_leaves_index_comments_exactly_as_they_were(db):
    """The index pin is a lock-only COMMENT: an existing comment (even a tricky one) survives, others stay uncommented."""
    tricky = "keep me: it's ':named' and $1 and 100% 'quoted'"
    q(db, f"CREATE INDEX {FIRST_NAME} ON {FIRST_TABLE} ({FIRST_COLUMN})")
    q(db, f"COMMENT ON INDEX {FIRST_NAME} IS '{tricky.replace(chr(39), chr(39) * 2)}'")
    alembic_ok(db, "upgrade", "head")
    comments = {
        r["name"]: r["comment"]
        for r in q(
            db,
            "SELECT c.relname AS name, obj_description(c.oid, 'pg_class') AS comment FROM pg_class c "
            "WHERE c.relname = ANY($1)",
            NAMES,
        )
    }
    assert comments[FIRST_NAME] == tricky
    assert all(v is None for n, v in comments.items() if n != FIRST_NAME)
    alembic_ok(db, "downgrade", "-1")
    assert version(db) == PREVIOUS and index_rows(db) == {}
