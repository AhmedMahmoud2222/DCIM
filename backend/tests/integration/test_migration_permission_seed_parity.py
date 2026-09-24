"""Per-checkpoint migration RBAC-seed regression tests (fix/migration-seed-freeze).

`test_migration_seed_state_at_each_checkpoint` is parametrized over all six migrations
that seed `DEFAULT_ROLE_PERMISSIONS`-derived rows: 0002, 0004, 0006, 0008, 0009, 0010.
For each one, it runs a real `alembic upgrade` subprocess against a genuinely separate,
freshly created PostgreSQL database (never simulated, never the shared long-lived
`dcim_test` database this repo's other tests reuse -- that database's already-applied
migration history is exactly why the original bug this guards against went unnoticed for
an entire session: `alembic upgrade head` against an already-head database is a no-op, so
a from-scratch collision never fires against it), stops at that exact revision, and
asserts the `permission`/`role`/`role_permission` tables hold precisely the expected
content at that discrete point -- not "no error was raised," an exact set-equality
comparison against independently reconstructed historical data (see
`_EXPECTED_STATE_AT_CHECKPOINT`'s own docstring for how that data was derived and
verified). It then continues to head from that exact stopping point and confirms the run
completes cleanly, so a frozen-snapshot regression at any one checkpoint is caught at the
checkpoint that actually has it, not just "somewhere in the chain."

This module is scoped to the migration-seed-freeze fix only (0002-0010). The
straight-to-head-vs-incremental-from-0020 parity test that originally motivated this
correction lives on claude/phase10a-lifecycle-backend instead: it asserts parity against
migration 0021's catalog:* RBAC seed, which is Phase 10A catalog feature work and does not
exist on this branch (this branch's alembic head is 0013)."""

import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.ext.asyncio import create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[2]
ADMIN_DATABASE_URL = os.environ["TEST_ADMIN_DATABASE_URL"]
APP_DATABASE_URL = os.environ["DATABASE_URL"]

# Independently reconstructed (not imported from the migrations under test, which would
# make this tautological -- a bug in a migration's own frozen dict would trivially "match
# itself"): for each checkpoint, the exact value of
# `app.application.rbac.DEFAULT_ROLE_PERMISSIONS` at the commit that introduced that
# migration, extracted via `git show <sha>:backend/app/application/rbac.py` and parsed
# with `ast.literal_eval` (never hand-copied). Verified, at the time this table was
# built, that no other commit modified that dict between one checkpoint's introduction
# and the next's (`git log --oneline -- app/application/rbac.py` on the linear
# pre-Phase-10A history shows exactly six touching commits total, one per checkpoint
# below, with the six revision-introducing commits confirmed reachable with zero merge
# commits between them) -- so each checkpoint's snapshot is not just "correct the day
# that migration was written" but "still exactly correct, unrevised, the day the next
# migration in this table was written." Also confirmed monotonically non-decreasing
# (every code granted at one checkpoint is still granted at the next -- no migration in
# this range ever revokes a previously seeded permission), which is what makes "state
# after upgrading through migration N" equal to snapshot N outright, rather than some
# more complex accumulation.
_EXPECTED_STATE_AT_CHECKPOINT: dict[str, dict[str, list[str]]] = {
    "0002_seed": {
        "Administrator": ["organization:read", "organization:manage", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "user:manage", "role:manage", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read"],
        "DCIM Manager": ["organization:read", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read"],
        "Engineer": ["organization:read", "location:read", "location:update", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "spatial:read"],
        "Operator": ["organization:read", "location:read", "managed_asset:read", "managed_asset:update_lifecycle", "rack:read", "rack:place", "equipment:read", "equipment:place", "floor_plan:read", "spatial:read"],
        "Viewer": ["organization:read", "location:read", "managed_asset:read", "rack:read", "equipment:read", "floor_plan:read", "spatial:read"],
    },
    "0004_phase2": {
        "Administrator": ["organization:read", "organization:manage", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "user:manage", "role:manage", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read"],
        "DCIM Manager": ["organization:read", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read"],
        "Engineer": ["organization:read", "location:read", "location:update", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "spatial:read"],
        "Operator": ["organization:read", "location:read", "managed_asset:read", "managed_asset:update_lifecycle", "rack:read", "rack:place", "equipment:read", "equipment:place", "floor_plan:read", "spatial:read"],
        "Viewer": ["organization:read", "location:read", "managed_asset:read", "rack:read", "equipment:read", "floor_plan:read", "spatial:read"],
    },
    "0006_phase3": {
        "Administrator": ["organization:read", "organization:manage", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "user:manage", "role:manage", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read", "power:read", "power:manage", "capacity:read", "capacity:manage", "dashboard:read"],
        "DCIM Manager": ["organization:read", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read", "power:read", "power:manage", "capacity:read", "capacity:manage", "dashboard:read"],
        "Engineer": ["organization:read", "location:read", "location:update", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "spatial:read", "power:read", "power:manage", "capacity:read", "dashboard:read"],
        "Operator": ["organization:read", "location:read", "managed_asset:read", "managed_asset:update_lifecycle", "rack:read", "rack:place", "equipment:read", "equipment:place", "floor_plan:read", "spatial:read", "power:read", "capacity:read", "dashboard:read"],
        "Viewer": ["organization:read", "location:read", "managed_asset:read", "rack:read", "equipment:read", "floor_plan:read", "spatial:read", "power:read", "capacity:read", "dashboard:read"],
    },
    "0008_phase8": {
        "Administrator": ["organization:read", "organization:manage", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "user:manage", "role:manage", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read", "power:read", "power:manage", "capacity:read", "capacity:manage", "dashboard:read", "integration:read", "integration:manage", "collector:read", "collector:manage", "collector:assign", "discovery:read", "discovery:reconcile"],
        "DCIM Manager": ["organization:read", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read", "power:read", "power:manage", "capacity:read", "capacity:manage", "dashboard:read", "integration:read", "integration:manage", "collector:read", "collector:manage", "collector:assign", "discovery:read", "discovery:reconcile"],
        "Engineer": ["organization:read", "location:read", "location:update", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "spatial:read", "power:read", "power:manage", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read", "discovery:reconcile"],
        "Operator": ["organization:read", "location:read", "managed_asset:read", "managed_asset:update_lifecycle", "rack:read", "rack:place", "equipment:read", "equipment:place", "floor_plan:read", "spatial:read", "power:read", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read"],
        "Viewer": ["organization:read", "location:read", "managed_asset:read", "rack:read", "equipment:read", "floor_plan:read", "spatial:read", "power:read", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read"],
    },
    "0009_mvp_telemetry": {
        "Administrator": ["organization:read", "organization:manage", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "user:manage", "role:manage", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read", "power:read", "power:manage", "capacity:read", "capacity:manage", "dashboard:read", "integration:read", "integration:manage", "collector:read", "collector:manage", "collector:assign", "discovery:read", "discovery:reconcile", "telemetry:read", "telemetry:manage"],
        "DCIM Manager": ["organization:read", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read", "power:read", "power:manage", "capacity:read", "capacity:manage", "dashboard:read", "integration:read", "integration:manage", "collector:read", "collector:manage", "collector:assign", "discovery:read", "discovery:reconcile", "telemetry:read", "telemetry:manage"],
        "Engineer": ["organization:read", "location:read", "location:update", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "spatial:read", "power:read", "power:manage", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read", "discovery:reconcile", "telemetry:read"],
        "Operator": ["organization:read", "location:read", "managed_asset:read", "managed_asset:update_lifecycle", "rack:read", "rack:place", "equipment:read", "equipment:place", "floor_plan:read", "spatial:read", "power:read", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read", "telemetry:read"],
        "Viewer": ["organization:read", "location:read", "managed_asset:read", "rack:read", "equipment:read", "floor_plan:read", "spatial:read", "power:read", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read", "telemetry:read"],
    },
    "0010_mvp_alarms": {
        "Administrator": ["organization:read", "organization:manage", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "user:manage", "role:manage", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read", "power:read", "power:manage", "capacity:read", "capacity:manage", "dashboard:read", "integration:read", "integration:manage", "collector:read", "collector:manage", "collector:assign", "discovery:read", "discovery:reconcile", "telemetry:read", "telemetry:manage", "alarm:read", "alarm:manage"],
        "DCIM Manager": ["organization:read", "location:read", "location:update", "location:manage", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "audit:view", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "floor_plan:manage", "spatial:read", "power:read", "power:manage", "capacity:read", "capacity:manage", "dashboard:read", "integration:read", "integration:manage", "collector:read", "collector:manage", "collector:assign", "discovery:read", "discovery:reconcile", "telemetry:read", "telemetry:manage", "alarm:read", "alarm:manage"],
        "Engineer": ["organization:read", "location:read", "location:update", "managed_asset:read", "managed_asset:manage", "managed_asset:update_lifecycle", "rack:read", "rack:manage", "rack:place", "equipment:read", "equipment:manage", "equipment:place", "floor_plan:read", "floor_plan:import", "spatial:read", "power:read", "power:manage", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read", "discovery:reconcile", "telemetry:read", "alarm:read"],
        "Operator": ["organization:read", "location:read", "managed_asset:read", "managed_asset:update_lifecycle", "rack:read", "rack:place", "equipment:read", "equipment:place", "floor_plan:read", "spatial:read", "power:read", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read", "telemetry:read", "alarm:read"],
        "Viewer": ["organization:read", "location:read", "managed_asset:read", "rack:read", "equipment:read", "floor_plan:read", "spatial:read", "power:read", "capacity:read", "dashboard:read", "integration:read", "collector:read", "discovery:read", "telemetry:read", "alarm:read"],
    },
}

# Ordered so the parametrized test's failure output ("checkpoint 0006_phase3 ...") reads
# in the same order the migration chain actually runs them.
_CHECKPOINT_REVISIONS = ["0002_seed", "0004_phase2", "0006_phase3", "0008_phase8", "0009_mvp_telemetry", "0010_mvp_alarms"]


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


def _expected_grants_at(revision: str) -> set[tuple[str, str, str]]:
    expected = _EXPECTED_STATE_AT_CHECKPOINT[revision]
    grants: set[tuple[str, str, str]] = set()
    for role_name, codes in expected.items():
        for code in codes:
            resource, action = code.split(":")
            grants.add((role_name, resource, action))
    return grants


@pytest.mark.parametrize("revision", _CHECKPOINT_REVISIONS)
async def test_migration_seed_state_at_each_checkpoint(revision: str):
    """Stops at exactly this migration -- not head -- and asserts the permission/role/
    role_permission tables hold precisely the expected content at that discrete point,
    against `_EXPECTED_STATE_AT_CHECKPOINT`'s independently reconstructed historical data
    (see the module docstring for how it was derived; it is never imported from the
    migration files under test, which would make this assertion tautological). Then
    continues to head from that exact stopping point and confirms the run completes
    cleanly, so a frozen-snapshot regression is caught at the specific checkpoint that
    actually has it."""
    db_name = f"dcim_checkpoint_{revision.split('_')[0]}_{uuid.uuid4().hex[:8]}"
    app_url = _url_for_db(APP_DATABASE_URL, db_name)
    try:
        await _create_scratch_database(db_name)
        _run_alembic(app_url, "upgrade", revision)

        snapshot = await _fetch_rbac_snapshot(app_url)
        expected_roles = set(_EXPECTED_STATE_AT_CHECKPOINT[revision].keys())
        expected_grants = _expected_grants_at(revision)

        assert snapshot["roles"] == expected_roles, (
            f"{revision}: role table mismatch -- expected {expected_roles}, got {snapshot['roles']}"
        )
        assert snapshot["grants"] == expected_grants, (
            f"{revision}: role_permission content mismatch.\n"
            f"Missing (expected but not granted): {sorted(expected_grants - snapshot['grants'])}\n"
            f"Extra (granted but not expected): {sorted(snapshot['grants'] - expected_grants)}"
        )
        expected_permission_codes = {(resource, action) for _, resource, action in expected_grants}
        assert expected_permission_codes <= snapshot["permissions"], (
            f"{revision}: permission table is missing codes the role_permission check "
            f"already confirmed are granted -- {sorted(expected_permission_codes - snapshot['permissions'])}"
        )

        # Prove this exact checkpoint is not a dead end: the rest of the chain must still
        # apply cleanly on top of it.
        _run_alembic(app_url, "upgrade", "head")
    finally:
        await _drop_scratch_database(db_name)
