"""PR #59 authorization-boundary regressions (independent review findings C1, H1-H4, M1-M3).

Each test drives the real HTTP API against PostgreSQL. A "delegate" is a site-restricted
user who holds user:manage/group:manage through a group, i.e. a delegated administrator."""

import asyncio
import importlib
import pkgutil
import re
import uuid
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.api.v1 as api_v1_pkg
from app.application.access_control import SCOPE_AWARE_PERMISSIONS, AccessScope
from app.db.session import get_db
from app.main import app
from tests.api._phase2_helpers import create_equipment_model_revision, create_rack_model_revision

PW = "correct horse battery staple"
ADMIN_PERMS = ["user:read", "user:manage", "group:read", "group:manage"]


async def _login(client, email: str) -> dict:
    resp = await client.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _site(client, admin) -> dict:
    org = (await client.post("/api/v1/organizations", json={"name": f"O-{uuid.uuid4().hex[:8]}"}, headers=admin)).json()["id"]
    country = (await client.post("/api/v1/countries", json={"organization_id": org, "name": "T", "iso_code": "TL"}, headers=admin)).json()
    city = (await client.post("/api/v1/cities", json={"country_id": country["id"], "name": f"C{uuid.uuid4().hex[:5]}"}, headers=admin)).json()
    site = (await client.post("/api/v1/sites", json={"city_id": city["id"], "code": f"S-{uuid.uuid4().hex[:6]}", "name": "Site"}, headers=admin)).json()
    building = (await client.post("/api/v1/buildings", json={"site_id": site["id"], "code": "A", "name": "B"}, headers=admin)).json()
    floor = (await client.post("/api/v1/floors", json={"building_id": building["id"], "name": "F", "level_number": 1}, headers=admin)).json()
    room = (await client.post("/api/v1/rooms", json={"floor_id": floor["id"], "code": f"R{uuid.uuid4().hex[:5]}", "name": "Room"}, headers=admin)).json()
    return {"site": site["id"], "room": room["id"], "org": org}


async def _rack(client, admin, auth_headers, room_id) -> str:
    revision = await create_rack_model_revision(client, auth_headers)
    resp = await client.post(
        "/api/v1/racks",
        json={"asset_tag": f"RK-{uuid.uuid4().hex[:8]}", "model_revision_id": revision, "name": "Rack", "room_id": room_id},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _group(client, headers, allow=(), deny=(), sites=()) -> str:
    resp = await client.post("/api/v1/groups", json={"name": f"G-{uuid.uuid4().hex[:8]}"}, headers=headers)
    assert resp.status_code == 201, resp.text
    gid = resp.json()["id"]
    if allow or deny:
        r = await client.put(f"/api/v1/groups/{gid}/permissions", json={"allow": list(allow), "deny": list(deny)}, headers=headers)
        assert r.status_code == 200, r.text
    if sites:
        r = await client.put(f"/api/v1/groups/{gid}/site-access", json={"sites": list(sites)}, headers=headers)
        assert r.status_code == 200, r.text
    return gid


async def _user(client, headers, group_ids, email=None):
    email = email or f"u-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/api/v1/users", json={"email": email, "full_name": "U", "password": PW, "group_ids": group_ids}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json(), await _login(client, email)


def _all(site_id: str) -> dict:
    return {"site_id": site_id, "rack_scope": "all"}


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


# ---------------------------------------------------------------- scope containment (unit)
def _scope(full=(), selected=None):
    selected = selected or {}
    sites = set(full) | set(selected)
    return AccessScope(
        unrestricted=False, site_ids=frozenset(sites), full_site_ids=frozenset(full),
        rack_ids=frozenset(r for racks in selected.values() for r in racks),
        selected_racks_by_site={k: frozenset(v) for k, v in selected.items()},
    )


def test_scope_containment_rules():
    a, b, r1, r2 = (uuid.uuid4() for _ in range(4))
    unrestricted = AccessScope(unrestricted=True)
    assert unrestricted.contains(_scope(full=[a])) and unrestricted.contains(unrestricted)
    assert not _scope(full=[a]).contains(unrestricted)
    assert _scope(full=[a]).contains(_scope()), "an empty scope is contained by everything"
    assert _scope(full=[a]).contains(_scope(selected={a: [r1]})), "all contains selected on the same site"
    assert not _scope(selected={a: [r1]}).contains(_scope(full=[a])), "selected never contains all"
    assert not _scope(selected={a: [r1]}).contains(_scope(selected={a: [r1, r2]}))
    assert _scope(selected={a: [r1, r2]}).contains(_scope(selected={a: [r1]}))
    assert not _scope(full=[a]).contains(_scope(full=[a, b])), "a missing site is not contained"
    assert not _scope(selected={a: [r1]}).contains(_scope(selected={b: [r1]})), "racks do not transfer between sites"


# ---------------------------------------------------------------- C1: administrators cannot be neutralised
async def test_delegate_cannot_deny_real_administrators_through_group_members(client, admin):
    delegate_group = await _group(client, admin, allow=ADMIN_PERMS)
    _, delegate = await _user(client, admin, [delegate_group])
    admin_id = (await client.get("/api/v1/auth/me", headers=admin)).json()["id"]
    deny = await _group(client, delegate, deny=ADMIN_PERMS)

    resp = await client.put(f"/api/v1/groups/{deny}/members", json={"user_ids": [admin_id]}, headers=delegate)

    assert resp.status_code == 404
    assert (await client.get("/api/v1/users", headers=admin)).status_code == 200


async def test_delegate_cannot_alter_a_group_that_holds_a_more_powerful_member(client, admin):
    shared = await _group(client, admin, allow=ADMIN_PERMS + ["rack:manage"])
    admin_id = (await client.get("/api/v1/auth/me", headers=admin)).json()["id"]
    assert (await client.put(f"/api/v1/groups/{shared}/members", json={"user_ids": []}, headers=admin)).status_code == 200
    other_admin = await _group(client, admin, allow=ADMIN_PERMS + ["rack:manage"])
    powerful, _ = await _user(client, admin, [other_admin])
    gid = await _group(client, admin)
    assert (await client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": [powerful["id"]]}, headers=admin)).status_code == 200
    _, delegate = await _user(client, admin, [await _group(client, admin, allow=ADMIN_PERMS)])

    for call in (
        client.put(f"/api/v1/groups/{gid}/permissions", json={"deny": ["rack:manage"]}, headers=delegate),
        client.put(f"/api/v1/groups/{gid}/members", json={"user_ids": []}, headers=delegate),
        client.patch(f"/api/v1/groups/{gid}", json={"name": "renamed"}, headers=delegate),
        client.delete(f"/api/v1/groups/{gid}", headers=delegate),
    ):
        assert (await call).status_code == 403
    del admin_id


async def test_nobody_changes_their_own_membership_through_the_group_endpoint(client, admin, auth_headers):
    a = await _site(client, admin)
    r1 = await _rack(client, admin, auth_headers, a["room"])
    await _rack(client, admin, auth_headers, a["room"])
    own = await _group(
        client, admin, allow=ADMIN_PERMS + ["rack:read", "location:read"],
        sites=[{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [r1]}],
    )
    delegate_user, delegate = await _user(client, admin, [own])
    target = await _group(client, delegate, allow=["rack:read"])

    resp = await client.put(f"/api/v1/groups/{target}/members", json={"user_ids": [delegate_user["id"]]}, headers=delegate)

    assert resp.status_code == 403
    assert (await client.patch(f"/api/v1/users/{delegate_user['id']}", json={"group_ids": [own, target]}, headers=delegate)).status_code == 403


# ---------------------------------------------------------------- H2: no cross-scope account takeover
async def test_delegate_cannot_reset_deactivate_or_delete_a_user_with_other_site_access(client, admin):
    a, b = await _site(client, admin), await _site(client, admin)
    _, delegate = await _user(client, admin, [await _group(client, admin, allow=ADMIN_PERMS + ["location:read"], sites=[_all(a["site"])])])
    victim, _ = await _user(client, admin, [await _group(client, admin, allow=["location:read"], sites=[_all(b["site"])])], "victim@example.com")

    assert (await client.patch(f"/api/v1/users/{victim['id']}", json={"password": "attacker chosen passphrase"}, headers=delegate)).status_code in (403, 404)
    assert (await client.patch(f"/api/v1/users/{victim['id']}", json={"is_active": False}, headers=delegate)).status_code in (403, 404)
    assert (await client.delete(f"/api/v1/users/{victim['id']}", headers=delegate)).status_code in (403, 404)
    login = await client.post("/api/v1/auth/login", json={"email": "victim@example.com", "password": PW})
    assert login.status_code == 200, "the victim's password and status are unchanged"


async def test_delegate_can_still_administer_a_user_inside_their_own_scope(client, admin):
    a = await _site(client, admin)
    _, delegate = await _user(client, admin, [await _group(client, admin, allow=ADMIN_PERMS + ["location:read"], sites=[_all(a["site"])])])
    inside, _ = await _user(client, admin, [await _group(client, admin, allow=["location:read"], sites=[_all(a["site"])])])
    unassigned, _ = await _user(client, admin, [])

    for user in (inside, unassigned):
        resp = await client.patch(f"/api/v1/users/{user['id']}", json={"full_name": "Renamed"}, headers=delegate)
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------- H3: rack scope cannot be widened
async def test_selected_rack_delegate_cannot_grant_all_racks(client, admin, auth_headers):
    a = await _site(client, admin)
    r1 = await _rack(client, admin, auth_headers, a["room"])
    r2 = await _rack(client, admin, auth_headers, a["room"])
    delegate_group = await _group(
        client, admin, allow=ADMIN_PERMS + ["rack:read", "location:read"],
        sites=[{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [r1]}],
    )
    _, delegate = await _user(client, admin, [delegate_group])
    big = await _group(client, delegate, allow=["rack:read"])

    wide = await client.put(f"/api/v1/groups/{big}/site-access", json={"sites": [_all(a["site"])]}, headers=delegate)
    beyond = await client.put(
        f"/api/v1/groups/{big}/site-access",
        json={"sites": [{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [r2]}]}, headers=delegate,
    )
    inside = await client.put(
        f"/api/v1/groups/{big}/site-access",
        json={"sites": [{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [r1]}]}, headers=delegate,
    )

    assert (wide.status_code, beyond.status_code, inside.status_code) == (403, 403, 200)


async def test_assigning_an_existing_wider_group_is_refused(client, admin, auth_headers):
    a = await _site(client, admin)
    r1 = await _rack(client, admin, auth_headers, a["room"])
    wide_group = await _group(client, admin, allow=["rack:read"], sites=[_all(a["site"])])
    delegate_group = await _group(
        client, admin, allow=ADMIN_PERMS + ["rack:read"],
        sites=[{"site_id": a["site"], "rack_scope": "selected", "rack_ids": [r1]}],
    )
    _, delegate = await _user(client, admin, [delegate_group])

    resp = await client.post(
        "/api/v1/users", json={"email": "acc@example.com", "full_name": "A", "password": PW, "group_ids": [wide_group]}, headers=delegate
    )

    assert resp.status_code == 404


# ---------------------------------------------------------------- H4: groups and users are scoped objects
async def test_scoped_delegate_cannot_see_or_edit_groups_and_users_of_other_sites(client, admin):
    a, b = await _site(client, admin), await _site(client, admin)
    _, delegate = await _user(client, admin, [await _group(client, admin, allow=ADMIN_PERMS, sites=[_all(a["site"])])])
    other_group = await _group(client, admin, allow=["location:read"], sites=[_all(b["site"])])
    other_user, _ = await _user(client, admin, [other_group], "other-tenant@example.com")
    own_group = await _group(client, admin, allow=["location:read"], sites=[_all(a["site"])])
    own_user, _ = await _user(client, admin, [own_group], "own-site@example.com")
    admin_email = (await client.get("/api/v1/auth/me", headers=admin)).json()["email"]

    assert (await client.get(f"/api/v1/groups/{other_group}", headers=delegate)).status_code == 404
    assert (await client.put(f"/api/v1/groups/{other_group}/site-access", json={"sites": []}, headers=delegate)).status_code == 404
    assert (await client.get(f"/api/v1/users/{other_user['id']}", headers=delegate)).status_code == 404
    assert (await client.get(f"/api/v1/users/{other_user['id']}/effective-access", headers=delegate)).status_code == 404
    group_ids = {g["id"] for g in (await client.get("/api/v1/groups", headers=delegate)).json()["items"]}
    assert other_group not in group_ids and own_group in group_ids
    emails = {u["email"] for u in (await client.get("/api/v1/users", headers=delegate)).json()["items"]}
    assert "other-tenant@example.com" not in emails and admin_email not in emails and own_user["email"] in emails
    assert (await client.get(f"/api/v1/groups/{other_group}", headers=admin)).status_code == 200


# ---------------------------------------------------------------- M1: equipment follows its rack
async def test_equipment_is_hidden_from_the_old_site_after_its_rack_moves(client, admin, auth_headers):
    a, b = await _site(client, admin), await _site(client, admin)
    rack = await _rack(client, admin, auth_headers, a["room"])
    revision = await create_equipment_model_revision(client, auth_headers)
    eq = (await client.post("/api/v1/equipment", json={"asset_tag": f"EQ-{uuid.uuid4().hex[:8]}", "model_revision_id": revision}, headers=admin)).json()
    mounted = await client.post(
        f"/api/v1/equipment/{eq['id']}/move",
        json={"placement_type": "rack_mounted", "room_id": a["room"], "rack_id": rack, "u_start": 1, "u_end": 2, "side": "front"},
        headers=admin,
    )
    assert mounted.status_code == 200, mounted.text
    group = await _group(client, admin, allow=["equipment:read", "rack:read"], sites=[_all(a["site"])])
    _, viewer = await _user(client, admin, [group])
    assert (await client.get(f"/api/v1/equipment/{eq['id']}", headers=viewer)).status_code == 200

    assert (await client.post(f"/api/v1/racks/{rack}/move", json={"room_id": b["room"]}, headers=admin)).status_code == 200

    assert (await client.get(f"/api/v1/equipment/{eq['id']}", headers=viewer)).status_code == 404
    assert (await client.get("/api/v1/equipment", headers=viewer)).json()["total"] == 0


async def test_rack_granted_on_one_site_stays_hidden_after_moving_to_another_granted_site(client, admin, auth_headers):
    a, b = await _site(client, admin), await _site(client, admin)
    rack = await _rack(client, admin, auth_headers, a["room"])
    other = await _rack(client, admin, auth_headers, b["room"])
    group = await _group(
        client, admin, allow=["rack:read"],
        sites=[
            {"site_id": a["site"], "rack_scope": "selected", "rack_ids": [rack]},
            {"site_id": b["site"], "rack_scope": "selected", "rack_ids": [other]},
        ],
    )
    _, viewer = await _user(client, admin, [group])
    assert (await client.post(f"/api/v1/racks/{rack}/move", json={"room_id": b["room"]}, headers=admin)).status_code == 200

    assert (await client.get(f"/api/v1/racks/{rack}", headers=viewer)).status_code == 404
    assert {r["id"] for r in (await client.get("/api/v1/racks", headers=viewer)).json()["items"]} == {other}


# ---------------------------------------------------------------- M2: users holding a legacy role can be deleted
async def test_user_with_a_legacy_role_can_be_deleted(client, admin, make_user):
    user = await make_user("legacy-viewer@example.com", PW, "Viewer")
    assert (await client.delete(f"/api/v1/users/{user.id}", headers=admin)).status_code == 204
    assert (await client.get(f"/api/v1/users/{user.id}", headers=admin)).status_code == 404
    assert (await client.post("/api/v1/auth/login", json={"email": "legacy-viewer@example.com", "password": PW})).status_code == 401


# ---------------------------------------------------------------- last administrator under real concurrency
async def test_concurrent_requests_cannot_remove_every_administrator(db_engine, make_user, db_session):
    a = await make_user("admin-a@example.com", PW, "Administrator")
    b = await make_user("admin-b@example.com", PW, "Administrator")
    factory = async_sessionmaker(bind=db_engine, expire_on_commit=False, autoflush=False)

    async def per_request_session():
        async with factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = per_request_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            ha, hb = await _login(c, "admin-a@example.com"), await _login(c, "admin-b@example.com")
            deny_a = await _group(c, ha, deny=["user:manage"])
            deny_b = await _group(c, hb, deny=["user:manage"])
            # Each request would be individually safe (the actor stays an administrator); together they are not.
            # Since PR #67 two global Administrators are peers and cannot administer each other, so neither
            # request is accepted: both are refused, nothing is written and both stay administrators. The
            # last-administrator check itself is covered at helper level in test_user_groups.py, and the
            # serialisation of authority changes in tests/integration/test_pr67_authority_serialization.py.
            first, second = await asyncio.gather(
                c.put(f"/api/v1/groups/{deny_a}/members", json={"user_ids": [str(b.id)]}, headers=ha),
                c.put(f"/api/v1/groups/{deny_b}/members", json={"user_ids": [str(a.id)]}, headers=hb),
            )
        assert [first.status_code, second.status_code] == [403, 403], (first.text, second.text)
        committed = (await db_session.execute(text("SELECT count(*) FROM user_group_member"))).scalar_one()
        assert committed == 0, "neither request wrote a membership"
        active_admins = (
            await db_session.execute(text("SELECT count(*) FROM app_user WHERE id IN (:a, :b) AND is_active"), {"a": a.id, "b": b.id})
        ).scalar_one()
        assert active_admins == 2, "both administrators remain"
    finally:
        app.dependency_overrides.clear()


# ---------------------------------------------------------------- M3: structural guards
# Every route below is gated by a permission code that restricted users keep. Each is either
# scope-filtered in its handler, or serves non-site reference data. A new route appearing here
# must be reviewed for scope handling, then added to this list.
REVIEWED_SCOPE_AWARE_ROUTES = {
    # Issue #105 cooling / thermal (reviewed): configuration needs the whole site (_site_visible); sensors use the equipment
    # visibility clause; thermal views filter sensors by scope before interpolation and withhold site-level data from rack-limited callers.
    ("cooling", "DELETE", "/cooling/relations/{relation_id}"),
    ("cooling", "DELETE", "/cooling/sensors/{sensor_id}/placement"),
    ("cooling", "DELETE", "/cooling/units/{unit_id}/placement"),
    ("cooling", "DELETE", "/cooling/zones/{zone_id}/containment-elements/{element_id}"),
    ("cooling", "GET", "/cooling/groups"),
    ("cooling", "GET", "/cooling/groups/{group_id}"),
    ("cooling", "GET", "/cooling/relations"),
    ("cooling", "GET", "/cooling/sensors"),
    ("cooling", "GET", "/cooling/sensors/{sensor_id}"),
    ("cooling", "GET", "/cooling/units"),
    ("cooling", "GET", "/cooling/units/{unit_id}"),
    ("cooling", "GET", "/cooling/zones"),
    ("cooling", "GET", "/cooling/zones/{zone_id}"),
    ("cooling", "PATCH", "/cooling/groups/{group_id}"),
    ("cooling", "PATCH", "/cooling/sensors/{sensor_id}"),
    ("cooling", "PATCH", "/cooling/units/{unit_id}"),
    ("cooling", "PATCH", "/cooling/zones/{zone_id}"),
    ("cooling", "POST", "/cooling/groups"),
    ("cooling", "POST", "/cooling/groups/{group_id}/retire"),
    ("cooling", "POST", "/cooling/relations"),
    ("cooling", "POST", "/cooling/sensors"),
    ("cooling", "POST", "/cooling/units"),
    ("cooling", "POST", "/cooling/units/{unit_id}/retire"),
    ("cooling", "POST", "/cooling/zones"),
    ("cooling", "POST", "/cooling/zones/{zone_id}/containment-elements"),
    ("cooling", "POST", "/cooling/zones/{zone_id}/retire"),
    ("cooling", "PUT", "/cooling/sensors/{sensor_id}/placement"),
    ("cooling", "PUT", "/cooling/units/{unit_id}/placement"),
    ("thermal", "GET", "/cooling/rooms/{room_id}/airflow"),
    ("thermal", "GET", "/cooling/rooms/{room_id}/capacity"),
    ("thermal", "GET", "/cooling/rooms/{room_id}/environment"),
    ("thermal", "GET", "/cooling/rooms/{room_id}/exceptions"),
    ("thermal", "GET", "/cooling/rooms/{room_id}/heat-map"),
    ("thermal", "GET", "/cooling/rooms/{room_id}/layout"),
    # Issue #101 object isolation is covered by test_cables_scope.py.
    ("cables", "DELETE", "/cables/{cable_id}"),
    ("cables", "GET", "/cables"),
    ("cables", "GET", "/cables/{cable_id}"),
    ("cables", "GET", "/topology/ports/{port_id}/trace"),
    ("cables", "PATCH", "/cables/{cable_id}"),
    ("cables", "POST", "/cables"),
    ("cables", "POST", "/cables/from-neighbor/{neighbor_id}"),
    ("cables", "POST", "/cables/{cable_id}/install"),
    ("cables", "POST", "/cables/{cable_id}/remove"),
    # Issue #101 B5: pass-throughs are scope-checked on the equipment of their ports; see test_cables_scope.py.
    ("cables", "DELETE", "/pass-throughs/{pass_through_id}"),
    ("cables", "GET", "/pass-throughs"),
    ("cables", "GET", "/pass-throughs/{pass_through_id}"),
    ("cables", "POST", "/pass-throughs"),
    ("catalog", "GET", "/equipment-models"),  # reference catalog, not site data
    ("catalog", "GET", "/equipment-models/{equipment_model_id}/revisions"),
    ("catalog", "GET", "/rack-models"),
    ("catalog", "GET", "/rack-models/{rack_model_id}/revisions"),
    ("equipment", "GET", "/equipment"),
    ("equipment", "GET", "/equipment/import-template"),  # static template file
    ("equipment", "GET", "/equipment/{equipment_id}"),
    ("equipment", "GET", "/equipment/{equipment_id}/ports"),
    ("locations", "GET", "/buildings"),
    ("locations", "GET", "/cities"),
    ("locations", "GET", "/countries"),
    ("locations", "GET", "/floors"),
    ("locations", "GET", "/organizations"),
    ("locations", "GET", "/organizations/{organization_id}"),
    ("locations", "GET", "/rooms"),
    ("locations", "GET", "/rooms/{room_id}"),
    ("locations", "GET", "/sites"),
    ("locations", "GET", "/sites/{site_id}"),
    ("racks", "GET", "/racks"),
    ("racks", "GET", "/racks/import-template"),  # static template file
    ("racks", "GET", "/racks/{rack_id}"),
    ("racks", "GET", "/racks/{rack_id}/elevation"),
    ("racks", "PATCH", "/racks/{rack_id}"),
    ("racks", "POST", "/racks"),
    ("racks", "POST", "/racks/{rack_id}/move"),
    ("racks", "POST", "/racks/{rack_id}/retire"),
}


def _dependency_codes(dependant) -> set[str]:
    codes: set[str] = set()

    def walk(node):
        for sub in node.dependencies:
            call = sub.call
            if getattr(call, "__closure__", None) and "require_" in getattr(call, "__qualname__", ""):
                codes.update(c.cell_contents for c in call.__closure__ if isinstance(c.cell_contents, str))
            walk(sub)

    walk(dependant)
    return codes


def test_every_route_using_a_scope_aware_permission_has_been_reviewed():
    from fastapi.routing import APIRoute

    found = set()
    for module in pkgutil.iter_modules(api_v1_pkg.__path__):
        if module.name == "router":
            continue
        router = getattr(importlib.import_module(f"app.api.v1.{module.name}"), "router", None)
        for route in getattr(router, "routes", []):
            if isinstance(route, APIRoute) and _dependency_codes(route.dependant) & SCOPE_AWARE_PERMISSIONS:
                found.add((module.name, sorted(route.methods)[0], route.path))
    assert found == REVIEWED_SCOPE_AWARE_ROUTES


def test_dynamic_permission_checks_are_confined_to_reviewed_modules():
    """A permission code computed at runtime (e.g. f"{import_type}:read") cannot be matched
    to the allow-list statically, so every such handler must apply its own scope rule."""
    api_dir = Path(api_v1_pkg.__file__).parent
    dynamic = {
        path.name
        for path in api_dir.glob("*.py")
        if re.search(r'has_permission\((?!")|required\s*=\s*f"', path.read_text())
    }
    # bulk_import: jobs are restricted to their uploader (_assert_job_visible).
    # catalog_designer: conditional catalog:* codes, which restricted users never hold.
    # thermal: `_holds` applies a granted-but-scope-inactive spatial/telemetry/power/alarm code only because every query
    # behind the thermal views is filtered by the caller's site/rack scope before interpolation or aggregation.
    assert dynamic <= {"bulk_import.py", "catalog_designer.py", "thermal.py"}, dynamic
