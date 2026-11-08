"""SEC-ARCH-01 (#57): cross-scope negative tests for routers whose permissions are NOT site-aware.

A restricted (non-global) user keeps only SCOPE_AWARE_PERMISSIONS plus user/group administration;
every other code is reported inactive and refused (fail closed). These routers do not filter by
site, so if one of their codes is ever added to SCOPE_AWARE_PERMISSIONS without real router-level
enforcement, a site- or organization-scoped grant would expose every site's data and #57 reopens.
The allowlist snapshot forces any such change to be deliberate.
"""

import uuid

import pytest

from app.application.access_control import (
    SCOPE_AWARE_PERMISSIONS,
    UNSCOPED_ADMIN_RESOURCES,
    is_scope_independent,
    load_effective_access,
)
from app.application.rbac import DEFAULT_ROLE_PERMISSIONS
from tests.api.test_sec_arch_01_role_assignment_scope import _assign, _role
from tests.api.test_user_groups import _group, _group_user, _make_site

# (family, permission code, method, path); "{room}" is a room inside the user's own granted site.
DENY_ALL_ROUTES = [
    ("alarm", "alarm:read", "GET", "/api/v1/alarms"),
    ("incident", "alarm:read", "GET", "/api/v1/operations/incidents"),
    ("telemetry", "telemetry:read", "GET", "/api/v1/telemetry/metric-registry"),
    ("monitoring_settings", "telemetry:read", "GET", "/api/v1/settings/monitoring"),
    ("power", "power:read", "GET", "/api/v1/power/nodes"),
    ("power_protection", "power:read", "GET", "/api/v1/power/protection-devices"),
    ("power_analytics", "power:read", "GET", "/api/v1/power/analytics/reports"),
    ("capacity", "capacity:read", "GET", "/api/v1/power/capacity-exceptions"),
    ("impact", "power:read", "POST", "/api/v1/impact/simulate"),
    ("floor_plan", "floor_plan:read", "GET", "/api/v1/floor-plans"),
    ("integration", "integration:read", "GET", "/api/v1/integrations"),
    ("notification_channel", "integration:read", "GET", "/api/v1/operations/notification-channels"),
    ("collector", "collector:read", "GET", "/api/v1/collectors"),
    ("collector_state", "collector:read", "GET", "/api/v1/operations/collector-states"),
    ("discovery", "discovery:read", "GET", "/api/v1/discovery/devices"),
    ("managed_asset", "managed_asset:read", "GET", "/api/v1/managed-assets"),
    ("network_profile", "network_profile:read", "GET", "/api/v1/network-profiles/templates"),
    ("spatial", "spatial:read", "GET", "/api/v1/spatial/rooms/{room}/view"),
    ("dashboard", "dashboard:read", "GET", "/api/v1/dashboard/summary"),
    ("catalog", "catalog:read", "GET", "/api/v1/catalog/manufacturers"),
]
ROUTE_IDS = [r[0] for r in DENY_ALL_ROUTES]


@pytest.fixture
async def admin(auth_headers):
    return await auth_headers("Administrator")


async def _call(client, headers, method: str, path: str, room: str):
    url = path.format(room=room)
    if method == "POST":
        return await client.post(url, json={"target_type": "rack", "target_id": str(uuid.uuid4())}, headers=headers)
    return await client.get(url, headers=headers)


async def _restricted_user(client, admin, db, code: str, source: str):
    """A user with rack_scope=all on one site whose grant of `code` comes from `source`."""
    site = await _make_site(client, admin)
    allow = ["location:read", code] if source == "group_allow" else ["location:read"]
    gid = await _group(client, admin, allow=allow, sites=[{"site_id": site["site"], "rack_scope": "all"}])
    user, headers = await _group_user(client, admin, [gid])
    if source == "site_role":
        await _assign(db, user["id"], await _role(db, [code]), "site", site["site"])
    return site, uuid.UUID(user["id"]), headers


# ------------------------------------------------------------------ allowlist snapshot
def test_scope_aware_allowlist_is_exactly_the_reviewed_set():
    """Growing this set needs router-level site filtering for the new code first (#57, #101, #105)."""
    assert SCOPE_AWARE_PERMISSIONS == {
        "organization:read", "location:read", "rack:read", "rack:manage", "rack:place", "equipment:read",
        "cable:read", "cable:manage", "cooling:read", "cooling:manage",
    }
    assert UNSCOPED_ADMIN_RESOURCES == {"user", "group"}


@pytest.mark.parametrize("family,code,method,path", DENY_ALL_ROUTES, ids=ROUTE_IDS)
def test_deny_all_route_codes_are_not_scope_independent(family, code, method, path):
    assert not is_scope_independent(code)


async def test_every_non_site_aware_seeded_code_is_inactive_for_a_restricted_user(client, admin, db_session):
    codes = sorted(c for c in DEFAULT_ROLE_PERMISSIONS["Administrator"] if not is_scope_independent(c))
    site = await _make_site(client, admin)
    gid = await _group(client, admin, allow=codes, sites=[{"site_id": site["site"], "rack_scope": "all"}])
    user, _ = await _group_user(client, admin, [gid])
    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert set(codes) <= access.inactive_permissions
    assert not set(codes) & access.permission_codes


# ------------------------------------------------------------------ per-router negative tests
@pytest.mark.parametrize("family,code,method,path", DENY_ALL_ROUTES, ids=ROUTE_IDS)
async def test_administrator_passes_the_permission_gate(client, admin, family, code, method, path):
    """Positive control: the route exists and is gated only by the permission under test."""
    site = await _make_site(client, admin)
    resp = await _call(client, admin, method, path, site["room"])
    assert resp.status_code not in (401, 403, 405), resp.text
    if method == "GET":
        assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("source", ["site_role", "group_allow"])
@pytest.mark.parametrize("family,code,method,path", DENY_ALL_ROUTES, ids=ROUTE_IDS)
async def test_site_scoped_grant_does_not_unlock_a_non_site_aware_route(
    client, admin, db_session, family, code, method, path, source
):
    site, uid, headers = await _restricted_user(client, admin, db_session, code, source)
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert code in access.inactive_permissions  # the grant was honoured, then stripped (not vacuously absent)
    resp = await _call(client, headers, method, path, site["room"])
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("family,code,method,path", DENY_ALL_ROUTES, ids=ROUTE_IDS)
async def test_organization_scoped_role_does_not_unlock_a_non_site_aware_route(
    client, admin, db_session, family, code, method, path
):
    """Organization scope widens site visibility for scope-aware codes only, never these."""
    site = await _make_site(client, admin)
    user, headers = await _group_user(client, admin, [])
    await _assign(db_session, user["id"], await _role(db_session, ["location:read", code]), "organization", site["org"])
    uid = uuid.UUID(user["id"])
    access = (await load_effective_access(db_session, [uid]))[uid]
    assert "location:read" in access.permission_codes and code in access.inactive_permissions
    resp = await _call(client, headers, method, path, site["room"])
    assert resp.status_code == 403, resp.text
