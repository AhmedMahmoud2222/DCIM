"""Issue #145 follow-up: fail-closed CHECK-constraint normalization.

`_normalize_check_constraint_def` asks PostgreSQL to parse/normalize a CHECK
expression via a temporary probe. Prior to this fix, any probe failure (malformed
expression, missing catalog row) was swallowed and treated as "no drift" — the exact
opposite of what a schema-drift gate must do. These tests prove, against a real
PostgreSQL connection, that:

1. A probe failure raises `SchemaDriftNormalizationError` rather than being silently
   accepted as a match.
2. The failure is isolated to its own SAVEPOINT: the connection remains fully usable
   for every other constraint checked in the same run (no poisoned transaction).
3. `missing_or_modified_parent_metadata_constraints` reports an unverifiable
   constraint as drift instead of crashing or silently passing.

The "missing catalog result" branch (`_normalize_check_constraint_def` returning no
row despite the ALTER succeeding) has no reachable real-PostgreSQL fixture: PostgreSQL
cannot execute `ALTER TABLE ... ADD CONSTRAINT` without a corresponding `pg_constraint`
row appearing in the same transaction. That branch is exercised directly with a
minimal fake connection instead.
"""

import asyncio
import os

import pytest
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, Table, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from migrations.schema_drift import (
    SchemaDriftNormalizationError,
    _normalize_check_constraint_def,
    attached_partitions,
    existing_parent_tables,
    local_partition_drift,
    missing_or_modified_parent_metadata_constraints,
)

APP_DATABASE_URL = os.environ["DATABASE_URL"]
ADMIN_DATABASE_URL = os.environ["TEST_ADMIN_DATABASE_URL"]


# `AsyncConnection` has no direct sync `Connection` to hand to code written for the
# sync Alembic migration context; `run_sync` is the standard bridge (same pattern the
# app's own `do_run_migrations` uses via `connection.run_sync`).
async def _run_sync(fn, *args, url=APP_DATABASE_URL):
    engine = create_async_engine(url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(lambda sync_conn: fn(sync_conn, *args))
    finally:
        await engine.dispose()


def _normalize_invalid_expression(sync_conn: Connection) -> None:
    with pytest.raises(SchemaDriftNormalizationError):
        _normalize_check_constraint_def(sync_conn, "public", "audit_log", "nonexistent_column_xyz > 0")


async def test_normalize_check_constraint_fails_closed_on_malformed_expression():
    """A probe that PostgreSQL cannot parse/execute must raise, not return None."""
    await _run_sync(_normalize_invalid_expression)


def _normalize_then_recover(sync_conn: Connection) -> None:
    with pytest.raises(SchemaDriftNormalizationError):
        _normalize_check_constraint_def(sync_conn, "public", "audit_log", "nonexistent_column_xyz > 0")

    # The failed probe must be isolated to its own SAVEPOINT: the connection is still
    # usable for an ordinary query...
    assert sync_conn.execute(text("SELECT 1")).scalar() == 1

    # ...and for a second, valid normalization call on the same connection, in the
    # same outer transaction, proving the earlier failure did not poison it.
    norm = _normalize_check_constraint_def(sync_conn, "public", "audit_log", "result IN ('success', 'failure')")
    assert "result" in norm


async def test_connection_recovers_after_normalization_failure():
    """A probe failure must not abort the caller's outer transaction (savepoint isolation)."""
    await _run_sync(_normalize_then_recover)


def _missing_or_modified_with_unverifiable_constraint(sync_conn: Connection) -> None:
    # A synthetic parent table/constraint, independent of the real app metadata, naming
    # the real `audit_log.ck_audit_log_result_allowed` constraint (app/db/base.py's
    # naming_convention prefixes every CheckConstraint's explicit name with
    # `ck_<table>_`) but with a deliberately malformed SQL expression, so normalization
    # is forced to fail for it.
    synthetic_metadata = MetaData()
    Table(
        "audit_log",
        synthetic_metadata,
        CheckConstraint("nonexistent_column_xyz > 0", name="ck_audit_log_result_allowed"),
    )

    drift = missing_or_modified_parent_metadata_constraints(sync_conn, {"audit_log"}, synthetic_metadata)

    assert any("ck_audit_log_result_allowed" in entry and "treated as drift" in entry for entry in drift), drift


async def test_missing_or_modified_parent_metadata_constraints_fails_closed():
    """An unverifiable constraint must surface as drift, never be silently accepted."""
    await _run_sync(_missing_or_modified_with_unverifiable_constraint)


class _FakeNestedTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConnectionMissingCatalogRow:
    """Simulates CREATE/ALTER succeeding but the catalog lookup returning no row.

    Not reachable with real PostgreSQL (see module docstring) — this exercises the
    defensive `norm_def is None` branch directly and deterministically.
    """

    dialect = postgresql.dialect()

    def begin_nested(self):
        return _FakeNestedTransaction()

    def execute(self, _statement, _params=None):
        return _FakeScalarResult(None)


def test_normalize_check_constraint_fails_closed_on_missing_catalog_row():
    fake_conn = _FakeConnectionMissingCatalogRow()
    with pytest.raises(SchemaDriftNormalizationError, match="no catalog definition"):
        _normalize_check_constraint_def(fake_conn, "public", "audit_log", "true")


class _FakeConnectionScripted(_FakeConnectionMissingCatalogRow):
    """Returns a scripted catalog value, or raises a scripted exception, from execute()."""

    def __init__(self, value=None, error=None):
        self._value = value
        self._error = error

    def execute(self, _statement, _params=None):
        if self._error is not None:
            raise self._error
        return _FakeScalarResult(self._value)


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_normalize_fails_closed_on_blank_catalog_result(blank):
    with pytest.raises(SchemaDriftNormalizationError, match="no catalog definition"):
        _normalize_check_constraint_def(_FakeConnectionScripted(value=blank), "public", "audit_log", "true")


@pytest.mark.parametrize("expression", ["", "   ", None])
def test_normalize_rejects_blank_expression(expression):
    with pytest.raises(SchemaDriftNormalizationError, match="blank CHECK expression"):
        _normalize_check_constraint_def(_FakeConnectionScripted(value="x"), "public", "audit_log", expression)


def test_normalize_wraps_unexpected_non_sqlalchemy_exceptions():
    fake = _FakeConnectionScripted(error=ValueError("driver exploded"))
    with pytest.raises(SchemaDriftNormalizationError, match="driver exploded") as info:
        _normalize_check_constraint_def(fake, "public", "audit_log", "true")
    assert isinstance(info.value.__cause__, ValueError)


def _probe_relations(sync_conn: Connection) -> int:
    return sync_conn.execute(
        text("SELECT count(*) FROM pg_class WHERE relname LIKE '\\_norm\\_probe\\_%' ")
    ).scalar()


def _normalize_valid_repeated_and_clean(sync_conn: Connection) -> None:
    expression = "result IN ('success', 'failure')"
    results = {_normalize_check_constraint_def(sync_conn, "public", "audit_log", expression) for _ in range(5)}
    assert len(results) == 1
    for bad in ("nonexistent_column_xyz > 0", "result IN (", "result IN ('a'))) --", "1 +"):
        with pytest.raises(SchemaDriftNormalizationError):
            _normalize_check_constraint_def(sync_conn, "public", "audit_log", bad)
    assert sync_conn.execute(text("SELECT 1")).scalar() == 1
    assert _probe_relations(sync_conn) == 0


async def test_valid_repeated_normalization_and_no_lingering_probe_relations():
    await _run_sync(_normalize_valid_repeated_and_clean)


def _normalize_missing_parent(sync_conn: Connection) -> None:
    with pytest.raises(SchemaDriftNormalizationError):
        _normalize_check_constraint_def(sync_conn, "public", "no_such_parent_table", "true")
    assert sync_conn.execute(text("SELECT 1")).scalar() == 1
    assert _probe_relations(sync_conn) == 0


async def test_normalize_fails_closed_when_parent_does_not_exist():
    await _run_sync(_normalize_missing_parent)


def _normalize_unusual_identifiers(sync_conn: Connection) -> None:
    sync_conn.execute(text('CREATE SCHEMA "Odd Schema ""Q"""'))
    sync_conn.execute(
        text('CREATE TABLE "Odd Schema ""Q"""."Odd ""Table"" ; --" ("Weird Col" integer, "select" integer)')
    )
    norm = _normalize_check_constraint_def(
        sync_conn, 'Odd Schema "Q"', 'Odd "Table" ; --', '"Weird Col" > 0 AND "select" < 10'
    )
    assert '"Weird Col"' in norm
    assert '"select"' in norm
    assert _probe_relations(sync_conn) == 0


async def test_normalization_quotes_unusual_schema_table_and_column_identifiers():
    # Runs as the admin role inside a transaction that is never committed.
    await _run_sync(_normalize_unusual_identifiers, url=ADMIN_DATABASE_URL)


def _normalize_ignores_search_path_shadow(sync_conn: Connection) -> None:
    # A same-named table in another schema must not be picked up instead of the
    # schema-qualified parent.
    sync_conn.execute(text("CREATE SCHEMA shadow_ns"))
    sync_conn.execute(text("CREATE TABLE shadow_ns.audit_log (other integer)"))
    sync_conn.execute(text("SET LOCAL search_path TO shadow_ns, public"))
    norm = _normalize_check_constraint_def(sync_conn, "public", "audit_log", "result IN ('success', 'failure')")
    assert "result" in norm


async def test_normalization_uses_schema_qualified_parent_not_search_path():
    await _run_sync(_normalize_ignores_search_path_shadow, url=ADMIN_DATABASE_URL)


async def test_concurrent_normalization_on_separate_connections():
    expression = "result IN ('success', 'failure')"

    def work(sync_conn: Connection) -> list[str]:
        return [_normalize_check_constraint_def(sync_conn, "public", "audit_log", expression) for _ in range(10)]

    outcomes = await asyncio.gather(*(_run_sync(work) for _ in range(6)))
    flat = {definition for outcome in outcomes for definition in outcome}
    assert len(flat) == 1


def _scratch_partitioned_parent(sync_conn: Connection, with_check: bool = True) -> MetaData:
    check = ", CONSTRAINT ck_scratch_parent_val CHECK (val > 0)" if with_check else ""
    sync_conn.execute(text(f"CREATE TABLE scratch_parent (id integer NOT NULL, val integer{check}) PARTITION BY RANGE (id)"))
    metadata = MetaData()
    Table(
        "scratch_parent",
        metadata,
        Column("id", Integer),
        Column("val", Integer),
        CheckConstraint("val > 0", name="ck_scratch_parent_val"),
    )
    return metadata


def _parent_constraint_drift_cases(sync_conn: Connection) -> None:
    metadata = _scratch_partitioned_parent(sync_conn)

    # Empty parent (zero attached children) is still discovered and validated.
    assert existing_parent_tables(sync_conn, {"scratch_parent"}) == {"scratch_parent"}
    assert attached_partitions(sync_conn, {"scratch_parent"}) == set()
    assert missing_or_modified_parent_metadata_constraints(sync_conn, {"scratch_parent"}, metadata) == []

    sync_conn.execute(text("ALTER TABLE scratch_parent ADD CONSTRAINT ck_scratch_extra CHECK (id > 0)"))
    drift = missing_or_modified_parent_metadata_constraints(sync_conn, {"scratch_parent"}, metadata)
    assert drift == ["unexpected parent constraint public.scratch_parent.ck_scratch_extra"]
    sync_conn.execute(text("ALTER TABLE scratch_parent DROP CONSTRAINT ck_scratch_extra"))

    sync_conn.execute(text("ALTER TABLE scratch_parent DROP CONSTRAINT ck_scratch_parent_val"))
    sync_conn.execute(text("ALTER TABLE scratch_parent ADD CONSTRAINT ck_scratch_parent_val CHECK (val > 5)"))
    drift = missing_or_modified_parent_metadata_constraints(sync_conn, {"scratch_parent"}, metadata)
    assert drift == ["modified parent constraint public.scratch_parent.ck_scratch_parent_val"]

    sync_conn.execute(text("ALTER TABLE scratch_parent DROP CONSTRAINT ck_scratch_parent_val"))
    drift = missing_or_modified_parent_metadata_constraints(sync_conn, {"scratch_parent"}, metadata)
    assert drift == ["missing parent constraint public.scratch_parent.ck_scratch_parent_val"]
    assert _probe_relations(sync_conn) == 0


async def test_parent_constraint_missing_modified_unexpected_on_empty_parent():
    await _run_sync(_parent_constraint_drift_cases, url=ADMIN_DATABASE_URL)


def _partition_local_drift_cases(sync_conn: Connection) -> None:
    _scratch_partitioned_parent(sync_conn)
    sync_conn.execute(text("CREATE INDEX ix_scratch_parent_val ON scratch_parent (val)"))
    sync_conn.execute(
        text("CREATE TABLE scratch_child PARTITION OF scratch_parent FOR VALUES FROM (0) TO (100)")
    )
    attached = attached_partitions(sync_conn, {"scratch_parent"})
    assert attached == {("public", "scratch_child")}
    assert local_partition_drift(sync_conn, attached, {"scratch_parent"}) == []

    sync_conn.execute(text("CREATE INDEX ix_scratch_child_local ON scratch_child (id)"))
    sync_conn.execute(text("ALTER TABLE scratch_child ADD CONSTRAINT ck_scratch_child_local CHECK (val < 1000)"))
    drift = local_partition_drift(sync_conn, attached, {"scratch_parent"})
    assert "index public.scratch_child.ix_scratch_child_local" in drift
    assert "constraint public.scratch_child.ck_scratch_child_local" in drift

    # Detached table is no longer a partition, so it is not reported as attached.
    sync_conn.execute(text("ALTER TABLE scratch_parent DETACH PARTITION scratch_child"))
    assert attached_partitions(sync_conn, {"scratch_parent"}) == set()


async def test_partition_local_constraint_and_index_drift_and_detach():
    await _run_sync(_partition_local_drift_cases, url=ADMIN_DATABASE_URL)


def _cross_schema_collision(sync_conn: Connection) -> None:
    _scratch_partitioned_parent(sync_conn)
    sync_conn.execute(text("CREATE SCHEMA foreign_ns"))
    sync_conn.execute(
        text("CREATE TABLE foreign_ns.scratch_child PARTITION OF scratch_parent FOR VALUES FROM (0) TO (100)")
    )
    sync_conn.execute(text("CREATE TABLE public.scratch_child (id integer, val integer)"))
    attached = attached_partitions(sync_conn, {"scratch_parent"})
    # Only the foreign-schema child is attached; the same-named public table is not.
    assert attached == {("foreign_ns", "scratch_child")}
    assert ("public", "scratch_child") not in attached


async def test_cross_schema_name_collision_is_not_treated_as_attached():
    await _run_sync(_cross_schema_collision, url=ADMIN_DATABASE_URL)
