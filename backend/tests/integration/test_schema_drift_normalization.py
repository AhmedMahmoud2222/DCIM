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

import os

import pytest
from sqlalchemy import CheckConstraint, MetaData, Table, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from migrations.schema_drift import (
    SchemaDriftNormalizationError,
    _normalize_check_constraint_def,
    missing_or_modified_parent_metadata_constraints,
)

APP_DATABASE_URL = os.environ["DATABASE_URL"]


# `AsyncConnection` has no direct sync `Connection` to hand to code written for the
# sync Alembic migration context; `run_sync` is the standard bridge (same pattern the
# app's own `do_run_migrations` uses via `connection.run_sync`).
async def _run_sync(fn, *args):
    engine = create_async_engine(APP_DATABASE_URL, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(lambda sync_conn: fn(sync_conn, *args))
    finally:
        await engine.dispose()


def _normalize_invalid_expression(sync_conn: Connection) -> None:
    with pytest.raises(SchemaDriftNormalizationError):
        _normalize_check_constraint_def(sync_conn, "public.audit_log", "nonexistent_column_xyz > 0")


async def test_normalize_check_constraint_fails_closed_on_malformed_expression():
    """A probe that PostgreSQL cannot parse/execute must raise, not return None."""
    await _run_sync(_normalize_invalid_expression)


def _normalize_then_recover(sync_conn: Connection) -> None:
    with pytest.raises(SchemaDriftNormalizationError):
        _normalize_check_constraint_def(sync_conn, "public.audit_log", "nonexistent_column_xyz > 0")

    # The failed probe must be isolated to its own SAVEPOINT: the connection is still
    # usable for an ordinary query...
    assert sync_conn.execute(text("SELECT 1")).scalar() == 1

    # ...and for a second, valid normalization call on the same connection, in the
    # same outer transaction, proving the earlier failure did not poison it.
    norm = _normalize_check_constraint_def(sync_conn, "public.audit_log", "result IN ('success', 'failure')")
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

    def begin_nested(self):
        return _FakeNestedTransaction()

    def execute(self, _statement, _params=None):
        return _FakeScalarResult(None)


def test_normalize_check_constraint_fails_closed_on_missing_catalog_row():
    fake_conn = _FakeConnectionMissingCatalogRow()
    with pytest.raises(SchemaDriftNormalizationError, match="no catalog definition"):
        _normalize_check_constraint_def(fake_conn, "public.audit_log", "true")
