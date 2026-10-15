"""SEC-ARCH-01 (#57): a non-global RoleAssignment must not widen what its holder can do.

Before the fix, `load_effective_access` ignored `RoleAssignment.scope_id`. A user holding a
site-A-scoped role plus a group that grants site B exercised the role's permissions (for example
`rack:manage`) over site B, because role permissions were merged into one flat set and the data
scope came only from groups.

Enforced rule (fail closed, no per-permission scoping): a permission that comes ONLY from
non-global role assignments is honoured only while every site in the user's group-derived scope lies
inside every such assignment's own scope. The `building` scope type was removed (migration 0054).
An `organization` assignment grants every site of its organization, never sites of another
organization, and never makes its holder unrestricted. Group allow-grants are unaffected.

Also holds the positive/negative matrix for unrestricted, site-limited, rack-limited and
tenant-limited (organization-limited) users, and a guard that the surviving-administrator safeguard
of #127 still requires an unrestricted administrator.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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


async def test_building_scope_type_is_rejected_by_the_database(client, admin, db_session):
    """`building` was removed (migration 0054); the CHECK constraint refuses it outright."""
    a = await _make_site(client, admin)
    user, _ = await _group_user(client, admin, [])
    role_id = await _role(db_session, ["rack:manage"])
    with pytest.raises(IntegrityError, match="ck_role_assignment_scope_type_allowed"):
        await _assign(db_session, user["id"], role_id, "building", a["site"])
    await db_session.rollback()


def test_unknown_scope_types_fail_closed_in_the_containment_check():
    from app.application.access_control import AccessScope, scoped_role_is_contained

    site = uuid.uuid4()
    empty = AccessScope(unrestricted=False)
    assert not scoped_role_is_contained("building", site, unrestricted=False, scope=empty)
    assert not scoped_role_is_contained("organization", None, unrestricted=False, scope=empty)
    assert not scoped_role_is_contained("organization", site, unrestricted=True, scope=empty, organization_site_ids=frozenset({site}))
    in_site = AccessScope(unrestricted=False, site_ids=frozenset({site}))
    assert scoped_role_is_contained("organization", uuid.uuid4(), unrestricted=False, scope=in_site, organization_site_ids=frozenset({site}))
    assert not scoped_role_is_contained("organization", uuid.uuid4(), unrestricted=False, scope=in_site, organization_site_ids=frozenset())


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



# ------------------------------------------------------------------ legitimate in-scope operations are not denied
async def test_rack_limited_user_keeps_a_site_scoped_role_inside_the_assignment_site(client, admin, auth_headers, db_session):
    """Over-denial check: a rack-limited user (selected racks on site A) with a site-A role keeps exactly its racks."""
    a, b, rack_a1, rack_b = await _world(client, admin, auth_headers)
    rack_a2 = await _make_rack(client, admin, auth_headers, a["room"])
    gid = await _group(client, admin, allow=["rack:read", "location:read"], sites=[
        {"site_id": a["site"], "rack_scope": "selected", "rack_ids": [rack_a1]}])
    user, headers = await _group_user(client, admin, [gid])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "site", a["site"])

    assert (await _patch_rack(client, headers, rack_a1)).status_code == 200
    assert (await _patch_rack(client, headers, rack_a2)).status_code == 404  # outside the selected racks
    assert (await _patch_rack(client, headers, rack_b)).status_code == 404  # outside the site


async def test_in_scope_create_move_and_retire_work_with_a_site_scoped_role(client, admin, auth_headers, db_session):
    a, b, rack_a, _rack_b = await _world(client, admin, auth_headers)
    gid = await _group(client, admin, allow=["rack:read", "location:read"], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage", "rack:place"]), "site", a["site"])
    from tests.api._phase2_helpers import create_rack_model_revision

    revision = await create_rack_model_revision(client, auth_headers)
    body = {"asset_tag": f"RK-{uuid.uuid4().hex[:8]}", "model_revision_id": revision, "name": "n"}
    assert (await client.post("/api/v1/racks", json={**body, "room_id": b["room"]}, headers=headers)).status_code == 404
    assert (await client.post("/api/v1/racks", json={**body, "room_id": a["room"]}, headers=headers)).status_code == 201
    assert (await client.post(f"/api/v1/racks/{rack_a}/move", json={"room_id": b["room"]}, headers=headers)).status_code == 404
    assert (await client.post(f"/api/v1/racks/{rack_a}/move", json={"room_id": a["room"]}, headers=headers)).status_code in (200, 409)


async def test_scoped_administrator_assignment_does_not_confer_the_role_name(client, admin, db_session):
    """An out-of-scope Administrator assignment must not satisfy role-membership checks (catalog, import)."""
    a, b = await _make_site(client, admin), await _make_site(client, admin)
    admin_role = (await db_session.execute(select(Role.id).where(Role.name == "Administrator"))).scalar_one()
    gid = await _group(client, admin, allow=["rack:read"], sites=[
        {"site_id": a["site"], "rack_scope": "all"}, {"site_id": b["site"], "rack_scope": "all"}])
    user, _ = await _group_user(client, admin, [gid])
    uid = uuid.UUID(user["id"])
    await _assign(db_session, user["id"], admin_role, "site", a["site"])  # spans site B too: out of scope
    assert "Administrator" not in (await load_effective_access(db_session, [uid]))[uid].role_names

    # a global Viewer plus a site-scoped Administrator assignment stays a Viewer (unrestricted, but no Administrator)
    viewer = (await db_session.execute(select(Role.id).where(Role.name == "Viewer"))).scalar_one()
    other, _ = await _group_user(client, admin, [])
    await _assign(db_session, other["id"], viewer, "global", None)
    await _assign(db_session, other["id"], admin_role, "site", a["site"])
    oid = uuid.UUID(other["id"])
    access = (await load_effective_access(db_session, [oid]))[oid]
    assert access.scope.unrestricted and "Administrator" not in access.role_names
    assert "user:manage" not in access.permission_codes and "rack:manage" not in access.permission_codes


async def test_site_scoped_assignment_honoured_when_group_scope_equals_its_site(client, admin, db_session):
    a = await _make_site(client, admin)
    gid = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": a["site"], "rack_scope": "all"}])
    user, _ = await _group_user(client, admin, [gid])
    uid = uuid.UUID(user["id"])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "site", a["site"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert {"rack:read", "rack:manage"} <= access.permission_codes


# ------------------------------------------------------------------ organization scope
async def _org_world(client, admin, auth_headers):
    """Two sites in organization A and one in organization B, one rack each."""
    a1 = await _make_site(client, admin)
    a2 = await _make_site(client, admin, org_id=a1["org"])
    b = await _make_site(client, admin)
    racks = [await _make_rack(client, admin, auth_headers, s["room"]) for s in (a1, a2, b)]
    return a1, a2, b, racks


async def _racks(client, headers) -> set[str]:
    return {r["id"] for r in (await client.get("/api/v1/racks", headers=headers)).json()["items"]}


async def test_organization_scoped_role_grants_every_site_of_its_organization(client, admin, auth_headers, db_session):
    a1, a2, b, (rack_a1, rack_a2, rack_b) = await _org_world(client, admin, auth_headers)
    user, headers = await _group_user(client, admin, [])  # no group grants at all
    await _assign(db_session, user["id"], await _role(db_session, ["rack:read", "rack:manage", "location:read", "organization:read"]), "organization", a1["org"])

    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert access.scope.site_ids == {uuid.UUID(a1["site"]), uuid.UUID(a2["site"])}
    assert access.scope.full_site_ids == access.scope.site_ids
    assert await _racks(client, headers) == {rack_a1, rack_a2}
    assert (await _patch_rack(client, headers, rack_a1)).status_code == 200
    assert (await _patch_rack(client, headers, rack_a2)).status_code == 200
    # a site of another organization stays invisible
    assert (await client.get(f"/api/v1/racks/{rack_b}", headers=headers)).status_code == 404
    assert (await _patch_rack(client, headers, rack_b)).status_code == 404
    assert (await client.get(f"/api/v1/sites/{b['site']}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/organizations/{b['org']}", headers=headers)).status_code == 404
    assert (await client.get(f"/api/v1/organizations/{a1['org']}", headers=headers)).status_code == 200


async def test_organization_scoped_role_is_unioned_with_in_organization_group_grants(client, admin, auth_headers, db_session):
    a1, a2, _b, (rack_a1, rack_a2, rack_b) = await _org_world(client, admin, auth_headers)
    gid = await _group(client, admin, allow=["rack:read"], sites=[{"site_id": a1["site"], "rack_scope": "selected", "rack_ids": []}])
    user, headers = await _group_user(client, admin, [gid])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:read", "location:read"]), "organization", a1["org"])
    # the organization grant (rack_scope=all) wins over the narrower group grant on the same site
    assert await _racks(client, headers) == {rack_a1, rack_a2}


async def test_organization_scoped_role_fails_closed_when_groups_reach_another_organization(client, admin, auth_headers, db_session):
    a1, _a2, b, (rack_a1, _rack_a2, rack_b) = await _org_world(client, admin, auth_headers)
    gid = await _group(client, admin, allow=["rack:read", "location:read"], sites=[{"site_id": b["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "organization", a1["org"])

    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert "rack:manage" not in access.permission_codes  # would otherwise apply to organization B's site
    assert access.scope.site_ids == {uuid.UUID(b["site"])}
    assert (await _patch_rack(client, headers, rack_b)).status_code == 403
    assert (await client.get(f"/api/v1/racks/{rack_a1}", headers=headers)).status_code == 404


async def test_organization_scoped_roles_for_two_organizations_fail_closed(client, admin, auth_headers, db_session):
    a1, _a2, b, _racks_ = await _org_world(client, admin, auth_headers)
    user, _ = await _group_user(client, admin, [])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "organization", a1["org"])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:read"]), "organization", b["org"])
    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert not access.scope.site_ids
    assert not {"rack:manage", "rack:read"} & access.permission_codes


async def test_organization_scoped_administrator_is_not_global(client, admin, auth_headers, db_session):
    from app.application.access_control import active_administrator_ids

    a1, _a2, _b, _racks_ = await _org_world(client, admin, auth_headers)
    user, headers = await _group_user(client, admin, [])
    admin_role = (await db_session.execute(select(Role.id).where(Role.name == "Administrator"))).scalar_one()
    await _assign(db_session, user["id"], admin_role, "organization", a1["org"])

    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert not access.scope.unrestricted
    assert "Administrator" not in access.role_names  # no catalog-administrator / bulk-import role checks
    assert "rack:manage" in access.permission_codes  # scope-aware permissions are honoured
    assert "catalog:manage" in access.inactive_permissions and "catalog:manage" not in access.permission_codes
    assert uid not in await active_administrator_ids(db_session)
    assert (await client.post("/api/v1/rack-models", json={}, headers=headers)).status_code == 403


async def test_global_role_is_unchanged_by_an_organization_assignment(client, admin, auth_headers, db_session):
    a1, _a2, _b, _racks_ = await _org_world(client, admin, auth_headers)
    user, _ = await _group_user(client, admin, [])
    viewer = (await db_session.execute(select(Role.id).where(Role.name == "Viewer"))).scalar_one()
    await _assign(db_session, user["id"], viewer, "global", None)
    await _assign(db_session, user["id"], await _role(db_session, ["rack:manage"]), "organization", a1["org"])
    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert access.scope.unrestricted and "rack:manage" not in access.permission_codes


async def test_organization_scoped_role_reaches_a_site_created_after_the_assignment(client, admin, auth_headers, db_session):
    """Pins the deliberate, owner-approved dynamic-expansion behaviour (SEC-ARCH-01 decision memo,
    B2): reach is recomputed from the live hierarchy on every request, not snapshotted at
    assignment time, so a site added to the organization later is covered automatically."""
    a1, _a2, _b, (rack_a1, _rack_a2, _rack_b) = await _org_world(client, admin, auth_headers)
    user, headers = await _group_user(client, admin, [])
    await _assign(db_session, user["id"], await _role(db_session, ["rack:read", "rack:manage"]), "organization", a1["org"])

    # the assignment above already exists; this site is created afterwards, under the same org
    a3 = await _make_site(client, admin, org_id=a1["org"])
    rack_a3 = await _make_rack(client, admin, auth_headers, a3["room"])

    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert uuid.UUID(a3["site"]) in access.scope.site_ids
    assert (await _patch_rack(client, headers, rack_a1)).status_code == 200
    assert (await _patch_rack(client, headers, rack_a3)).status_code == 200
