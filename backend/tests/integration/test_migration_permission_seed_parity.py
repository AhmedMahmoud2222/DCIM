"""Migration path-parity regression test (PR-15/PR-2 correction).

Proves that freezing migrations 0002/0004/0006/0008/0009/0010's
`app.application.rbac.DEFAULT_ROLE_PERMISSIONS` import to a literal historical
snapshot (rather than reading the live, ever-growing dict at migration-run-time)
produces byte-for-byte identical seeded RBAC state on two genuinely different paths to
the same head:

1. A brand-new database, migrated straight to head in one `alembic upgrade head` call
   -- what a new install does today.
2. An existing, already-migrated database that had only reached
   `0020_catalog_legacy_bridge` (the revision immediately before the catalog:*
   permission seed this correction targets -- i.e. an install that predates Phase 10A's
   RBAC migration), then continues to head with a second `alembic upgrade head` call --
   what upgrading an existing production database through these newer migrations does.

Before the fix, migration 0002 read the *live* `DEFAULT_ROLE_PERMISSIONS` (which had
grown to include `catalog:*` codes once Phase 10A PR-2 added them to that dict) and
seeded them itself, colliding with migration 0021's own separately hardcoded `catalog:*`
seed with a `UniqueViolationError` on `uq_permission_resource_action` -- on *both* paths
above, since both run 0002 before 0021 regardless of where the run is split. This test
runs real `alembic upgrade`/`downgrade` subprocesses against two genuinely separate,
freshly created PostgreSQL databases (not simulated, not mocked, not the shared
long-lived `dcim_test` database this repo's other tests reuse -- that database's
already-applied migration history is exactly why this bug went unnoticed all session:
`alembic upgrade head` against an already-head database is a no-op, so the from-scratch
collision never fired against it)."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]
ADMIN_DATABASE_URL = os.environ["TEST_ADMIN_DATABASE_URL"]
APP_DATABASE_URL = os.environ["DATABASE_URL"]

_CATALOG_MUTATION_ACTIONS = {"manage", "publish", "retire", "import", "migrate", "read_draft"}


def _url_for_db(base_url: str, db_name: str) -> str:
    # str(URL) / repr(URL) mask the password as a literal "***" — must render explicitly
    # with hide_password=False or every downstream connection attempt authenticates with
    # the four-character string "***" instead of the real credential.
    return make_url(base_url).set(database=db_name).render_as_string(hide_password=False)


def _asyncpg_dsn(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+asyncpg://", "postgresql://", 1)


def _run_alembic(database_url: str, *args: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} against {database_url!r} failed:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


async def _create_scratch_database(db_name: str) -> None:
    maintenance_dsn = _asyncpg_dsn(_url_for_db(ADMIN_DATABASE_URL, "postgres"))
    admin_conn = await asyncpg.connect(maintenance_dsn)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin_conn.close()

    db_dsn = _asyncpg_dsn(_url_for_db(ADMIN_DATABASE_URL, db_name))
    conn = await asyncpg.connect(db_dsn)
    try:
        await conn.execute("GRANT CREATE, USAGE ON SCHEMA public TO dcim_app")
        await conn.execute('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"')
        await conn.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
        await conn.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    finally:
        await conn.close()


async def _drop_scratch_database(db_name: str) -> None:
    maintenance_dsn = _asyncpg_dsn(_url_for_db(ADMIN_DATABASE_URL, "postgres"))
    admin_conn = await asyncpg.connect(maintenance_dsn)
    try:
        await admin_conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid()",
            db_name,
        )
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    finally:
        await admin_conn.close()


async def _fetch_rbac_snapshot(app_db_url: str) -> dict:
    """Compares by content (resource/action/role name), never by row id -- ids are
    freshly generated uuid4() values on every independent migration run, so an id-level
    comparison would never match between two separately created databases even when the
    seeded RBAC content is genuinely identical."""
    engine = create_async_engine(app_db_url, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            permissions = (
                await conn.execute(text("SELECT resource, action FROM permission ORDER BY resource, action"))
            ).all()
            roles = (await conn.execute(text("SELECT name FROM role ORDER BY name"))).all()
            grants = (
                await conn.execute(
                    text(
                        "SELECT r.name AS role_name, p.resource, p.action FROM role_permission rp "
                        "JOIN role r ON r.id = rp.role_id JOIN permission p ON p.id = rp.permission_id "
                        "ORDER BY r.name, p.resource, p.action"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    return {
        "permissions": {(row.resource, row.action) for row in permissions},
        "roles": {row.name for row in roles},
        "grants": {(row.role_name, row.resource, row.action) for row in grants},
    }


async def test_migration_path_parity_fresh_head_vs_incremental_upgrade_from_pre_0021():
    db_a = f"dcim_parity_a_{uuid.uuid4().hex[:8]}"
    db_b = f"dcim_parity_b_{uuid.uuid4().hex[:8]}"
    app_url_a = _url_for_db(APP_DATABASE_URL, db_a)
    app_url_b = _url_for_db(APP_DATABASE_URL, db_b)
    try:
        await _create_scratch_database(db_a)
        await _create_scratch_database(db_b)

        # Path A: a brand-new install, straight to head in one shot.
        _run_alembic(app_url_a, "upgrade", "head")

        # Path B: an existing install that had only reached the revision immediately
        # before the catalog:* RBAC seed (0021) -- exactly what upgrading a real,
        # already-migrated production database through these newer migrations does --
        # then continues to head with a second, separate upgrade call.
        _run_alembic(app_url_b, "upgrade", "0020_catalog_legacy_bridge")
        _run_alembic(app_url_b, "upgrade", "head")

        snapshot_a = await _fetch_rbac_snapshot(app_url_a)
        snapshot_b = await _fetch_rbac_snapshot(app_url_b)

        assert snapshot_a["roles"] == snapshot_b["roles"]
        assert snapshot_a["permissions"] == snapshot_b["permissions"]
        assert snapshot_a["grants"] == snapshot_b["grants"], (
            "the two paths to head seeded different permission/role/role_permission "
            "content -- a frozen migration must not depend on when or in how many steps "
            "it happens to run"
        )

        for label, snapshot in (("path A (straight to head)", snapshot_a), ("path B (incremental)", snapshot_b)):
            mutation_grant_roles = {
                role_name
                for role_name, resource, action in snapshot["grants"]
                if resource == "catalog" and action in _CATALOG_MUTATION_ACTIONS
            }
            assert mutation_grant_roles == {"Administrator"}, (
                f"{label}: only Administrator may hold a catalog mutation permission, got {mutation_grant_roles}"
            )
            read_grant_roles = {
                role_name for role_name, resource, action in snapshot["grants"] if resource == "catalog" and action == "read"
            }
            assert read_grant_roles == {"Administrator", "DCIM Manager", "Engineer", "Operator", "Viewer"}, (
                f"{label}: catalog:read must be granted to every seeded role, got {read_grant_roles}"
            )
    finally:
        await _drop_scratch_database(db_a)
        await _drop_scratch_database(db_b)
