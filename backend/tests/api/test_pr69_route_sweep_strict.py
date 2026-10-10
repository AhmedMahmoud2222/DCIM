"""Whole-API sweep for PR #59, with the three claims kept apart.

  1. Route registration / sweep coverage: every operation in the OpenAPI schema is called; the count is
     compared with the schema, not with a lower bound.
  2. Authentication and permission enforcement: the caller really authenticates (200 on /auth/me, 401
     anonymously) and every refusal is exactly 403 from the permission layer. Each refused route is
     contrasted with the unrestricted administrator, who must NOT be refused: otherwise the route could be
     "refused" simply because it is unreachable for everybody.
  3. Cross-resource isolation is NOT claimed here. A 404 or 422 for a random id says nothing about isolation;
     that is tested object by object in test_pr69_object_matrix_strict.py.

The set of operations a fully-permissioned restricted user can get past the permission layer is pinned
EXACTLY (REVIEWED_REACHABLE, 72 operations, with no conditional or optional entries). A new route that becomes reachable fails the test until someone reviews it, and
so does a reviewed route that disappears. The sweep itself is tested against a deliberate regression
(a non-site-aware permission declared scope-aware)."""

import re
import uuid

from app.application import access_control
from app.main import app
from tests.api.test_user_groups import _group, _group_user, _make_site

# The administrator's own 403s: cookie/CSRF-protected session routes, refused for every bearer-token caller.
REFUSED_FOR_EVERYONE = {("POST", "/api/v1/auth/refresh"), ("POST", "/api/v1/auth/logout")}

# (method, path) a restricted user holding EVERY permission still gets past the permission layer on, reviewed:
#   health/auth self-service, user and group administration (strict-outranking rules apply inside),
#   the reviewed site-aware modules (isolation proven in the object matrix), global reference catalogs,
#   HMAC-authenticated collector endpoints (a separate trust boundary), and import jobs (per-object, see PR #68).
REVIEWED_REACHABLE = {
    # Issue #105 (reviewed): every cooling / thermal route is scope-filtered in its handler -- configuration needs the whole site,
    # sensors use the equipment visibility clause, thermal views filter sensors before interpolation. See test_thermal_scope.py.
    ("DELETE", "/api/v1/cooling/relations/{relation_id}"),
    ("DELETE", "/api/v1/cooling/sensors/{sensor_id}/placement"),
    ("DELETE", "/api/v1/cooling/units/{unit_id}/placement"),
    ("DELETE", "/api/v1/cooling/zones/{zone_id}/containment-elements/{element_id}"),
    ("GET", "/api/v1/cooling/groups"),
    ("GET", "/api/v1/cooling/groups/{group_id}"),
    ("GET", "/api/v1/cooling/relations"),
    ("GET", "/api/v1/cooling/rooms/{room_id}/airflow"),
    ("GET", "/api/v1/cooling/rooms/{room_id}/capacity"),
    ("GET", "/api/v1/cooling/rooms/{room_id}/environment"),
    ("GET", "/api/v1/cooling/rooms/{room_id}/exceptions"),
    ("GET", "/api/v1/cooling/rooms/{room_id}/heat-map"),
    ("GET", "/api/v1/cooling/rooms/{room_id}/layout"),
    ("GET", "/api/v1/cooling/sensors"),
    ("GET", "/api/v1/cooling/sensors/{sensor_id}"),
    ("GET", "/api/v1/cooling/units"),
    ("GET", "/api/v1/cooling/units/{unit_id}"),
    ("GET", "/api/v1/cooling/zones"),
    ("GET", "/api/v1/cooling/zones/{zone_id}"),
    ("PATCH", "/api/v1/cooling/groups/{group_id}"),
    ("PATCH", "/api/v1/cooling/sensors/{sensor_id}"),
    ("PATCH", "/api/v1/cooling/units/{unit_id}"),
    ("PATCH", "/api/v1/cooling/zones/{zone_id}"),
    ("POST", "/api/v1/cooling/groups"),
    ("POST", "/api/v1/cooling/groups/{group_id}/retire"),
    ("POST", "/api/v1/cooling/relations"),
    ("POST", "/api/v1/cooling/sensors"),
    ("POST", "/api/v1/cooling/units"),
    ("POST", "/api/v1/cooling/units/{unit_id}/retire"),
    ("POST", "/api/v1/cooling/zones"),
    ("POST", "/api/v1/cooling/zones/{zone_id}/containment-elements"),
    ("POST", "/api/v1/cooling/zones/{zone_id}/retire"),
    ("PUT", "/api/v1/cooling/sensors/{sensor_id}/placement"),
    ("PUT", "/api/v1/cooling/units/{unit_id}/placement"),
    # Issue #101: both-endpoint mutation checks and masked reads; test_cables_scope.py.
    ("DELETE", "/api/v1/cables/{cable_id}"),
    ("GET", "/api/v1/cables"),
    ("GET", "/api/v1/cables/{cable_id}"),
    ("GET", "/api/v1/topology/ports/{port_id}/trace"),
    ("PATCH", "/api/v1/cables/{cable_id}"),
    ("POST", "/api/v1/cables"),
    ("POST", "/api/v1/cables/from-neighbor/{neighbor_id}"),
    ("POST", "/api/v1/cables/{cable_id}/install"),
    ("POST", "/api/v1/cables/{cable_id}/remove"),
    # Issue #101 B5: pass-throughs; hidden equipment is a 404 (test_cables_scope.py).
    ("DELETE", "/api/v1/pass-throughs/{pass_through_id}"),
    ("GET", "/api/v1/pass-throughs"),
    ("GET", "/api/v1/pass-throughs/{pass_through_id}"),
    ("POST", "/api/v1/pass-throughs"),
    # HMAC identity/assignment checks are exercised by test_discovery_plan.py.
    ("GET", "/api/v1/collectors/{collector_id}/discovery-plan"),
    ("GET", "/api/v1/collectors/{collector_id}/telemetry-contracts"),
    ("DELETE", "/api/v1/groups/{group_id}"),
    ("DELETE", "/api/v1/users/{user_id}"),
    ("GET", "/api/v1/auth/me"),
    ("GET", "/api/v1/buildings"),
    ("GET", "/api/v1/catalog/revisions/compare"),
    ("GET", "/api/v1/catalog/revisions/{revision_id}"),
    # PR-A (re-landed): lists the datasheets linked to a revision. Same global-catalog read dependency as the
    # revision GET above; downloading the file needs catalog:document_download and is not in this set.
    ("GET", "/api/v1/catalog/revisions/{revision_id}/documents"),
    ("GET", "/api/v1/catalog/revisions/{revision_id}/graphics/{side}/file"),
    ("GET", "/api/v1/catalog/revisions/{revision_id}/graphics/{side}/thumbnail"),
    ("GET", "/api/v1/cities"),
    ("GET", "/api/v1/countries"),
    ("GET", "/api/v1/equipment"),
    ("GET", "/api/v1/equipment-models"),
    ("GET", "/api/v1/equipment-models/{equipment_model_id}/revisions"),
    ("GET", "/api/v1/equipment/import-template"),
    ("GET", "/api/v1/equipment/{equipment_id}"),
    ("GET", "/api/v1/equipment/{equipment_id}/ports"),
    ("GET", "/api/v1/floors"),
    ("GET", "/api/v1/groups"),
    ("GET", "/api/v1/groups/permission-catalog"),
    ("GET", "/api/v1/groups/{group_id}"),
    ("GET", "/api/v1/health/live"),
    ("GET", "/api/v1/health/ready"),
    ("GET", "/api/v1/import-jobs/{job_id}"),
    ("GET", "/api/v1/import-jobs/{job_id}/report"),
    ("GET", "/api/v1/import-jobs/{job_id}/rows"),
    ("GET", "/api/v1/organizations"),
    ("GET", "/api/v1/organizations/{organization_id}"),
    ("GET", "/api/v1/rack-models"),
    ("GET", "/api/v1/rack-models/{rack_model_id}/revisions"),
    ("GET", "/api/v1/racks"),
    ("GET", "/api/v1/racks/import-template"),
    ("GET", "/api/v1/racks/{rack_id}"),
    ("GET", "/api/v1/racks/{rack_id}/elevation"),
    ("GET", "/api/v1/rooms"),
    ("GET", "/api/v1/rooms/{room_id}"),
    ("GET", "/api/v1/sites"),
    ("GET", "/api/v1/sites/{site_id}"),
    ("GET", "/api/v1/users"),
    ("GET", "/api/v1/users/{user_id}"),
    ("GET", "/api/v1/users/{user_id}/effective-access"),
    ("PATCH", "/api/v1/groups/{group_id}"),
    ("PATCH", "/api/v1/racks/{rack_id}"),
    ("PATCH", "/api/v1/users/{user_id}"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/collectors/{collector_id}/heartbeat"),
    ("POST", "/api/v1/collectors/{collector_id}/ingest"),
    ("POST", "/api/v1/collectors/{collector_id}/telemetry"),
    ("POST", "/api/v1/groups"),
    ("POST", "/api/v1/import-jobs/{job_id}/cancel"),
    ("POST", "/api/v1/import-jobs/{job_id}/commit"),
    ("POST", "/api/v1/racks"),
    ("POST", "/api/v1/racks/{rack_id}/move"),
    ("POST", "/api/v1/racks/{rack_id}/retire"),
    ("POST", "/api/v1/users"),
    ("PUT", "/api/v1/groups/{group_id}/members"),
    ("PUT", "/api/v1/groups/{group_id}/permissions"),
    ("PUT", "/api/v1/groups/{group_id}/site-access"),
}


async def _sweep(client, admin):
    me = (await client.get("/api/v1/auth/me", headers=admin)).json()
    site = await _make_site(client, admin)
    gid = await _group(client, admin, allow=me["permission_codes"], sites=[{"site_id": site["site"], "rack_scope": "all"}])
    _, headers = await _group_user(client, admin, [gid])
    observed = {}
    for path, item in app.openapi()["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        concrete = re.sub(r"\{[^}]+\}", str(uuid.uuid4()), path)
        for method in sorted(m.upper() for m in item if m.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE")):
            kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH") else {}
            restricted = await client.request(method, concrete, headers={**headers, "If-Match": "1"}, **kwargs)
            unrestricted = await client.request(method, concrete, headers={**admin, "If-Match": "1"}, **kwargs)
            observed[(method, path)] = (restricted.status_code, unrestricted.status_code)
    return headers, observed


def _expected_operation_count() -> int:
    return sum(
        1 for path, item in app.openapi()["paths"].items() if path.startswith("/api/v1")
        for m in item if m.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE")
    )


async def test_sweep_covers_every_registered_operation_and_the_caller_really_authenticates(client, auth_headers):
    admin = await auth_headers("Administrator")
    headers, observed = await _sweep(client, admin)
    assert len(observed) == _expected_operation_count()
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 200  # authenticated, restricted
    assert (await client.get("/api/v1/racks")).status_code == 401  # anonymous is refused before any permission check
    eff = (await client.get(f"/api/v1/users/{(await client.get('/api/v1/auth/me', headers=headers)).json()['id']}/effective-access", headers=admin)).json()
    assert eff["unrestricted"] is False and eff["inactive_permissions"], "the sweep user is restricted and holds inactive permissions"
    refused = [op for op, (r, _) in observed.items() if r == 403]
    print(f"SWEEP operations={len(observed)} refused_403={len(refused)} not_refused={len(observed) - len(refused)}")


async def test_every_refusal_is_a_403_from_the_permission_layer_and_not_a_dead_route(client, auth_headers):
    admin = await auth_headers("Administrator")
    _, observed = await _sweep(client, admin)
    refused = {op: statuses for op, statuses in observed.items() if statuses[0] in (401, 403, 405)}
    assert {s[0] for s in refused.values()} == {403}, "a refusal must be exactly 403 (not 401 from a lapsed token, not 405)"
    dead = [op for op, (_, adm) in refused.items() if adm in (401, 403, 405) and op not in REFUSED_FOR_EVERYONE]
    assert not dead, f"routes refused for the unrestricted administrator too (permission layer not the cause): {dead}"
    assert REFUSED_FOR_EVERYONE <= set(refused)
    print(f"SWEEP refused_by_permission_layer={len(refused) - len(REFUSED_FOR_EVERYONE)} refused_for_everyone={len(REFUSED_FOR_EVERYONE)}")


async def test_the_reachable_set_is_exactly_the_reviewed_set(client, auth_headers):
    admin = await auth_headers("Administrator")
    _, observed = await _sweep(client, admin)
    reachable = {op for op, (r, _) in observed.items() if r != 403}
    assert len(REVIEWED_REACHABLE) == 107, "the reviewed reachable set is pinned at exactly 107 operations"
    assert reachable == REVIEWED_REACHABLE, (
        f"new reachable: {sorted(reachable - REVIEWED_REACHABLE)}; no longer reachable: {sorted(REVIEWED_REACHABLE - reachable)}"
    )


async def test_the_sweep_detects_a_route_that_wrongly_becomes_reachable(client, auth_headers, monkeypatch):
    """Meta-test: declare a permission that guards a NON-site-aware module (alarms) scope-aware, the way a careless
    change would. The sweep must report the alarm routes as newly reachable."""
    monkeypatch.setattr(access_control, "SCOPE_AWARE_PERMISSIONS", access_control.SCOPE_AWARE_PERMISSIONS | {"alarm:read"})
    admin = await auth_headers("Administrator")
    _, observed = await _sweep(client, admin)
    reachable = {op for op, (r, _) in observed.items() if r != 403}
    leaked = sorted(reachable - REVIEWED_REACHABLE)
    # Every route guarded by alarm:read must show up: the alarm API itself and, since Issue #103, the correlation
    # incident reads that are gated by the same permission.
    assert leaked and all("/alarms" in path or "/operations/incidents" in path for _, path in leaked), leaked
