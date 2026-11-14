"""Backend tests for Equipment Lifecycle Management and Administrator Lifecycle Settings."""

import uuid

from sqlalchemy import select, text

from app.domain.audit.models import AuditLog
from app.domain.identity.models import VISIBLE_LIFECYCLE_STATUSES
from app.domain.outbox.models import OutboxEvent
from tests.api._phase2_helpers import create_equipment, create_room


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
