"""SEC-ARCH-01 (#57): a non-global RoleAssignment must not widen what its holder can do.

Before the fix, `load_effective_access` ignored `RoleAssignment.scope_id`. A user holding a
site-A-scoped role plus a group that grants site B exercised the role's permissions (for example
`rack:manage`) over site B, because role permissions were merged into one flat set and the data
scope came only from groups.

Enforced rule (fail closed, no per-permission scoping): a permission that comes ONLY from
non-global role assignments is honoured only while every site in the user's group-derived scope lies
inside every such assignment's own scope. A `building` assignment cannot be proven to contain a
site-level scope, so it contributes nothing. Group allow-grants are unaffected.

Also holds the positive/negative matrix for unrestricted, site-limited, rack-limited and
tenant-limited (organization-limited) users, and a guard that the surviving-administrator safeguard
of #127 still requires an unrestricted administrator.
"""

import uuid

import pytest
from sqlalchemy import select

from app.application.access_control import load_effective_access
from app.domain.auth.models import Permission, Role, RoleAssignment, RolePermission, User
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def _role(db, codes: list[str]) -> uuid.UUID:
    role = Role(id=uuid.uuid4(), name=f"scoped-{uuid.uuid4().hex[:8]}", is_system=False)
    db.add(role)
    await db.flush()
    for code in codes:
        resource, action = code.split(":")
        pid = (await db.execute(
            select(Permission.id).where(Permission.resource == resource, Permission.action == action)
        )).scalar_one()
        db.add(RolePermission(role_id=role.id, permission_id=pid))
    await db.commit()
    return role.id


async def _assign(db, user_id: str, role_id: uuid.UUID, scope_type: str, scope_id: str | None) -> None:
    db.add(RoleAssignment(
        id=uuid.uuid4(), user_id=uuid.UUID(user_id), role_id=role_id, scope_type=scope_type,
        scope_id=uuid.UUID(scope_id) if scope_id else None,
    ))
    await db.commit()


async def _patch_rack(client, headers, rack_id: str, version: int = 1):
    return await client.patch(
        f"/api/v1/racks/{rack_id}", json={"name": f"renamed-{uuid.uuid4().hex[:4]}"},
        headers={**headers, "If-Match": str(version)},
    )


async def _world(client, admin, auth_headers):
    a, b = await _make_site(client, admin), await _make_site(client, admin)
    return a, b, await _make_rack(client, admin, auth_headers, a["room"]), await _make_rack(client, admin, auth_headers, b["room"])


# ------------------------------------------------------------------ role scope is not ignored
async def test_site_scoped_role_does_not_apply_to_a_group_granted_other_site(client, admin, auth_headers, db_session):
    a, b, rack_a, rack_b = await _world(client, admin, auth_headers)
    # group grants only reads on BOTH sites; the manage permission exists only in the A-scoped role
    gid = await _group(client, admin, allow=["rack:read", "location:read"], sites=[
        {"site_id": a["site"], "rack_scope": "all"}, {"site_id": b["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "site", a["site"])

    # both sites visible (group), but the site-A role must not be honoured because the scope spans site B
    assert (await client.get(f"/api/v1/racks/{rack_b}", headers=headers)).status_code == 200
    assert (await _patch_rack(client, headers, rack_b)).status_code == 403
    assert (await _patch_rack(client, headers, rack_a)).status_code == 403
    access = (await load_effective_access(db_session, [uuid.UUID(user["id"])]))[uuid.UUID(user["id"])]
    assert "rack:manage" not in access.permission_codes


async def test_site_scoped_role_works_when_group_scope_is_inside_the_assignment(client, admin, auth_headers, db_session):
    a, b, rack_a, rack_b = await _world(client, admin, auth_headers)
    gid = await _group(client, admin, allow=["rack:read", "location:read"], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "site", a["site"])

    assert (await _patch_rack(client, headers, rack_a)).status_code == 200  # legitimate access preserved
    assert (await _patch_rack(client, headers, rack_b)).status_code == 404  # still no cross-site access
    assert (await client.get(f"/api/v1/racks/{rack_b}", headers=headers)).status_code == 404


async def test_site_scoped_role_for_a_different_site_grants_nothing(client, admin, auth_headers, db_session):
    a, b, rack_a, rack_b = await _world(client, admin, auth_headers)
    gid = await _group(client, admin, allow=["rack:read", "location:read"], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "site", b["site"])  # role bound to B only

    assert (await _patch_rack(client, headers, rack_a)).status_code == 403
    assert (await _patch_rack(client, headers, rack_b)).status_code == 403  # permission dropped, so refused before the object lookup
    assert (await client.get(f"/api/v1/racks/{rack_b}", headers=headers)).status_code == 404


async def test_building_scoped_role_fails_closed(client, admin, auth_headers, db_session):
    a, _b, rack_a, _rack_b = await _world(client, admin, auth_headers)
    gid = await _group(client, admin, allow=["rack:read", "location:read"], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    building = (await client.get("/api/v1/buildings", headers=admin, params={"site_id": a["site"]})).json()["items"][0]["id"]
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "building", building)

    assert (await _patch_rack(client, headers, rack_a)).status_code == 403


async def test_global_roles_and_group_grants_are_unaffected(client, admin, auth_headers, db_session):
    a, b, rack_a, rack_b = await _world(client, admin, auth_headers)
    # group allow-grant on one site keeps working with no role assignment at all
    gid = await _group(client, admin, allow=["rack:read", "rack:manage", "location:read"], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    _, headers = await _group_user(client, admin, [gid])
    assert (await _patch_rack(client, headers, rack_a)).status_code == 200
    assert (await _patch_rack(client, headers, rack_b)).status_code == 404
    # unrestricted global users reach every site
    assert (await _patch_rack(client, admin, rack_a, 2)).status_code == 200
    assert (await _patch_rack(client, admin, rack_b)).status_code == 200


async def test_scoped_role_does_not_make_the_holder_a_surviving_administrator(client, admin, db_session):
    """#127 guard: a site-scoped Administrator assignment never counts as an unrestricted administrator."""
    from app.application.access_control import active_administrator_ids

    site = await _make_site(client, admin)
    gid = await _group(client, admin, allow=["user:manage", "group:manage"], sites=[{"site_id": site["site"], "rack_scope": "all"}])
    user, _ = await _group_user(client, admin, [gid])
    admin_role = (await db_session.execute(select(Role.id).where(Role.name == "Administrator"))).scalar_one()
    await _assign(db_session, user["id"], admin_role, "site", site["site"])
    assert uuid.UUID(user["id"]) not in await active_administrator_ids(db_session)
    assert (await db_session.execute(select(User.id).where(User.id == uuid.UUID(user["id"])))).scalar_one()


# ------------------------------------------------------------------ unrestricted / site / rack / tenant matrix
async def test_scope_matrix_unrestricted_site_rack_and_tenant_limited(client, admin, auth_headers):
    a, b = await _make_site(client, admin), await _make_site(client, admin)  # separate organizations = tenants
    rack_a1 = await _make_rack(client, admin, auth_headers, a["room"])
    rack_a2 = await _make_rack(client, admin, auth_headers, a["room"])
    rack_b1 = await _make_rack(client, admin, auth_headers, b["room"])
    perms = ["rack:read", "rack:manage", "location:read", "organization:read", "equipment:read"]

    site_user = (await _group_user(client, admin, [await _group(client, admin, allow=perms, sites=[{"site_id": a["site"], "rack_scope": "all"}])]))[1]
    rack_user = (await _group_user(client, admin, [await _group(client, admin, allow=perms, sites=[
        {"site_id": a["site"], "rack_scope": "selected", "rack_ids": [rack_a1]}])]))[1]
    tenant_user = (await _group_user(client, admin, [await _group(client, admin, allow=perms, sites=[{"site_id": b["site"], "rack_scope": "all"}])]))[1]

    async def racks(headers):
        return {r["id"] for r in (await client.get("/api/v1/racks", headers=headers)).json()["items"]}

    assert {rack_a1, rack_a2, rack_b1} <= await racks(admin)  # unrestricted
    assert await racks(site_user) == {rack_a1, rack_a2}
    assert await racks(rack_user) == {rack_a1}
    assert await racks(tenant_user) == {rack_b1}

    assert (await client.get(f"/api/v1/racks/{rack_a2}", headers=rack_user)).status_code == 404
    assert (await _patch_rack(client, rack_user, rack_a2)).status_code == 404
    assert (await _patch_rack(client, rack_user, rack_a1)).status_code == 200
    for headers, other_org in ((site_user, b["org"]), (tenant_user, a["org"])):
        assert (await client.get(f"/api/v1/organizations/{other_org}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/racks/{rack_b1}", headers=site_user)).status_code == 404
    assert (await client.get(f"/api/v1/racks/{rack_a1}", headers=tenant_user)).status_code == 404
    assert (await _patch_rack(client, tenant_user, rack_a1)).status_code == 404

