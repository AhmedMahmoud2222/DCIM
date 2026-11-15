"""Authorization, visibility, and concurrency regression tests for /api/v1/managed-assets."""

import asyncio
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.application.access_control as access_control
from app.db.session import get_db
from app.domain.audit.models import AuditLog
from app.domain.identity.models import ManagedAsset
from app.domain.outbox.models import OutboxEvent
from app.main import app
from tests.api._phase2_helpers import create_equipment, create_rack

PW = "correct horse battery staple"


async def _make_site_and_room(client: AsyncClient, admin: dict) -> dict:
    org = (
        await client.post("/api/v1/organizations", json={"name": f"Org-{uuid.uuid4().hex[:8]}"}, headers=admin)
    ).json()
    country = (
        await client.post(
            "/api/v1/countries",
            json={"organization_id": org["id"], "name": "Testland", "iso_code": "TL"},
            headers=admin,
        )
    ).json()
    city = (
        await client.post(
            "/api/v1/cities", json={"country_id": country["id"], "name": "Testville"}, headers=admin
        )
    ).json()
    site = (
        await client.post(
            "/api/v1/sites",
            json={"city_id": city["id"], "code": f"S-{uuid.uuid4().hex[:6]}", "name": "Test Site"},
            headers=admin,
        )
    ).json()
    building = (
        await client.post(
            "/api/v1/buildings",
            json={"site_id": site["id"], "code": "A", "name": "Building A"},
            headers=admin,
        )
    ).json()
    floor = (
        await client.post(
            "/api/v1/floors",
            json={"building_id": building["id"], "name": "Floor 1", "level_number": 1},
            headers=admin,
        )
    ).json()
    room = (
        await client.post(
            "/api/v1/rooms",
            json={"floor_id": floor["id"], "code": f"R-{uuid.uuid4().hex[:6]}", "name": "Test Room"},
            headers=admin,
        )
    ).json()
    return {"site_id": site["id"], "room_id": room["id"]}


async def _place_equipment_in_rack(
    client: AsyncClient, admin: dict, equipment_id: str, room_id: str, rack_id: str
) -> None:
    resp = await client.post(
        f"/api/v1/equipment/{equipment_id}/move",
        json={
            "placement_type": "rack_mounted",
            "room_id": room_id,
            "rack_id": rack_id,
            "u_start": 1,
            "u_end": 2,
            "side": "front",
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text


async def _create_scoped_group_user(
    client: AsyncClient,
    admin: dict,
    *,
    allow: list[str],
    sites: list[dict],
) -> dict:
    group_resp = await client.post(
        "/api/v1/groups", json={"name": f"Scoped-{uuid.uuid4().hex[:8]}"}, headers=admin
    )
    assert group_resp.status_code == 201, group_resp.text
    group_id = group_resp.json()["id"]

    perm_resp = await client.put(
        f"/api/v1/groups/{group_id}/permissions",
        json={"allow": allow, "deny": []},
        headers=admin,
    )
    assert perm_resp.status_code == 200, perm_resp.text

    site_resp = await client.put(
        f"/api/v1/groups/{group_id}/site-access",
        json={"sites": sites},
        headers=admin,
    )
    assert site_resp.status_code == 200, site_resp.text

    email = f"scoped-{uuid.uuid4().hex[:8]}@example.com"
    user_resp = await client.post(
        "/api/v1/users",
        json={"email": email, "full_name": "Scoped User", "password": PW, "group_ids": [group_id]},
        headers=admin,
    )
    assert user_resp.status_code == 201, user_resp.text

    login_resp = await client.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert login_resp.status_code == 200, login_resp.text
    return {"Authorization": f"Bearer {login_resp.json()['access_token']}"}


async def _audit_and_outbox_counts(db_session, asset_id: uuid.UUID) -> tuple[int, int]:
    audit_count = (
        await db_session.execute(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.entity_type == "managed_asset", AuditLog.entity_id == asset_id)
        )
    ).scalar_one()
    outbox_count = (
        await db_session.execute(
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.aggregate_type == "managed_asset", OutboxEvent.aggregate_id == asset_id)
        )
    ).scalar_one()
    return audit_count, outbox_count


@pytest.mark.asyncio
async def test_restricted_group_user_blocked_by_sec_arch_inactive_permissions(
    client: AsyncClient, auth_headers
):
    admin = await auth_headers("Administrator")
    site_a = await _make_site_and_room(client, admin)
    rack_a = await create_rack(client, admin, auth_headers, room_id=site_a["room_id"])

    restricted = await _create_scoped_group_user(
        client,
        admin,
        allow=["managed_asset:read", "managed_asset:update_lifecycle"],
        sites=[{"site_id": site_a["site_id"], "rack_scope": "all"}],
    )

    assert (await client.get("/api/v1/managed-assets", headers=restricted)).status_code == 403
    assert (await client.get(f"/api/v1/managed-assets/{rack_a['id']}", headers=restricted)).status_code == 403
    assert (
        await client.post(
            f"/api/v1/managed-assets/{rack_a['id']}/lifecycle-transition",
            json={"to_status": "installed"},
            headers=restricted,
        )
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/managed-assets/{rack_a['id']}/lifecycle-history", headers=restricted)
    ).status_code == 403


@pytest.mark.asyncio
async def test_missing_permission_returns_403(client: AsyncClient, auth_headers):
    admin = await auth_headers("Administrator")
    viewer = await auth_headers("Viewer")
    me = (await client.get("/api/v1/auth/me", headers=viewer)).json()

    deny_group = (
        await client.post("/api/v1/groups", json={"name": f"Deny-{uuid.uuid4().hex[:8]}"}, headers=admin)
    ).json()["id"]
    assert (
        await client.put(
            f"/api/v1/groups/{deny_group}/permissions",
            json={"allow": [], "deny": ["managed_asset:read", "managed_asset:update_lifecycle"]},
            headers=admin,
        )
    ).status_code == 200
    assert (
        await client.put(
            f"/api/v1/groups/{deny_group}/members",
            json={"user_ids": [me["id"]]},
            headers=admin,
        )
    ).status_code == 200

    fake_id = str(uuid.uuid4())
    assert (await client.get("/api/v1/managed-assets", headers=viewer)).status_code == 403
    assert (await client.get(f"/api/v1/managed-assets/{fake_id}", headers=viewer)).status_code == 403
    assert (
        await client.post(
            f"/api/v1/managed-assets/{fake_id}/lifecycle-transition",
            json={"to_status": "installed"},
            headers=viewer,
        )
    ).status_code == 403
    assert (
        await client.get(f"/api/v1/managed-assets/{fake_id}/lifecycle-history", headers=viewer)
    ).status_code == 403


@pytest.mark.asyncio
async def test_scoped_list_visibility_counts_and_pagination(
    client: AsyncClient, auth_headers, monkeypatch
):
    monkeypatch.setattr(
        access_control,
        "SCOPE_AWARE_PERMISSIONS",
        access_control.SCOPE_AWARE_PERMISSIONS | {"managed_asset:read", "managed_asset:update_lifecycle"},
    )

    admin = await auth_headers("Administrator")
    site_a = await _make_site_and_room(client, admin)
    site_b = await _make_site_and_room(client, admin)

    rack_a1 = await create_rack(client, admin, auth_headers, room_id=site_a["room_id"])
    rack_a2 = await create_rack(client, admin, auth_headers, room_id=site_a["room_id"])
    rack_b1 = await create_rack(client, admin, auth_headers, room_id=site_b["room_id"])

    eq_a1 = await create_equipment(client, admin, auth_headers)
    await _place_equipment_in_rack(client, admin, eq_a1["id"], site_a["room_id"], rack_a1["id"])
    eq_a2 = await create_equipment(client, admin, auth_headers)
    await _place_equipment_in_rack(client, admin, eq_a2["id"], site_a["room_id"], rack_a2["id"])
    eq_b1 = await create_equipment(client, admin, auth_headers)
    await _place_equipment_in_rack(client, admin, eq_b1["id"], site_b["room_id"], rack_b1["id"])
    eq_unplaced = await create_equipment(client, admin, auth_headers)

    pdu_resp = await client.post(
        "/api/v1/managed-assets",
        json={"asset_type": "pdu", "asset_tag": f"PDU-{uuid.uuid4().hex[:8]}"},
        headers=admin,
    )
    assert pdu_resp.status_code == 201, pdu_resp.text
    pdu_id = pdu_resp.json()["id"]

    # Unrestricted admin sees all 8 assets and accurate pagination.
    admin_all = await client.get("/api/v1/managed-assets", params={"limit": 50}, headers=admin)
    assert admin_all.status_code == 200
    assert admin_all.json()["total"] == 8
    admin_ids = {item["id"] for item in admin_all.json()["items"]}
    assert admin_ids == {
        rack_a1["id"], rack_a2["id"], rack_b1["id"],
        eq_a1["id"], eq_a2["id"], eq_b1["id"], eq_unplaced["id"], pdu_id,
    }

    # Site-A-scoped user sees only Site A's 2 racks and 2 placed equipment (total=4).
    site_a_user = await _create_scoped_group_user(
        client,
        admin,
        allow=["managed_asset:read", "managed_asset:update_lifecycle"],
        sites=[{"site_id": site_a["site_id"], "rack_scope": "all"}],
    )
    page1 = await client.get("/api/v1/managed-assets", params={"limit": 2, "offset": 0}, headers=site_a_user)
    page2 = await client.get("/api/v1/managed-assets", params={"limit": 2, "offset": 2}, headers=site_a_user)
    assert page1.status_code == 200 and page2.status_code == 200
    assert page1.json()["total"] == 4 and page2.json()["total"] == 4
    assert len(page1.json()["items"]) == 2 and len(page2.json()["items"]) == 2
    seen_site_a = {item["id"] for item in page1.json()["items"] + page2.json()["items"]}
    assert seen_site_a == {rack_a1["id"], rack_a2["id"], eq_a1["id"], eq_a2["id"]}

    # Unsupported asset type (pdu) fails closed for restricted callers.
    pdu_list = await client.get("/api/v1/managed-assets", params={"asset_type": "pdu"}, headers=site_a_user)
    assert pdu_list.status_code == 200
    assert pdu_list.json()["total"] == 0 and pdu_list.json()["items"] == []

    # Rack-selected user scoped only to rack_a1 sees only rack_a1 and eq_a1 (total=2).
    rack_user = await _create_scoped_group_user(
        client,
        admin,
        allow=["managed_asset:read", "managed_asset:update_lifecycle"],
        sites=[{"site_id": site_a["site_id"], "rack_scope": "selected", "rack_ids": [rack_a1["id"]]}],
    )
    rack_list = await client.get("/api/v1/managed-assets", headers=rack_user)
    assert rack_list.status_code == 200
    assert rack_list.json()["total"] == 2
    assert {item["id"] for item in rack_list.json()["items"]} == {rack_a1["id"], eq_a1["id"]}


@pytest.mark.asyncio
async def test_out_of_scope_reads_and_writes_fail_closed_with_zero_additional_events(
    client: AsyncClient, auth_headers, db_session, monkeypatch
):
    monkeypatch.setattr(
        access_control,
        "SCOPE_AWARE_PERMISSIONS",
        access_control.SCOPE_AWARE_PERMISSIONS | {"managed_asset:read", "managed_asset:update_lifecycle"},
    )

    admin = await auth_headers("Administrator")
    site_a = await _make_site_and_room(client, admin)
    site_b = await _make_site_and_room(client, admin)

    rack_a = await create_rack(client, admin, auth_headers, room_id=site_a["room_id"])
    rack_b = await create_rack(client, admin, auth_headers, room_id=site_b["room_id"])

    eq_a = await create_equipment(client, admin, auth_headers)
    await _place_equipment_in_rack(client, admin, eq_a["id"], site_a["room_id"], rack_a["id"])
    eq_b = await create_equipment(client, admin, auth_headers)
    await _place_equipment_in_rack(client, admin, eq_b["id"], site_b["room_id"], rack_b["id"])
    eq_unplaced = await create_equipment(client, admin, auth_headers)

    pdu_resp = await client.post(
        "/api/v1/managed-assets",
        json={"asset_type": "pdu", "asset_tag": f"PDU-{uuid.uuid4().hex[:8]}"},
        headers=admin,
    )
    assert pdu_resp.status_code == 201
    pdu_id = pdu_resp.json()["id"]

    site_a_user = await _create_scoped_group_user(
        client,
        admin,
        allow=["managed_asset:read", "managed_asset:update_lifecycle"],
        sites=[{"site_id": site_a["site_id"], "rack_scope": "all"}],
    )

    for forbidden_id in (eq_b["id"], rack_b["id"], eq_unplaced["id"], pdu_id):
        uid = uuid.UUID(forbidden_id)
        before_audit, before_outbox = await _audit_and_outbox_counts(db_session, uid)

        assert (await client.get(f"/api/v1/managed-assets/{forbidden_id}", headers=site_a_user)).status_code == 404
        assert (
            await client.get(f"/api/v1/managed-assets/{forbidden_id}/lifecycle-history", headers=site_a_user)
        ).status_code == 404
        tr = await client.post(
            f"/api/v1/managed-assets/{forbidden_id}/lifecycle-transition",
            json={"to_status": "installed"},
            headers=site_a_user,
        )
        assert tr.status_code == 404

        db_session.expire_all()
        asset = await db_session.get(ManagedAsset, uid)
        assert asset is not None and asset.lifecycle_status == "planned"
        after_audit, after_outbox = await _audit_and_outbox_counts(db_session, uid)
        assert after_audit == before_audit
        assert after_outbox == before_outbox

    # In-scope equipment transition succeeds and emits exactly 1 audit + 1 outbox event.
    eq_a_uid = uuid.UUID(eq_a["id"])
    before_audit, before_outbox = await _audit_and_outbox_counts(db_session, eq_a_uid)
    ok = await client.post(
        f"/api/v1/managed-assets/{eq_a['id']}/lifecycle-transition",
        json={"to_status": "installed"},
        headers=site_a_user,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["lifecycle_status"] == "installed"
    after_audit, after_outbox = await _audit_and_outbox_counts(db_session, eq_a_uid)
    assert after_audit == before_audit + 1
    assert after_outbox == before_outbox + 1

    history = await client.get(f"/api/v1/managed-assets/{eq_a['id']}/lifecycle-history", headers=site_a_user)
    assert history.status_code == 200
    assert len(history.json()["items"]) == 1
    assert history.json()["items"][0]["to_status"] == "installed"


@pytest.mark.asyncio
async def test_concurrent_placement_move_blocks_stale_lifecycle_transition(
    client: AsyncClient, auth_headers, db_engine, db_session, monkeypatch
):
    monkeypatch.setattr(
        access_control,
        "SCOPE_AWARE_PERMISSIONS",
        access_control.SCOPE_AWARE_PERMISSIONS | {"managed_asset:read", "managed_asset:update_lifecycle"},
    )

    admin = await auth_headers("Administrator")
    site_a = await _make_site_and_room(client, admin)
    site_b = await _make_site_and_room(client, admin)
    rack_a = await create_rack(client, admin, auth_headers, room_id=site_a["room_id"])
    rack_b = await create_rack(client, admin, auth_headers, room_id=site_b["room_id"])

    eq_a = await create_equipment(client, admin, auth_headers)
    await _place_equipment_in_rack(client, admin, eq_a["id"], site_a["room_id"], rack_a["id"])

    site_a_user = await _create_scoped_group_user(
        client,
        admin,
        allow=["managed_asset:read", "managed_asset:update_lifecycle"],
        sites=[{"site_id": site_a["site_id"], "rack_scope": "all"}],
    )

    eq_uid = uuid.UUID(eq_a["id"])
    before_audit, before_outbox = await _audit_and_outbox_counts(db_session, eq_uid)

    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)
    lock_acquired = asyncio.Event()
    release_move = asyncio.Event()

    async def _concurrent_move_out_of_scope() -> None:
        async with session_factory() as tx:
            await tx.execute(
                select(ManagedAsset).where(ManagedAsset.id == eq_uid).with_for_update()
            )
            lock_acquired.set()
            await release_move.wait()
            await tx.execute(
                text(
                    "UPDATE equipment_placement "
                    "SET room_id = :room_b, rack_id = :rack_b "
                    "WHERE equipment_id = :eid AND effective_to IS NULL"
                ),
                {"room_b": uuid.UUID(site_b["room_id"]), "rack_b": uuid.UUID(rack_b["id"]), "eid": eq_uid},
            )
            await tx.commit()

    async def _run_transition_request() -> int:
        async def _independent_get_db():
            async with session_factory() as s:
                yield s

        app.dependency_overrides[get_db] = _independent_get_db
        try:
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as req_client:
                resp = await req_client.post(
                    f"/api/v1/managed-assets/{eq_a['id']}/lifecycle-transition",
                    json={"to_status": "installed"},
                    headers=site_a_user,
                )
                return resp.status_code
        finally:
            app.dependency_overrides.clear()

    move_task = asyncio.create_task(_concurrent_move_out_of_scope())
    await lock_acquired.wait()

    transition_task = asyncio.create_task(_run_transition_request())
    await asyncio.sleep(0.15)
    release_move.set()

    move_res, transition_status = await asyncio.gather(move_task, transition_task)
    assert move_res is None
    assert transition_status == 404

    db_session.expire_all()
    asset = await db_session.get(ManagedAsset, eq_uid)
    assert asset is not None and asset.lifecycle_status == "planned"
    after_audit, after_outbox = await _audit_and_outbox_counts(db_session, eq_uid)
    assert after_audit == before_audit
    assert after_outbox == before_outbox
