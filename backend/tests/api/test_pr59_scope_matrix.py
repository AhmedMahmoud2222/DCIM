"""PR #59 scope-isolation evidence: object-level A/B matrix and a whole-API route sweep.

Question under test: can a user restricted to Site A / Rack A retrieve, modify, trigger,
import or export anything belonging to Site B / Rack B through any registered route?"""

import re
import uuid

import pytest

from app.main import app
from tests.api._phase2_helpers import create_equipment
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site

DATA_PERMS = ["organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read"]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


@pytest.fixture
async def world(client, admin, auth_headers):
    a = await _make_site(client, admin)
    b = await _make_site(client, admin)  # separate organization: also tests org/country/city leakage
    w = {"a": a, "b": b}
    for key, site in (("a1", a), ("a2", a), ("b1", b)):
        w[f"rack_{key}"] = await _make_rack(client, admin, auth_headers, site["room"])
    for key, rack, site in (("a1", w["rack_a1"], a), ("a2", w["rack_a2"], a), ("b1", w["rack_b1"], b)):
        eq = await create_equipment(client, admin, auth_headers)
        mv = await client.post(
            f"/api/v1/equipment/{eq['id']}/move",
            json={"placement_type": "rack_mounted", "room_id": site["room"], "rack_id": rack, "u_start": 1, "u_end": 2, "side": "front"},
            headers=admin,
        )
        assert mv.status_code == 200, mv.text
        w[f"eq_{key}"] = eq["id"]
    eq_room_a = await create_equipment(client, admin, auth_headers)
    mv = await client.post(f"/api/v1/equipment/{eq_room_a['id']}/move", json={"placement_type": "floor_standing", "room_id": a["room"]}, headers=admin)
    w["eq_room_a"] = eq_room_a["id"] if mv.status_code == 200 else None
    return w


async def _restricted(client, admin, site, *, rack_scope="all", rack_ids=(), perms=DATA_PERMS):
    gid = await _group(client, admin, allow=perms, sites=[{"site_id": site, "rack_scope": rack_scope, "rack_ids": list(rack_ids)}])
    return (await _group_user(client, admin, [gid]))[1]


def _ids(page):
    return {item["id"] for item in page["items"]}


# ------------------------------------------------------------------ object-level matrix
async def test_site_a_user_cannot_touch_site_b_objects(client, admin, world):
    ua = await _restricted(client, admin, world["a"]["site"])
    b, a = world["b"], world["a"]
    rack_b, eq_b = world["rack_b1"], world["eq_b1"]

    reads = [
        f"/api/v1/racks/{rack_b}", f"/api/v1/racks/{rack_b}/elevation", f"/api/v1/equipment/{eq_b}",
        f"/api/v1/equipment/{eq_b}/ports", f"/api/v1/sites/{b['site']}", f"/api/v1/rooms/{b['room']}",
        f"/api/v1/organizations/{b['org']}",
    ]
    for url in reads:
        resp = await client.get(url, headers=ua)
        assert resp.status_code == 404, (url, resp.status_code)

    # writes against Site B objects
    version = (await client.get(f"/api/v1/racks/{rack_b}", headers=admin)).json()["version"]
    writes = [
        await client.patch(f"/api/v1/racks/{rack_b}", json={"name": "x"}, headers={**ua, "If-Match": str(version)}),
        await client.post(f"/api/v1/racks/{rack_b}/move", json={"room_id": a["room"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=ua),
        await client.post(f"/api/v1/racks/{rack_b}/retire", headers=ua),
        await client.post(f"/api/v1/racks/{world['rack_a1']}/move", json={"room_id": b["room"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=ua),
        await client.post("/api/v1/racks", json={"asset_tag": "X1", "model_revision_id": str(uuid.uuid4()), "name": "n", "room_id": b["room"]}, headers=ua),
    ]
    for resp in writes:
        assert resp.status_code in (403, 404, 409, 422) and resp.status_code != 200, (resp.request.url, resp.status_code, resp.text)
    assert (await client.get(f"/api/v1/racks/{rack_b}", headers=admin)).json()["name"] != "x"


async def test_collection_endpoints_exclude_other_site_and_report_true_totals(client, admin, world):
    ua = await _restricted(client, admin, world["a"]["site"])
    racks = (await client.get("/api/v1/racks?limit=200", headers=ua)).json()
    assert world["rack_b1"] not in _ids(racks)
    assert {world["rack_a1"], world["rack_a2"]} <= _ids(racks)
    assert racks["total"] == len(_ids(racks)) == 2  # count must not include Site B racks
    assert (await client.get(f"/api/v1/racks?site_id={world['b']['site']}", headers=ua)).json()["total"] == 0
    eq = (await client.get("/api/v1/equipment?limit=200", headers=ua)).json()
    assert world["eq_b1"] not in _ids(eq) and eq["total"] == len(_ids(eq))
    assert {world["eq_a1"], world["eq_a2"]} <= _ids(eq)
    for path, own in (("sites", world["a"]["site"]), ("rooms", world["a"]["room"]), ("organizations", world["a"]["org"])):
        page = (await client.get(f"/api/v1/{path}?limit=200", headers=ua)).json()
        assert _ids(page) == {own}, path
    for path in ("countries", "cities", "buildings", "floors"):
        page = (await client.get(f"/api/v1/{path}?limit=200", headers=ua)).json()
        assert page["total"] == len(page["items"]) == 1, path


async def test_rack_limited_user_sees_only_selected_rack_and_its_equipment(client, admin, world):
    ur = await _restricted(client, admin, world["a"]["site"], rack_scope="selected", rack_ids=[world["rack_a1"]])
    assert (await client.get(f"/api/v1/racks/{world['rack_a1']}", headers=ur)).status_code == 200
    assert (await client.get(f"/api/v1/racks/{world['rack_a2']}", headers=ur)).status_code == 404
    assert _ids((await client.get("/api/v1/racks", headers=ur)).json()) == {world["rack_a1"]}
    eq_ids = _ids((await client.get("/api/v1/equipment", headers=ur)).json())
    assert world["eq_a1"] in eq_ids and world["eq_a2"] not in eq_ids and world["eq_b1"] not in eq_ids
    assert (await client.get(f"/api/v1/equipment/{world['eq_a2']}", headers=ur)).status_code == 404
    if world["eq_room_a"]:  # room-placed equipment is only visible with rack_scope=all
        assert (await client.get(f"/api/v1/equipment/{world['eq_room_a']}", headers=ur)).status_code == 404


async def test_unknown_and_foreign_ids_are_indistinguishable(client, admin, world):
    ua = await _restricted(client, admin, world["a"]["site"])
    foreign = await client.get(f"/api/v1/racks/{world['rack_b1']}", headers=ua)
    missing = await client.get(f"/api/v1/racks/{uuid.uuid4()}", headers=ua)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["title"] == missing.json()["title"]


async def test_stale_rack_grant_does_not_follow_a_rack_to_another_site(client, admin, world):
    ur = await _restricted(client, admin, world["a"]["site"], rack_scope="selected", rack_ids=[world["rack_a1"]])
    moved = await client.post(
        f"/api/v1/racks/{world['rack_a1']}/move", json={"room_id": world["b"]["room"], "x_mm": 0, "y_mm": 0, "rotation_deg": 0}, headers=admin
    )
    assert moved.status_code == 200, moved.text
    assert (await client.get(f"/api/v1/racks/{world['rack_a1']}", headers=ur)).status_code == 404


# ------------------------------------------------------------------ whole-API sweep
SCOPED_PREFIXES = (
    "/api/v1/racks", "/api/v1/equipment", "/api/v1/organizations", "/api/v1/countries", "/api/v1/cities", "/api/v1/sites",
    "/api/v1/buildings", "/api/v1/floors", "/api/v1/rooms", "/api/v1/users", "/api/v1/groups", "/api/v1/auth", "/api/v1/health",
)


async def test_route_sweep_restricted_user_holding_every_permission(client, admin):
    """Grant a restricted user (Site A) EVERY permission the administrator holds, then call
    every registered route. Routes outside the reviewed, site-aware modules must refuse."""
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    site = await _make_site(client, admin)
    gid = await _group(client, admin, allow=me["permission_codes"], sites=[{"site_id": site["site"], "rack_scope": "all"}])
    _, headers = await _group_user(client, admin, [gid])

    reachable: list[str] = []
    refused = checked = 0
    families: dict[str, list[int]] = {}
    # Non-vacuity: the user really does hold the permissions, so a scoped route answers.
    assert (await client.get("/api/v1/racks", headers=headers)).status_code == 200
    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        concrete = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        for method in sorted(m.upper() for m in item if m.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE")):
            checked += 1
            resp = await client.request(method, concrete, headers={**headers, "If-Match": "1"}, json={} if method in ("POST", "PUT", "PATCH") else None)
            family = families.setdefault(path.split("/")[3], [0, 0])
            family[0] += 1
            if resp.status_code in (401, 403):
                refused += 1
                family[1] += 1
            elif not path.startswith(SCOPED_PREFIXES) and not _expected_reachable(method, path):
                reachable.append(f"{method} {path} -> {resp.status_code}")
    print("FAMILY-TABLE " + "; ".join(f"{name}: {done}/{total} refused" for name, (total, done) in sorted(families.items())))
    assert checked >= 150 and refused >= 100, (checked, refused)
    assert not reachable, "\n".join(reachable)


def _expected_reachable(method: str, path: str) -> bool:
    """Routes a restricted user may legitimately reach outside the reviewed site-aware modules:
    global reference catalogs (no site data), collector machine endpoints (HMAC-authenticated, a
    separate trust boundary, not a user session), and import-jobs (authorised per object: see
    test_pr59_import_job_scope.py)."""
    if method == "GET" and path.startswith(("/api/v1/rack-models", "/api/v1/equipment-models", "/api/v1/catalog")):
        return True
    if method == "POST" and re.fullmatch(r"/api/v1/collectors/\{[^}]+\}/(heartbeat|ingest|telemetry)", path):
        return True
    return path.startswith("/api/v1/import-jobs/")
