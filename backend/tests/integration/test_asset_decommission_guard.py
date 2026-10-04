"""Exercise the migrated PostgreSQL constraint and populated migration round trip."""

import importlib.util
import uuid
from datetime import datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.domain.identity.models import LIFECYCLE_STATUSES, ManagedAsset

CONSTRAINT = "ck_managed_asset_decommissioned_at_terminal"
STAMP = datetime(2026, 1, 1)
MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0033_asset_decommission_guard.py"


def _run_migration(connection, direction):
    spec = importlib.util.spec_from_file_location("asset_decommission_guard", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _insert(conn, status, stamp):
    asset_id = uuid.uuid4()
    await conn.execute(
        text("""
            INSERT INTO managed_asset (id, asset_type, asset_tag, lifecycle_status, external_ids, decommissioned_at)
            VALUES (:id, 'equipment', :tag, :status, '{}'::jsonb, :stamp)
        """),
        {"id": asset_id, "tag": str(asset_id), "status": status, "stamp": stamp},
    )
    return asset_id


@pytest.mark.parametrize("status", LIFECYCLE_STATUSES)
@pytest.mark.parametrize("stamp", [None, STAMP])
async def test_database_enforces_timestamp_status_matrix(db_engine, status, stamp):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            if stamp is not None and status not in {"decommissioned", "removed"}:
                with pytest.raises(IntegrityError, match=CONSTRAINT):
                    await _insert(conn, status, stamp)
            else:
                asset_id = await _insert(conn, status, stamp)
                assert await conn.scalar(
                    text("SELECT lifecycle_status FROM managed_asset WHERE id = :id"), {"id": asset_id}
                ) == status
        finally:
            await transaction.rollback()


async def test_constraint_blocks_direct_update_and_retains_timestamp(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            asset_id = await _insert(conn, "decommissioned", STAMP)
            async with conn.begin_nested():
                await conn.execute(
                    text("UPDATE managed_asset SET lifecycle_status = 'removed' WHERE id = :id"), {"id": asset_id}
                )
            assert await conn.scalar(
                text("SELECT decommissioned_at FROM managed_asset WHERE id = :id"), {"id": asset_id}
            ) == STAMP
            with pytest.raises(IntegrityError, match=CONSTRAINT):
                async with conn.begin_nested():
                    await conn.execute(
                        text("UPDATE managed_asset SET lifecycle_status = 'active' WHERE id = :id"), {"id": asset_id}
                    )
            assert await conn.scalar(
                text("SELECT lifecycle_status FROM managed_asset WHERE id = :id"), {"id": asset_id}
            ) == "removed"
        finally:
            await transaction.rollback()


async def test_populated_upgrade_downgrade_preserves_unknown_dates(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.run_sync(_run_migration, "downgrade")
            for status in LIFECYCLE_STATUSES:
                await _insert(conn, status, None)
            await _insert(conn, "decommissioned", STAMP)
            await _insert(conn, "removed", STAMP)
            before = (await conn.execute(text("SELECT * FROM managed_asset ORDER BY id"))).all()
            for direction in ("upgrade", "downgrade", "upgrade"):
                await conn.run_sync(_run_migration, direction)
                assert (await conn.execute(text("SELECT * FROM managed_asset ORDER BY id"))).all() == before
            assert await conn.scalar(text(
                "SELECT convalidated FROM pg_constraint "
                "WHERE conrelid = 'managed_asset'::regclass AND conname = :name"
            ), {"name": CONSTRAINT}) is True
        finally:
            await transaction.rollback()


async def test_upgrade_refuses_bad_history_without_rewriting_it(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.run_sync(_run_migration, "downgrade")
            asset_id = await _insert(conn, "active", STAMP)
            with pytest.raises(DBAPIError, match="audit and correct these rows explicitly"):
                async with conn.begin_nested():
                    await conn.run_sync(_run_migration, "upgrade")
            row = (await conn.execute(
                text("SELECT lifecycle_status, decommissioned_at FROM managed_asset WHERE id = :id"),
                {"id": asset_id},
            )).one()
            assert tuple(row) == ("active", STAMP)
            assert await conn.scalar(text(
                "SELECT count(*) FROM pg_constraint "
                "WHERE conrelid = 'managed_asset'::regclass AND conname = :name"
            ), {"name": CONSTRAINT}) == 0
        finally:
            await transaction.rollback()


def test_orm_declares_same_guard():
    assert CONSTRAINT in {constraint.name for constraint in ManagedAsset.__table__.constraints}
