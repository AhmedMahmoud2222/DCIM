"""Backend tests for Equipment Lifecycle Management and Administrator Lifecycle Settings."""

import asyncio
import importlib.util
import json
import re
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.domain.audit.models import AuditLog
from app.domain.identity.models import (
    DEFAULT_LIFECYCLE_STATES,
    MANDATORY_VISIBLE_LIFECYCLE_TRANSITIONS,
    VISIBLE_LIFECYCLE_STATUSES,
    default_lifecycle_states_config,
)
from app.domain.outbox.models import OutboxEvent
from tests.api._phase2_helpers import create_equipment, create_room


@pytest_asyncio.fixture(autouse=True)
async def _reset_lifecycle_settings(db_engine):
    async with db_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO lifecycle_settings (id, states_config, version) "
                "VALUES (1, CAST(:cfg AS jsonb), 1) "
                "ON CONFLICT (id) DO UPDATE SET states_config = CAST(:cfg AS jsonb), version = 1"
            ),
            {"cfg": json.dumps(default_lifecycle_states_config())},
        )
    yield


async def test_lifecycle_settings_defaults_expose_six_states_and_exclude_reserved(client, auth_headers):
    viewer = await auth_headers("Viewer")
    resp = await client.get("/api/v1/settings/lifecycle", headers=viewer)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    statuses = [s["status"] for s in body["states"]]
    assert statuses == ["planned", "installed", "active", "maintenance", "decommissioned", "removed"]
    assert "reserved" not in statuses
    assert body["legacy_states"] == ["reserved"]
    for item in body["states"]:
        assert "reserved" not in item["allowed_transitions"]


async def test_lifecycle_settings_admin_customization_persists_and_restores_defaults(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    engineer = await auth_headers("Engineer")
    viewer = await auth_headers("Viewer")

    initial = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()
    updated_states = []
    for idx, state in enumerate(reversed(initial["states"]), start=1):
        new_state = dict(state)
        new_state["display_order"] = idx
        new_state["label"] = f"Custom {state['label']}"
        new_state["description"] = f"Updated description for {state['status']}"
        new_state["badge_color"] = "info" if state["status"] == "planned" else state["badge_color"]
        if state["status"] == "planned":
            # Restrict planned -> only installed (disable direct planned -> removed)
            new_state["allowed_transitions"] = ["installed"]
        updated_states.append(new_state)

    # Non-administrators cannot update settings even if they have managed_asset:manage
    forbidden_eng = await client.put("/api/v1/settings/lifecycle", json={"states": updated_states}, headers=engineer)
    assert forbidden_eng.status_code == 403
    forbidden_view = await client.put("/api/v1/settings/lifecycle", json={"states": updated_states}, headers=viewer)
    assert forbidden_view.status_code == 403

    put_resp = await client.put("/api/v1/settings/lifecycle", json={"states": updated_states}, headers=admin)
    assert put_resp.status_code == 200, put_resp.text
    persisted = put_resp.json()["states"]
    assert [s["status"] for s in persisted] == [
        "removed",
        "decommissioned",
        "maintenance",
        "active",
        "installed",
        "planned",
    ]
    planned_cfg = next(s for s in persisted if s["status"] == "planned")
    assert planned_cfg["label"] == "Custom Planned"
    assert planned_cfg["badge_color"] == "info"
    assert planned_cfg["allowed_transitions"] == ["installed"]

    # Verify GET reflects persisted settings
    get_resp = await client.get("/api/v1/settings/lifecycle", headers=viewer)
    assert get_resp.status_code == 200
    assert get_resp.json()["states"] == persisted

    # Verify audit log recorded for update
    update_audits = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "settings.lifecycle.update"))
    ).scalars().all()
    assert len(update_audits) == 1

    # Restore defaults requires Administrator
    assert (await client.post("/api/v1/settings/lifecycle/restore-defaults", headers=engineer)).status_code == 403
    restore_resp = await client.post("/api/v1/settings/lifecycle/restore-defaults", headers=admin)
    assert restore_resp.status_code == 200, restore_resp.text
    restored = restore_resp.json()["states"]
    assert [s["status"] for s in restored] == list(VISIBLE_LIFECYCLE_STATUSES)
    assert next(s for s in restored if s["status"] == "planned")["label"] == "Planned"
    assert next(s for s in restored if s["status"] == "planned")["allowed_transitions"] == ["installed", "removed"]


async def test_lifecycle_settings_rejects_reserved_new_deleted_or_mandatory_forbidden_transitions(client, auth_headers):
    admin = await auth_headers("Administrator")
    base = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]

    # 1. Cannot expose reserved as a state
    with_reserved = [dict(s) for s in base]
    with_reserved.append(
        {
            "status": "reserved",
            "label": "Reserved",
            "description": "Legacy",
            "badge_color": "info",
            "display_order": 6,
            "allowed_transitions": ["installed"],
        }
    )
    r1 = await client.put("/api/v1/settings/lifecycle", json={"states": with_reserved}, headers=admin)
    assert r1.status_code == 422
    assert "reserved" in r1.text.lower()

    # 2. Cannot delete an existing state
    missing_one = [dict(s) for s in base if s["status"] != "maintenance"]
    r2 = await client.put("/api/v1/settings/lifecycle", json={"states": missing_one}, headers=admin)
    assert r2.status_code == 422

    # 3. Cannot create a new state
    invented = [dict(s) for s in base]
    invented[-1] = {**invented[-1], "status": "quarantined"}
    r3 = await client.put("/api/v1/settings/lifecycle", json={"states": invented}, headers=admin)
    assert r3.status_code == 422

    # 4. Cannot allow transitions forbidden by mandatory backend restrictions (e.g. planned -> active or removed -> active)
    forbidden_transition = [dict(s) for s in base]
    for s in forbidden_transition:
        if s["status"] == "planned":
            s["allowed_transitions"] = ["installed", "active"]
    r4 = await client.put("/api/v1/settings/lifecycle", json={"states": forbidden_transition}, headers=admin)
    assert r4.status_code == 422
    assert "mandatory backend restrictions" in r4.text.lower()

    # 5. Cannot target reserved in allowed_transitions
    target_reserved = [dict(s) for s in base]
    for s in target_reserved:
        if s["status"] == "planned":
            s["allowed_transitions"] = ["installed", "reserved"]
    r5 = await client.put("/api/v1/settings/lifecycle", json={"states": target_reserved}, headers=admin)
    assert r5.status_code == 422
    assert "reserved" in r5.text.lower()


async def test_equipment_lifecycle_transitions_mandatory_reason_and_history(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    viewer = await auth_headers("Viewer")
    eq = await create_equipment(client, admin, auth_headers)
    eq_id = eq["id"]

    # Viewer cannot transition
    r_unauth = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "installed"},
        headers=viewer,
    )
    assert r_unauth.status_code == 403

    # Invalid jump planned -> active fails
    r_invalid = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "active"},
        headers=admin,
    )
    assert r_invalid.status_code == 422

    # planned -> installed -> active -> maintenance -> active
    for target, reason in [
        ("installed", "Racked in Hall A"),
        ("active", "Commissioned into production"),
        ("maintenance", "Firmware upgrade window"),
        ("active", "Maintenance complete"),
    ]:
        resp = await client.post(
            f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
            json={"to_status": target, "reason": reason},
            headers=admin,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["lifecycle_status"] == target

    # Decommissioning equipment WITHOUT reason or with blank reason MUST fail (422)
    r_no_reason = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "decommissioned"},
        headers=admin,
    )
    assert r_no_reason.status_code == 422
    r_blank_reason = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "decommissioned", "reason": "   "},
        headers=admin,
    )
    assert r_blank_reason.status_code == 422

    # Decommissioning equipment WITH reason succeeds
    r_decomm = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "decommissioned", "reason": "End of hardware support"},
        headers=admin,
    )
    assert r_decomm.status_code == 200, r_decomm.text
    assert r_decomm.json()["lifecycle_status"] == "decommissioned"

    # Removing equipment WITHOUT reason fails, WITH reason succeeds
    r_rem_no_reason = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "removed"},
        headers=admin,
    )
    assert r_rem_no_reason.status_code == 422

    r_rem = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "removed", "reason": "Physically unmounted and disposed"},
        headers=admin,
    )
    assert r_rem.status_code == 200, r_rem.text
    assert r_rem.json()["lifecycle_status"] == "removed"

    # Verify lifecycle history endpoint returns all 6 transitions in reverse chronological order
    hist_resp = await client.get(f"/api/v1/managed-assets/{eq_id}/lifecycle-history", headers=viewer)
    assert hist_resp.status_code == 200, hist_resp.text
    items = hist_resp.json()["items"]
    assert len(items) == 6
    assert items[0]["from_status"] == "decommissioned"
    assert items[0]["to_status"] == "removed"
    assert items[0]["reason"] == "Physically unmounted and disposed"
    assert items[0]["actor_name"] is not None
    assert items[-1]["from_status"] == "planned"
    assert items[-1]["to_status"] == "installed"
    assert items[-1]["reason"] == "Racked in Hall A"

    # Verify outbox events were created for each transition
    outbox_rows = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == uuid.UUID(eq_id),
                OutboxEvent.event_type == "ManagedAssetLifecycleChanged",
            )
        )
    ).scalars().all()
    assert len(outbox_rows) == 6


async def test_configured_transition_restriction_is_enforced_by_backend(client, auth_headers):
    admin = await auth_headers("Administrator")
    eq = await create_equipment(client, admin, auth_headers)
    eq_id = eq["id"]

    # Disable planned -> removed in Lifecycle Settings
    base = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]
    for s in base:
        if s["status"] == "planned":
            s["allowed_transitions"] = ["installed"]
    put_resp = await client.put("/api/v1/settings/lifecycle", json={"states": base}, headers=admin)
    assert put_resp.status_code == 200

    # Attempt planned -> removed now fails with 422 because admin disabled it
    r_disabled = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "removed", "reason": "Cancelled order"},
        headers=admin,
    )
    assert r_disabled.status_code == 422
    assert "disabled by lifecycle settings" in r_disabled.text.lower()


async def test_legacy_reserved_equipment_remains_readable_and_is_not_silently_converted(
    client, auth_headers, db_session
):
    admin = await auth_headers("Administrator")
    eq = await create_equipment(client, admin, auth_headers)
    eq_id = eq["id"]

    # Simulate an existing legacy record with lifecycle_status = 'reserved'
    await db_session.execute(
        text("UPDATE managed_asset SET lifecycle_status = 'reserved' WHERE id = :id"),
        {"id": uuid.UUID(eq_id)},
    )
    await db_session.commit()

    # Record remains readable in equipment detail and listing
    detail = await client.get(f"/api/v1/equipment/{eq_id}", headers=admin)
    assert detail.status_code == 200
    assert detail.json()["lifecycle_status"] == "reserved"

    listing = await client.get("/api/v1/equipment", headers=admin)
    assert listing.status_code == 200
    found = next(item for item in listing.json()["items"] if item["id"] == eq_id)
    assert found["lifecycle_status"] == "reserved"

    # Editing equipment metadata does NOT silently convert `reserved` to another status
    patched = await client.patch(
        f"/api/v1/equipment/{eq_id}",
        json={"hostname": "legacy-reserved-host", "owner": "NetOps", "notes": "Preserved legacy record"},
        headers={**admin, "If-Match": str(detail.json()["version"])},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["hostname"] == "legacy-reserved-host"
    assert patched.json()["lifecycle_status"] == "reserved"


async def test_placement_and_metadata_updates_never_auto_transition_installed_to_active(client, auth_headers):
    admin = await auth_headers("Administrator")
    room_id = await create_room(client, auth_headers)
    eq = await create_equipment(client, admin, auth_headers)
    eq_id = eq["id"]

    # Transition planned -> installed
    r = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "installed"},
        headers=admin,
    )
    assert r.status_code == 200

    # Moving/placing an installed device must not automatically transition it to active
    moved = await client.post(
        f"/api/v1/equipment/{eq_id}/move",
        json={"placement_type": "floor_standing", "room_id": room_id},
        headers=admin,
    )
    assert moved.status_code == 200
    assert moved.json()["lifecycle_status"] == "installed"


async def test_lifecycle_settings_do_not_restrict_non_equipment_and_accept_null_reason(client, auth_headers):
    admin = await auth_headers("Administrator")
    base = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]
    for s in base:
        if s["status"] == "planned":
            s["allowed_transitions"] = ["installed"]  # disable planned -> removed for equipment
    assert (await client.put("/api/v1/settings/lifecycle", json={"states": base}, headers=admin)).status_code == 200

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
    assert (await client.put("/api/v1/settings/lifecycle", json={"states": first}, headers=admin)).status_code == 200
    assert (await client.post("/api/v1/settings/lifecycle/restore-defaults", headers=admin)).status_code == 200
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
    """F4 / Section B regression: concurrent lifecycle settings updates and restores enforce optimistic
    concurrency (`If-Match` / `version`), prevent lost updates via PostgreSQL `FOR UPDATE` row
    locking, and record the actual prior committed state in `audit_log`."""
    admin = await auth_headers("Administrator")
    initial = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()
    v1 = initial["version"]
    assert v1 >= 1

    states_a = [dict(s) for s in initial["states"]]
    states_a[0] = {**states_a[0], "label": "Planned Wave A"}

    # First update with If-Match: v1 succeeds and increments version to v1 + 1
    r_a = await client.put(
        "/api/v1/settings/lifecycle",
        json={"states": states_a, "version": v1},
        headers={**admin, "If-Match": str(v1)},
    )
    assert r_a.status_code == 200, r_a.text
    v2 = r_a.json()["version"]
    assert v2 == v1 + 1

    # Stale update using old version v1 (via If-Match or body.version) is rejected with 409 Conflict
    states_b = [dict(s) for s in initial["states"]]
    states_b[0] = {**states_b[0], "label": "Stale Overwrite Attempt"}
    r_stale_header = await client.put(
        "/api/v1/settings/lifecycle",
        json={"states": states_b},
        headers={**admin, "If-Match": str(v1)},
    )
    assert r_stale_header.status_code == 409

    r_stale_body = await client.put(
        "/api/v1/settings/lifecycle",
        json={"states": states_b, "version": v1},
        headers=admin,
    )
    assert r_stale_body.status_code == 409

    # Mismatched If-Match header vs body version is rejected with 400 Bad Request
    r_mismatch = await client.put(
        "/api/v1/settings/lifecycle",
        json={"states": states_b, "version": v2},
        headers={**admin, "If-Match": str(v1)},
    )
    assert r_mismatch.status_code == 400

    # Stale restore-defaults using old version v1 is also rejected with 409 Conflict
    r_stale_restore = await client.post(
        "/api/v1/settings/lifecycle/restore-defaults",
        json={"version": v1},
        headers={**admin, "If-Match": str(v1)},
    )
    assert r_stale_restore.status_code == 409

    # Release the shared test `db_session` transaction lock held after the 409 rejection
    # before opening two independent concurrent PostgreSQL sessions.
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

    # Restore with current version v2 + 2 succeeds and records the actual prior committed state in audit_log
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


@pytest.mark.parametrize(
    "asset_type",
    ["rack", "pdu", "ups", "crac", "sensor", "power_panel"],
)
async def test_f7_cross_asset_lifecycle_settings_apply_only_to_equipment(
    client, auth_headers, asset_type
):
    """F7 regression: administrator-configured equipment lifecycle restrictions apply only to
    equipment and do not restrict racks, PDUs, UPS systems, cooling units, sensors, or other assets."""
    admin = await auth_headers("Administrator")
    base = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]
    restricted = []
    for s in base:
        item = dict(s)
        item["allowed_transitions"] = []
        restricted.append(item)
    put_resp = await client.put("/api/v1/settings/lifecycle", json={"states": restricted}, headers=admin)
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
    """Section D regression: migration 0061 must not import mutable application constants from `app.*`,
    and frontend/backend default transitions must match."""
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

    fe_defaults_path = (
        repo_root / "frontend" / "src" / "features" / "managed-assets" / "lifecycleDefaults.ts"
    )
    fe_source = fe_defaults_path.read_text(encoding="utf-8")
    for status_key, expected_targets in MANDATORY_VISIBLE_LIFECYCLE_TRANSITIONS.items():
        pattern = (
            rf'status:\s*"{status_key}".*?allowed_transitions:\s*\[([^\]]*)\]'
        )
        m = re.search(pattern, fe_source, flags=re.DOTALL)
        assert m is not None, f"Missing {status_key} in frontend lifecycleDefaults.ts"
        fe_targets = [
            t.strip().strip('"').strip("'")
            for t in m.group(1).split(",")
            if t.strip()
        ]
        assert fe_targets == expected_targets

