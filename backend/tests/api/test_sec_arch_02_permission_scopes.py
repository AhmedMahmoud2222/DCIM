"""SEC-ARCH-02: each allow stays bound to its source's own resource scope."""

import uuid

import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.audit.models import AuditLog
from app.domain.auth.models import Role, UserGroupPermission, UserGroupSiteAccess
from app.domain.outbox.models import OutboxEvent
from tests.api.test_sec_arch_01_role_assignment_scope import _assign, _patch_rack, _role
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin, org_id=a["org"])
    racks = [await _make_rack(client, admin, auth_headers, s["room"]) for s in (a, b)]
    return a, b, *racks


def _site(site, scope="all", racks=()):
    return {"site_id": site["site"], "rack_scope": scope, "rack_ids": list(racks)}


async def _snapshot(client, admin, db, rack):
    response = await client.get(f"/api/v1/racks/{rack}", headers=admin)
    counts = [
        (await db.execute(select(func.count()).select_from(model))).scalar_one()
        for model in (AuditLog, OutboxEvent)
    ]
    return response.json(), counts


@pytest.mark.parametrize("global_viewer", [False, True], ids=["F1", "F2"])
async def test_group_write_cannot_borrow_other_sources_scope(client, admin, db_session, world, global_viewer):
    a, b, ra, rb = world
    ga = await _group(client, admin, allow=["rack:manage", "rack:read"], sites=[_site(a)])
    gb = await _group(client, admin, allow=["rack:read"], sites=[_site(b)])
    user, headers = await _group_user(client, admin, [ga] if global_viewer else [ga, gb])
    if global_viewer:
        viewer = (await db_session.execute(select(Role.id).where(Role.name == "Viewer"))).scalar_one()
        await _assign(db_session, user["id"], viewer, "global", None)
    assert (await client.get(f"/api/v1/racks/{rb}", headers=headers)).status_code == 200
    listing = await client.get("/api/v1/racks", headers=headers)
    assert listing.json()["total"] == 2
    assert {r["id"] for r in listing.json()["items"]} == {ra, rb}
    before = await _snapshot(client, admin, db_session, rb)
    response = await _patch_rack(client, headers, rb)
    assert response.status_code == 404, response.text
    assert await _snapshot(client, admin, db_session, rb) == before
    assert (await _patch_rack(client, headers, ra)).status_code == 200
    # In-scope optimistic concurrency remains an ordinary conflict.
    assert (await _patch_rack(client, headers, ra)).status_code == 409


async def test_read_scope_cannot_borrow_write_site_or_hierarchy(client, admin, world):
    a, b, ra, rb = world
    ga = await _group(client, admin, allow=["rack:manage"], sites=[_site(a)])
    gb = await _group(client, admin, allow=["rack:read", "location:read", "organization:read"], sites=[_site(b)])
    _, headers = await _group_user(client, admin, [ga, gb])
    assert (await client.get(f"/api/v1/racks/{ra}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/racks/{rb}", headers=headers)).status_code == 200
    response = await client.get("/api/v1/racks", headers=headers)
    assert response.json()["total"] == 1
    assert [r["id"] for r in response.json()["items"]] == [rb]
    assert (await client.get(f"/api/v1/sites/{a['site']}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/rooms/{a['room']}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/sites/{b['site']}", headers=headers)).status_code == 200
    assert (await _patch_rack(client, headers, ra)).status_code == 200


async def test_all_racks_read_does_not_widen_selected_write(client, admin, auth_headers, world):
    a, _, ra, _ = world
    other = await _make_rack(client, admin, auth_headers, a["room"])
    ga = await _group(client, admin, allow=["rack:manage"], sites=[_site(a, "selected", [ra])])
    gb = await _group(client, admin, allow=["rack:read"], sites=[_site(a)])
    _, headers = await _group_user(client, admin, [ga, gb])
    assert (await _patch_rack(client, headers, other)).status_code == 404
    assert (await _patch_rack(client, headers, ra)).status_code == 200
    # A second source of the SAME permission may legitimately widen it.
    response = await client.put(f"/api/v1/groups/{gb}/permissions", json={"allow": ["rack:read", "rack:manage"], "deny": []}, headers=admin)
    assert response.status_code == 200, response.text
    assert (await _patch_rack(client, headers, other)).status_code == 200


async def test_global_permission_preserved_and_explicit_deny_still_wins(client, admin, db_session, world):
    a, _, _, rb = world
    ga = await _group(client, admin, allow=["rack:read"], sites=[_site(a)])
    user, headers = await _group_user(client, admin, [ga])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "global", None)
    assert (await _patch_rack(client, headers, rb)).status_code == 200
    response = await client.put(f"/api/v1/groups/{ga}/permissions", json={"allow": ["rack:read"], "deny": ["rack:manage"]}, headers=admin)
    assert response.status_code == 200, response.text
    assert (await _patch_rack(client, headers, rb, version=2)).status_code == 403


async def test_global_viewer_does_not_activate_scoped_unfiltered_write(client, admin, db_session, world):
    a, _, _, _ = world
    ga = await _group(client, admin, allow=["alarm:manage", "rack:manage"], sites=[_site(a)])
    user, headers = await _group_user(client, admin, [ga])
    viewer = (await db_session.execute(select(Role.id).where(Role.name == "Viewer"))).scalar_one()
    await _assign(db_session, user["id"], viewer, "global", None)
    response = await client.post("/api/v1/alarms/rules", json={}, headers=headers)
    assert response.status_code == 403, response.text


async def test_membership_revocation_and_empty_source_do_not_borrow_scope(client, admin, world):
    a, _, ra, _ = world
    permission_only = await _group(client, admin, allow=["rack:manage"])
    read = await _group(client, admin, allow=["rack:read"], sites=[_site(a)])
    write = await _group(client, admin, allow=["rack:manage"], sites=[_site(a)])
    user, headers = await _group_user(client, admin, [permission_only, read, write])
    assert (await _patch_rack(client, headers, ra)).status_code == 200
    response = await client.patch(f"/api/v1/users/{user['id']}", json={"group_ids": [permission_only, read]}, headers=admin)
    assert response.status_code == 200, response.text
    assert (await _patch_rack(client, headers, ra, version=2)).status_code == 404


async def test_delegation_cannot_cross_product_permission_and_scope(client, admin, world):
    a, b, _, _ = world
    admin_perms = ["group:read", "group:manage", "user:read", "user:manage", "rack:read"]
    manage_a = await _group(client, admin, allow=["rack:manage"], sites=[_site(a)])
    administer = await _group(client, admin, allow=admin_perms, sites=[_site(a), _site(b)])
    _, headers = await _group_user(client, admin, [manage_a, administer])
    empty_b = await _group(client, admin, allow=[], sites=[_site(b)])
    response = await client.put(f"/api/v1/groups/{empty_b}/permissions", json={"allow": ["rack:manage"], "deny": []}, headers=headers)
    assert response.status_code == 403, response.text
    detail = (await client.get(f"/api/v1/groups/{empty_b}", headers=admin)).json()
    assert detail["allow_permissions"] == []


async def test_global_viewer_does_not_globalize_group_administration(client, admin, db_session, world):
    a, b, _, _ = world
    ga = await _group(client, admin, allow=["group:read", "group:manage"], sites=[_site(a)])
    user, headers = await _group_user(client, admin, [ga])
    viewer = (await db_session.execute(select(Role.id).where(Role.name == "Viewer"))).scalar_one()
    await _assign(db_session, user["id"], viewer, "global", None)
    gb = await _group(client, admin, sites=[_site(b)])
    response = await client.get(f"/api/v1/groups/{gb}", headers=headers)
    assert response.status_code == 404, response.text
    response = await client.patch(f"/api/v1/groups/{gb}", json={"name": "unauthorized"}, headers=headers)
    assert response.status_code == 404, response.text


async def test_thermal_required_permissions_intersect_source_scopes(client, admin, world):
    a, b, _, _ = world
    cooling = await _group(client, admin, allow=["cooling:read"], sites=[_site(a), _site(b)])
    auxiliary = await _group(client, admin, allow=["telemetry:read", "spatial:read"], sites=[_site(a)])
    _, headers = await _group_user(client, admin, [cooling, auxiliary])
    assert (await client.get(f"/api/v1/cooling/rooms/{a['room']}/environment", headers=headers)).status_code == 200
    response = await client.get(f"/api/v1/cooling/rooms/{b['room']}/environment", headers=headers)
    assert response.status_code == 404, response.text


async def test_spatial_overlay_cannot_borrow_global_spatial_scope(client, admin, db_session, world):
    a, b, ra, _ = world
    network = await _group(client, admin, allow=["cable:read"], sites=[_site(a)])
    user, headers = await _group_user(client, admin, [network])
    await _assign(db_session, user["id"], await _role(db_session, ["spatial:read"]), "global", None)
    response = await client.get(f"/api/v1/spatial/rooms/{b['room']}/overlays?kinds=network", headers=headers)
    assert response.status_code == 404, response.text
    response = await client.get(f"/api/v1/spatial/rooms/{a['room']}/overlays?kinds=network", headers=headers)
    assert response.status_code == 200, response.text
    assert {r["asset_id"] for r in response.json()["overlays"]["network"]["items"]} == {ra}


async def test_effective_access_reports_each_permission_scope(client, admin, world):
    a, b, _, _ = world
    ga = await _group(client, admin, allow=["rack:manage"], sites=[_site(a)])
    gb = await _group(client, admin, allow=["rack:read"], sites=[_site(b)])
    user, _ = await _group_user(client, admin, [ga, gb])
    response = await client.get(f"/api/v1/users/{user['id']}/effective-access", headers=admin)
    assert response.status_code == 200, response.text
    scopes = response.json()["permission_scopes"]
    assert scopes["rack:manage"]["site_ids"] == [a["site"]]
    assert scopes["rack:read"]["site_ids"] == [b["site"]]


async def test_preflight_reports_narrowing_in_read_only_transaction(client, admin, db_session, world):
    from app.application.access_preflight import access_change_report

    a, b, _, rb = world
    ga = await _group(client, admin, allow=["rack:manage"], sites=[_site(a)])
    gb = await _group(client, admin, allow=["rack:read"], sites=[_site(b)])
    user, _ = await _group_user(client, admin, [ga, gb])
    before = await _snapshot(client, admin, db_session, rb)
    async with AsyncSession(db_session.bind) as report_session:
        await report_session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
        report = await access_change_report(report_session)
        assert (await report_session.execute(text("SHOW transaction_read_only"))).scalar_one() == "on"
    row = next(r for r in report if r["user_id"] == user["id"] and r["permission"] == "rack:manage")
    assert set(row["before_scope"]["site_ids"]) == {a["site"], b["site"]}
    assert row["after_scope"]["site_ids"] == [a["site"]]
    assert row["before_active"] is row["after_active"] is True
    assert await _snapshot(client, admin, db_session, rb) == before


@pytest.mark.parametrize("deny_removal", [False, True], ids=["incomparable", "post-state"])
async def test_authority_comparison_keeps_permission_scope_pairs(client, admin, db_session, world, deny_removal):
    a, b, _, rb = world
    admin_codes = ["group:read", "group:manage", "user:read", "user:manage", "rack:read"]
    administration = await _group(client, admin, allow=admin_codes, sites=[_site(a), _site(b)])
    manage_a = await _group(client, admin, allow=["rack:manage"], sites=[_site(a)])
    _, actor = await _group_user(client, admin, [administration, manage_a])
    manage_b = await _group(client, admin, allow=["rack:manage", "rack:read"], sites=[_site(b)])
    read_a = await _group(client, admin, allow=["rack:read"], sites=[_site(a)])
    denied = await _group(client, admin, deny=["rack:manage"], sites=[_site(a), _site(b)])
    target, _ = await _group_user(client, admin, [manage_b, read_a, *([denied] if deny_removal else [])])
    before = await _snapshot(client, admin, db_session, rb)
    if deny_removal:
        response = await client.put(f"/api/v1/groups/{denied}/permissions", json={"allow": [], "deny": []}, headers=actor)
        assert response.status_code == 403, response.text
        detail = (await client.get(f"/api/v1/groups/{denied}", headers=admin)).json()
        assert detail["deny_permissions"] == ["rack:manage"]
    else:
        response = await client.patch(f"/api/v1/users/{target['id']}", json={"full_name": "unauthorized"}, headers=actor)
        assert response.status_code == 403, response.text
        assert (await client.get(f"/api/v1/users/{target['id']}", headers=admin)).json()["full_name"] == "Group User"
    assert await _snapshot(client, admin, db_session, rb) == before


async def test_selected_write_does_not_follow_rack_to_another_readable_site(client, admin, world):
    a, b, ra, _ = world
    write = await _group(client, admin, allow=["rack:manage"], sites=[_site(a, "selected", [ra])])
    read = await _group(client, admin, allow=["rack:read"], sites=[_site(a), _site(b)])
    _, headers = await _group_user(client, admin, [write, read])
    response = await client.post(f"/api/v1/racks/{ra}/move", json={"room_id": b["room"]}, headers=admin)
    assert response.status_code == 200, response.text
    assert (await client.get(f"/api/v1/racks/{ra}", headers=headers)).status_code == 200
    assert (await _patch_rack(client, headers, ra)).status_code == 404


async def test_concurrent_grant_change_cannot_mix_permission_and_site_snapshots(client, admin, db_session, world, monkeypatch):
    from app.application.access_control import load_effective_access
    from app.application.authority_lock import acquire_authority_lock

    a, b, _, _ = world
    gid = await _group(client, admin, allow=["rack:manage"], sites=[_site(a)])
    user, _ = await _group_user(client, admin, [gid])
    uid, group_id = uuid.UUID(user["id"]), uuid.UUID(gid)
    original = db_session.execute
    changed = False

    async def interleaved(statement, *args, **kwargs):
        nonlocal changed
        result = await original(statement, *args, **kwargs)
        if not changed and "user_group_permission" in str(statement):
            changed = True
            # The old permission is fetched before this independent transaction
            # atomically removes it and moves the grant. No committed state ever
            # allows rack:manage at B. A multi-statement loader must not invent it.
            async with AsyncSession(db_session.bind) as writer:
                await acquire_authority_lock(writer)
                await writer.execute(delete(UserGroupPermission).where(UserGroupPermission.group_id == group_id))
                await writer.execute(update(UserGroupSiteAccess).where(UserGroupSiteAccess.group_id == group_id).values(site_id=uuid.UUID(b["site"])))
                await writer.commit()
        return result

    monkeypatch.setattr(db_session, "execute", interleaved)
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert changed, "the actual PostgreSQL grant-change interleaving must execute"
    assert not access.scope_for("rack:manage").allows_site(uuid.UUID(b["site"]))
    assert "rack:manage" not in (await load_effective_access(db_session, [uid]))[uid].permission_codes


async def test_surviving_admin_requires_each_admin_permission_to_be_global(client, admin, db_session, world):
    from app.application.access_control import active_administrator_ids

    a, _, _, _ = world
    group = await _group(client, admin, allow=["user:manage", "group:manage"], sites=[_site(a)])
    user, _ = await _group_user(client, admin, [group])
    viewer = (await db_session.execute(select(Role.id).where(Role.name == "Viewer"))).scalar_one()
    await _assign(db_session, user["id"], viewer, "global", None)
    uid = uuid.UUID(user["id"])
    assert uid not in await active_administrator_ids(db_session)
    global_admin = await _role(db_session, ["user:manage", "group:manage"])
    await _assign(db_session, user["id"], global_admin, "global", None)
    assert uid in await active_administrator_ids(db_session)


@pytest.mark.parametrize("import_type", ["rack", "equipment"])
async def test_import_job_visibility_uses_its_read_permission_scope(client, admin, db_session, world, import_type):
    from tests.api.test_pr59_import_job_scope import _job

    a, _, _, _ = world
    group = await _group(client, admin, allow=[f"{import_type}:read"], sites=[_site(a)])
    user, headers = await _group_user(client, admin, [group])
    await _assign(db_session, user["id"], await _role(db_session, ["organization:read"]), "global", None)
    job = await _job(client, admin, db_session, import_type)
    for suffix in ("", "/rows"):
        response = await client.get(f"/api/v1/import-jobs/{job.id}{suffix}", headers=headers)
        assert response.status_code == 404, response.text
    # The pre-existing owner-only rule still permits the caller's own job.
    job.uploaded_by_user_id = uuid.UUID(user["id"])
    await db_session.commit()
    response = await client.get(f"/api/v1/import-jobs/{job.id}", headers=headers)
    assert response.status_code == 200, response.text


async def test_unused_inactive_permission_does_not_block_legitimate_administration(client, admin, world):
    a, _, _, _ = world
    actor_group = await _group(client, admin, allow=["user:manage", "user:read", "rack:read"], sites=[_site(a)])
    _, actor = await _group_user(client, admin, [actor_group])
    target_group = await _group(client, admin, allow=["rack:read", "alarm:manage"], sites=[_site(a)])
    target, _ = await _group_user(client, admin, [target_group])
    response = await client.patch(f"/api/v1/users/{target['id']}", json={"full_name": "Legitimate update"}, headers=actor)
    assert response.status_code == 200, response.text


async def test_filtered_thermal_read_entitlements_are_part_of_authority(client, admin, world):
    a, b, _, _ = world
    administration = await _group(
        client, admin, allow=["user:manage", "user:read", "cooling:read", "spatial:read"], sites=[_site(a), _site(b)]
    )
    telemetry_a = await _group(client, admin, allow=["telemetry:read"], sites=[_site(a)])
    _, actor = await _group_user(client, admin, [administration, telemetry_a])
    target_group = await _group(client, admin, allow=["cooling:read", "spatial:read", "telemetry:read"], sites=[_site(b)])
    target, _ = await _group_user(client, admin, [target_group])
    # Both have the same inactive telemetry code and aggregate reach is contained,
    # but only the target can read thermal telemetry at B.
    response = await client.patch(f"/api/v1/users/{target['id']}", json={"full_name": "unauthorized"}, headers=actor)
    assert response.status_code == 403, response.text
