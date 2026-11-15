"""Backend tests for Equipment Lifecycle Management UI flows and Administrator Settings."""

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.api.v1.managed_assets import get_managed_asset, get_managed_asset_lifecycle_history
from app.application.access_control import UNRESTRICTED, AccessScope, site_id_for_room
from app.core.errors import NotFoundError
from app.domain.audit.models import AuditLog
from app.domain.identity.models import VISIBLE_LIFECYCLE_STATUSES
from app.domain.outbox.models import OutboxEvent
from tests.api._lifecycle_helpers import (
    put_lifecycle_states,
    reset_lifecycle_settings_row,
    restore_lifecycle_defaults,
)
from tests.api._phase2_helpers import create_equipment, create_rack, create_room


@pytest_asyncio.fixture(autouse=True)
async def _reset_lifecycle_settings(db_engine):
    await reset_lifecycle_settings_row(db_engine)
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
            new_state["allowed_transitions"] = ["installed"]
        updated_states.append(new_state)

    # Non-administrators cannot update settings even if they have managed_asset:manage
    assert (await client.put("/api/v1/settings/lifecycle", json={"states": updated_states}, headers=engineer)).status_code == 403
    assert (await client.put("/api/v1/settings/lifecycle", json={"states": updated_states}, headers=viewer)).status_code == 403

    put_resp = await put_lifecycle_states(client, admin, updated_states)
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

    get_resp = await client.get("/api/v1/settings/lifecycle", headers=viewer)
    assert get_resp.status_code == 200
    assert get_resp.json()["states"] == persisted

    update_audits = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "settings.lifecycle.update"))
    ).scalars().all()
    assert len(update_audits) == 1

    assert (await client.post("/api/v1/settings/lifecycle/restore-defaults", headers=engineer)).status_code == 403
    restore_resp = await restore_lifecycle_defaults(client, admin)
    assert restore_resp.status_code == 200, restore_resp.text
    restored = restore_resp.json()["states"]
    assert [s["status"] for s in restored] == list(VISIBLE_LIFECYCLE_STATUSES)
    assert next(s for s in restored if s["status"] == "planned")["label"] == "Planned"
    assert next(s for s in restored if s["status"] == "planned")["allowed_transitions"] == ["installed", "removed"]


async def test_lifecycle_settings_rejects_reserved_new_deleted_or_mandatory_forbidden_transitions(client, auth_headers):
    admin = await auth_headers("Administrator")
    base = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]

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
    r1 = await put_lifecycle_states(client, admin, with_reserved)
    assert r1.status_code == 422
    assert "reserved" in r1.text.lower()

    missing_one = [dict(s) for s in base if s["status"] != "maintenance"]
    assert (await put_lifecycle_states(client, admin, missing_one)).status_code == 422

    invented = [dict(s) for s in base]
    invented[-1] = {**invented[-1], "status": "quarantined"}
    assert (await put_lifecycle_states(client, admin, invented)).status_code == 422

    forbidden_transition = [dict(s) for s in base]
    for s in forbidden_transition:
        if s["status"] == "planned":
            s["allowed_transitions"] = ["installed", "active"]
    r4 = await put_lifecycle_states(client, admin, forbidden_transition)
    assert r4.status_code == 422
    assert "mandatory backend restrictions" in r4.text.lower()

    target_reserved = [dict(s) for s in base]
    for s in target_reserved:
        if s["status"] == "planned":
            s["allowed_transitions"] = ["installed", "reserved"]
    r5 = await put_lifecycle_states(client, admin, target_reserved)
    assert r5.status_code == 422
    assert "reserved" in r5.text.lower()


async def test_equipment_lifecycle_transitions_mandatory_reason_and_history(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    viewer = await auth_headers("Viewer")
    eq = await create_equipment(client, admin, auth_headers)
    eq_id = eq["id"]

    r_unauth = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "installed"},
        headers=viewer,
    )
    assert r_unauth.status_code == 403

    r_invalid = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "active"},
        headers=admin,
    )
    assert r_invalid.status_code == 422

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

    # Decommissioning and removal require non-blank reason for equipment
    assert (
        await client.post(f"/api/v1/managed-assets/{eq_id}/lifecycle-transition", json={"to_status": "decommissioned"}, headers=admin)
    ).status_code == 422
    assert (
        await client.post(
            f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
            json={"to_status": "decommissioned", "reason": "   "},
            headers=admin,
        )
    ).status_code == 422

    r_decomm = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "decommissioned", "reason": "End of hardware support"},
        headers=admin,
    )
    assert r_decomm.status_code == 200, r_decomm.text
    assert r_decomm.json()["lifecycle_status"] == "decommissioned"

    assert (
        await client.post(f"/api/v1/managed-assets/{eq_id}/lifecycle-transition", json={"to_status": "removed"}, headers=admin)
    ).status_code == 422
    r_rem = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "removed", "reason": "Physically unmounted and disposed"},
        headers=admin,
    )
    assert r_rem.status_code == 200, r_rem.text
    assert r_rem.json()["lifecycle_status"] == "removed"

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

    outbox_rows = (
        await db_session.execute(
            select(OutboxEvent).where(
                OutboxEvent.aggregate_id == uuid.UUID(eq_id),
                OutboxEvent.event_type == "ManagedAssetLifecycleChanged",
            )
        )
    ).scalars().all()
    assert len(outbox_rows) == 6


async def _assert_both_fail_closed(db_session, scope: AccessScope, asset_id: uuid.UUID) -> None:
    expected = f"ManagedAsset {asset_id} not found."
    for call in (get_managed_asset, get_managed_asset_lifecycle_history):
        with pytest.raises(NotFoundError) as exc_info:
            await call(asset_id=asset_id, db=db_session, ctx=SimpleNamespace(scope=scope))
        assert exc_info.value.detail == expected


async def test_lifecycle_history_enforces_equipment_access_scope_when_restricted(client, auth_headers, db_session):
    """F1 & F2 regression: restricted scope fails closed across equipment, racks, unplaced assets,
    and non-equipment/rack asset types with identical 404 detail, while preserving authorized and
    unrestricted access on GET /managed-assets/{id} and GET /managed-assets/{id}/lifecycle-history."""
    admin = await auth_headers("Administrator")
    viewer = await auth_headers("Viewer")
    room_id = await create_room(client, auth_headers)
    site_id = await site_id_for_room(db_session, uuid.UUID(room_id))
    assert site_id is not None

    placed_eq = await create_equipment(client, admin, auth_headers)
    placed_eq_id = uuid.UUID(placed_eq["id"])
    moved = await client.post(
        f"/api/v1/equipment/{placed_eq['id']}/move",
        json={"placement_type": "floor_standing", "room_id": room_id},
        headers=admin,
    )
    assert moved.status_code == 200, moved.text
    placed_rack = await create_rack(client, admin, auth_headers, room_id=room_id)
    placed_rack_id = uuid.UUID(placed_rack["id"])
    unplaced_eq_id = uuid.UUID((await create_equipment(client, admin, auth_headers))["id"])
    unplaced_rack_id = uuid.UUID((await create_rack(client, admin, auth_headers))["id"])

    wrong_scope = AccessScope(unrestricted=False, site_ids=frozenset({uuid.uuid4()}), full_site_ids=frozenset({uuid.uuid4()}))
    right_scope = AccessScope(unrestricted=False, site_ids=frozenset({site_id}), full_site_ids=frozenset({site_id}))

    # 1 & 2. Out-of-scope equipment and rack fail closed with identical 404 (no existence leak)
    await _assert_both_fail_closed(db_session, wrong_scope, placed_eq_id)
    await _assert_both_fail_closed(db_session, wrong_scope, placed_rack_id)
    await _assert_both_fail_closed(db_session, right_scope, uuid.uuid4())

    # 3. Assets with no resolvable placement fail closed even for a restricted user with site grants
    await _assert_both_fail_closed(db_session, right_scope, unplaced_eq_id)
    await _assert_both_fail_closed(db_session, right_scope, unplaced_rack_id)

    # 4. Non-equipment/rack assets (PDU, UPS, generator, power_panel, sensor, cable, CRAC, CRAH, chiller)
    other_ids: list[uuid.UUID] = []
    for asset_type in ("pdu", "ups", "generator", "power_panel", "sensor", "cable", "crac", "crah", "chiller"):
        created = await client.post(
            "/api/v1/managed-assets",
            json={"asset_type": asset_type, "asset_tag": f"{asset_type.upper()}-{uuid.uuid4().hex[:6]}"},
            headers=admin,
        )
        assert created.status_code == 201, created.text
        aid = uuid.UUID(created.json()["id"])
        other_ids.append(aid)
        await _assert_both_fail_closed(db_session, right_scope, aid)

    # 5. Authorized restricted access succeeds for in-scope equipment and rack on both endpoints
    assert (await get_managed_asset(asset_id=placed_eq_id, db=db_session, ctx=SimpleNamespace(scope=right_scope))).id == placed_eq_id
    assert (await get_managed_asset(asset_id=placed_rack_id, db=db_session, ctx=SimpleNamespace(scope=right_scope))).id == placed_rack_id
    assert (await get_managed_asset_lifecycle_history(asset_id=placed_eq_id, db=db_session, ctx=SimpleNamespace(scope=right_scope))).items == []
    assert (await get_managed_asset_lifecycle_history(asset_id=placed_rack_id, db=db_session, ctx=SimpleNamespace(scope=right_scope))).items == []

    # 6. Unrestricted users retain access across placed, unplaced, and non-equipment/rack assets
    for aid in (placed_eq_id, placed_rack_id, unplaced_eq_id, unplaced_rack_id, other_ids[0]):
        assert (await get_managed_asset(asset_id=aid, db=db_session, ctx=SimpleNamespace(scope=UNRESTRICTED))).id == aid
        assert (await client.get(f"/api/v1/managed-assets/{aid}", headers=viewer)).status_code == 200
        assert (await client.get(f"/api/v1/managed-assets/{aid}/lifecycle-history", headers=viewer)).status_code == 200


async def test_configured_transition_restriction_is_enforced_by_backend(client, auth_headers):
    admin = await auth_headers("Administrator")
    eq = await create_equipment(client, admin, auth_headers)
    eq_id = eq["id"]

    base = (await client.get("/api/v1/settings/lifecycle", headers=admin)).json()["states"]
    for s in base:
        if s["status"] == "planned":
            s["allowed_transitions"] = ["installed"]
    assert (await put_lifecycle_states(client, admin, base)).status_code == 200

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

    await db_session.execute(
        text("UPDATE managed_asset SET lifecycle_status = 'reserved' WHERE id = :id"),
        {"id": uuid.UUID(eq_id)},
    )
    await db_session.commit()

    detail = await client.get(f"/api/v1/equipment/{eq_id}", headers=admin)
    assert detail.status_code == 200
    assert detail.json()["lifecycle_status"] == "reserved"

    listing = await client.get("/api/v1/equipment", headers=admin)
    assert listing.status_code == 200
    found = next(item for item in listing.json()["items"] if item["id"] == eq_id)
    assert found["lifecycle_status"] == "reserved"

    patched = await client.patch(
        f"/api/v1/equipment/{eq_id}",
        json={"hostname": "legacy-reserved-host", "owner": "NetOps", "notes": "Preserved legacy record"},
        headers={**admin, "If-Match": str(detail.json()["version"])},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["hostname"] == "legacy-reserved-host"
    assert patched.json()["lifecycle_status"] == "reserved"


async def test_patch_equipment_clears_fields_on_explicit_null_and_preserves_omitted(client, auth_headers):
    admin = await auth_headers("Administrator")
    eq = await create_equipment(client, admin, auth_headers)
    eq_id = eq["id"]
    version = (await client.get(f"/api/v1/equipment/{eq_id}", headers=admin)).json()["version"]
    set_resp = await client.patch(
        f"/api/v1/equipment/{eq_id}",
        json={"hostname": "host-a", "owner": "NetOps", "notes": "n"},
        headers={**admin, "If-Match": str(version)},
    )
    assert set_resp.status_code == 200, set_resp.text

    clear_resp = await client.patch(
        f"/api/v1/equipment/{eq_id}",
        json={"hostname": None, "notes": "   "},
        headers={**admin, "If-Match": str(set_resp.json()["version"])},
    )
    assert clear_resp.status_code == 200, clear_resp.text
    assert clear_resp.json()["hostname"] is None
    assert clear_resp.json()["notes"] is None
    assert clear_resp.json()["owner"] == "NetOps"
    reread = (await client.get(f"/api/v1/equipment/{eq_id}", headers=admin)).json()
    assert reread["hostname"] is None
    assert reread["owner"] == "NetOps"


async def test_placement_and_metadata_updates_never_auto_transition_installed_to_active(client, auth_headers):
    admin = await auth_headers("Administrator")
    room_id = await create_room(client, auth_headers)
    eq = await create_equipment(client, admin, auth_headers)
    eq_id = eq["id"]

    r = await client.post(
        f"/api/v1/managed-assets/{eq_id}/lifecycle-transition",
        json={"to_status": "installed"},
        headers=admin,
    )
    assert r.status_code == 200

    moved = await client.post(
        f"/api/v1/equipment/{eq_id}/move",
        json={"placement_type": "floor_standing", "room_id": room_id},
        headers=admin,
    )
    assert moved.status_code == 200
    assert moved.json()["lifecycle_status"] == "installed"
