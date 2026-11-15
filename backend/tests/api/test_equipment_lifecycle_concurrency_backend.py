"""Backend tests for Lifecycle Settings concurrency, cross-asset isolation, legacy states, and migration 0061."""

import asyncio
import importlib.util
import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.audit.models import AuditLog
from app.domain.identity.models import DEFAULT_LIFECYCLE_STATES, MANDATORY_VISIBLE_LIFECYCLE_TRANSITIONS
from tests.api._lifecycle_helpers import (
    put_lifecycle_states,
    reset_lifecycle_settings_row,
    restore_lifecycle_defaults,
)
from tests.api._phase2_helpers import create_equipment


@pytest_asyncio.fixture(autouse=True)
async def _reset_lifecycle_settings(db_engine):
    await reset_lifecycle_settings_row(db_engine)
    yield


async def test_lifecycle_settings_writes_require_a_version_precondition(client, auth_headers):
    admin = await auth_headers("Administrator")
    initial = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()
    states = initial["states"]

    # Absent precondition -> 428 on both write endpoints, and nothing changes.
    put_absent = await client.put("/api/v1/settings/lifecycle", json={"states": states}, headers=admin)
    assert put_absent.status_code == 428, put_absent.text
    restore_absent = await client.post("/api/v1/settings/lifecycle/restore-defaults", headers=admin)
    assert restore_absent.status_code == 428, restore_absent.text
    restore_empty_body = await client.post("/api/v1/settings/lifecycle/restore-defaults", json={}, headers=admin)
    assert restore_empty_body.status_code == 428, restore_empty_body.text
    assert (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["version"] == initial["version"]

    # Stale version -> 409 on both endpoints.
    stale = initial["version"] + 5
    put_stale = await client.put(
        "/api/v1/settings/lifecycle", json={"states": states}, headers={**admin, "If-Match": str(stale)}
    )
    assert put_stale.status_code == 409
    restore_stale = await client.post(
        "/api/v1/settings/lifecycle/restore-defaults", headers={**admin, "If-Match": str(stale)}
    )
    assert restore_stale.status_code == 409

    # Body `version` alone satisfies the precondition.
    ok = await client.put(
        "/api/v1/settings/lifecycle", json={"states": states, "version": initial["version"]}, headers=admin
    )
    assert ok.status_code == 200, ok.text


async def test_lifecycle_settings_do_not_restrict_non_equipment_and_accept_null_reason(client, auth_headers):
    admin = await auth_headers("Administrator")
    base = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]
    for s in base:
        if s["status"] == "planned":
            s["allowed_transitions"] = ["installed"]  # disable planned -> removed for equipment
    assert (await put_lifecycle_states(client, admin, base)).status_code == 200

    created = await client.post(
        "/api/v1/managed-assets",
        json={"asset_type": "rack", "asset_tag": f"RK-{uuid.uuid4().hex[:8]}"},
        headers=admin,
    )
    assert created.status_code == 201, created.text
    resp = await client.post(
        f"/api/v1/managed-assets/{created.json()['id']}/lifecycle-transition",
        json={"to_status": "removed", "reason": None},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text


async def test_transition_out_of_legacy_reserved_equipment_is_rejected(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    eq = await create_equipment(client, admin, auth_headers)
    await db_session.execute(
        text("UPDATE managed_asset SET lifecycle_status = 'reserved' WHERE id = :id"),
        {"id": uuid.UUID(eq["id"])},
    )
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/managed-assets/{eq['id']}/lifecycle-transition",
        json={"to_status": "installed"},
        headers=admin,
    )
    assert resp.status_code == 422
    assert "legacy" in resp.text.lower()


async def test_lifecycle_settings_restore_audit_before_reflects_prior_update(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    first = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]
    for s in first:
        if s["status"] == "active":
            s["label"] = "In Service"
    assert (await put_lifecycle_states(client, admin, first)).status_code == 200
    assert (await restore_lifecycle_defaults(client, admin)).status_code == 200
    row = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "settings.lifecycle.restore_defaults")
            .order_by(AuditLog.timestamp.desc())
        )
    ).scalars().first()
    before_active = next(s for s in row.before["states"] if s["status"] == "active")
    assert before_active["label"] == "In Service"


async def test_f4_lifecycle_settings_optimistic_concurrency_and_postgres_row_locking(
    client, auth_headers, db_session, db_engine
):
    """Concurrent updates enforce optimistic concurrency (If-Match/version), FOR UPDATE row locking, and audit_log."""
    admin = await auth_headers("Administrator")
    initial = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()
    v1 = initial["version"]
    assert v1 >= 1

    states_a = [dict(s) for s in initial["states"]]
    states_a[0] = {**states_a[0], "label": "Planned Wave A"}

    r_a = await client.put(
        "/api/v1/settings/lifecycle",
        json={"states": states_a, "version": v1},
        headers={**admin, "If-Match": str(v1)},
    )
    assert r_a.status_code == 200, r_a.text
    v2 = r_a.json()["version"]
    assert v2 == v1 + 1

    states_b = [dict(s) for s in initial["states"]]
    states_b[0] = {**states_b[0], "label": "Stale Overwrite Attempt"}
    assert (
        await client.put("/api/v1/settings/lifecycle", json={"states": states_b}, headers={**admin, "If-Match": str(v1)})
    ).status_code == 409
    assert (
        await client.put("/api/v1/settings/lifecycle", json={"states": states_b, "version": v1}, headers=admin)
    ).status_code == 409
    assert (
        await client.put(
            "/api/v1/settings/lifecycle", json={"states": states_b, "version": v2}, headers={**admin, "If-Match": str(v1)}
        )
    ).status_code == 400
    assert (
        await client.post(
            "/api/v1/settings/lifecycle/restore-defaults", json={"version": v1}, headers={**admin, "If-Match": str(v1)}
        )
    ).status_code == 409

    await db_session.rollback()

    from app.application.lifecycle_settings import effective_states_from_row, lock_lifecycle_settings_row

    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def _concurrent_tx(label_suffix: str) -> int:
        async with session_factory() as session:
            row = await lock_lifecycle_settings_row(session)
            before_states = effective_states_from_row(row)
            new_states = [dict(s) for s in before_states]
            new_states[0] = {**new_states[0], "label": f"Concurrent {label_suffix}"}
            row.states_config = new_states
            row.version = row.version + 1
            await session.commit()
            return row.version

    v_res_1, v_res_2 = await asyncio.gather(_concurrent_tx("One"), _concurrent_tx("Two"))
    assert sorted([v_res_1, v_res_2]) == [v2 + 1, v2 + 2]

    r_restore = await client.post(
        "/api/v1/settings/lifecycle/restore-defaults",
        json={"version": v2 + 2},
        headers={**admin, "If-Match": str(v2 + 2)},
    )
    assert r_restore.status_code == 200, r_restore.text
    assert r_restore.json()["version"] == v2 + 3

    restore_audit = (
        await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "settings.lifecycle.restore_defaults")
            .order_by(AuditLog.timestamp.desc(), AuditLog.audit_id.desc())
        )
    ).scalars().first()
    assert restore_audit is not None
    assert restore_audit.before["version"] == v2 + 2
    assert restore_audit.before["states"][0]["label"] in ("Concurrent One", "Concurrent Two")
    assert restore_audit.after["version"] == v2 + 3
    assert restore_audit.after["states"][0]["label"] == "Planned"


@pytest.mark.parametrize("asset_type", ["rack", "pdu", "ups", "crac", "sensor", "power_panel"])
async def test_f7_cross_asset_lifecycle_settings_apply_only_to_equipment(client, auth_headers, asset_type):
    admin = await auth_headers("Administrator")
    base = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]
    restricted = [{**s, "allowed_transitions": []} for s in base]
    put_resp = await put_lifecycle_states(client, admin, restricted)
    assert put_resp.status_code == 200, put_resp.text

    create_resp = await client.post(
        "/api/v1/managed-assets",
        json={"asset_type": asset_type, "asset_tag": f"{asset_type.upper()}-{uuid.uuid4().hex[:8]}"},
        headers=admin,
    )
    assert create_resp.status_code == 201, create_resp.text
    asset_id = create_resp.json()["id"]

    for target in ("installed", "active", "maintenance", "decommissioned", "removed"):
        t_resp = await client.post(
            f"/api/v1/managed-assets/{asset_id}/lifecycle-transition",
            json={"to_status": target},
            headers=admin,
        )
        assert t_resp.status_code == 200, f"{asset_type} failed transition to {target}: {t_resp.text}"
        assert t_resp.json()["lifecycle_status"] == target


def test_migration_0061_seed_is_self_contained_and_matches_defaults():
    repo_root = Path(__file__).resolve().parents[3]
    mig_path = repo_root / "backend" / "migrations" / "versions" / "0061_lifecycle_settings.py"
    mig_source = mig_path.read_text(encoding="utf-8")
    assert "from app." not in mig_source
    assert "import app." not in mig_source

    spec = importlib.util.spec_from_file_location("mig_0061", mig_path)
    assert spec is not None and spec.loader is not None
    mig_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig_mod)
    assert mig_mod._SEED_LIFECYCLE_STATES == DEFAULT_LIFECYCLE_STATES

    fe_defaults_path = repo_root / "frontend" / "src" / "features" / "managed-assets" / "lifecycleDefaults.ts"
    fe_source = fe_defaults_path.read_text(encoding="utf-8")
    for status_key, expected_targets in MANDATORY_VISIBLE_LIFECYCLE_TRANSITIONS.items():
        pattern = rf'status:\s*"{status_key}".*?allowed_transitions:\s*\[([^\]]*)\]'
        m = re.search(pattern, fe_source, flags=re.DOTALL)
        assert m is not None, f"Missing {status_key} in frontend lifecycleDefaults.ts"
        fe_targets = [t.strip().strip('"').strip("'") for t in m.group(1).split(",") if t.strip()]
        assert fe_targets == expected_targets
