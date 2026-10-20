"""SEC-ARCH-01 (#57) migration 0054: existing `building` assignments are never silently re-scoped."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

MIGRATION = Path(__file__).resolve().parents[2] / "migrations/versions/0054_role_assignment_scope_types.py"


def _run_migration(connection, direction):
    spec = importlib.util.spec_from_file_location("role_assignment_scope_types", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with Operations.context(MigrationContext.configure(connection)):
        getattr(module, direction)()


async def _assignment(conn, scope_type: str) -> uuid.UUID:
    user_id, role_id, assignment_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(text(
        "INSERT INTO app_user (id, email, password_hash, full_name, is_active) VALUES (:id, :email, 'x', 'u', true)"
    ), {"id": user_id, "email": f"{user_id}@example.test"})
    await conn.execute(text("INSERT INTO role (id, name, is_system) VALUES (:id, :name, false)"), {"id": role_id, "name": f"r-{role_id.hex[:8]}"})
    await conn.execute(text(
        "INSERT INTO role_assignment (id, user_id, role_id, scope_type, scope_id) VALUES (:id, :u, :r, :t, :s)"
    ), {"id": assignment_id, "u": user_id, "r": role_id, "t": scope_type, "s": uuid.uuid4()})
    return assignment_id


async def test_upgrade_refuses_while_building_assignments_exist_and_leaves_them_untouched(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await conn.run_sync(_run_migration, "downgrade")
            assignment_id = await _assignment(conn, "building")
            savepoint = await conn.begin_nested()
            with pytest.raises(RuntimeError, match="scope_type='building'"):
                await conn.run_sync(_run_migration, "upgrade")
            await savepoint.rollback()
            # not converted to site/organization/global: the row is exactly as it was
            assert await conn.scalar(text("SELECT scope_type FROM role_assignment WHERE id = :i"), {"i": assignment_id}) == "building"

            await conn.execute(text("DELETE FROM role_assignment WHERE id = :i"), {"i": assignment_id})
            await conn.run_sync(_run_migration, "upgrade")
            await _assignment(conn, "organization")
        finally:
            await transaction.rollback()


async def test_downgrade_refuses_while_organization_assignments_exist(db_engine):
    async with db_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            await _assignment(conn, "organization")
            with pytest.raises(RuntimeError, match="scope_type='organization'"):
                await conn.run_sync(_run_migration, "downgrade")
        finally:
            await transaction.rollback()
