"""Issue #195 / P2 F2: `POST /cables/from-neighbor/{id}` must not disclose hidden topology.

Actor: a custom GLOBAL role holding only `discovery:reconcile` (not site-aware) plus a group that grants `cable:manage` for
site A only. That is the one combination that reaches the handler as a site-restricted caller. A discovered neighbor is
global evidence; once it is resolved to equipment ports, it belongs to the sites of those ports, so a neighbor with any
port outside the caller's scope must be indistinguishable from a missing neighbor: same status, same title, and a detail
naming only the identifier the caller supplied. No neighbor state (unconfirmed, stale, already cabled) and no
neighbor-derived port id may be observable through the response.
"""

import re
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text

from tests.api._cable_helpers import CABLES, create_cable
from tests.api._network_helpers import collector_with_integration
from tests.api._network_inventory import publish_revision
from tests.api.test_cables_scope import place
from tests.api.test_sec_arch_01_role_assignment_scope import _assign, _role
from tests.api.test_user_groups import _group, _group_user, _make_rack, _make_site

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
BODY = {"label": "FROM-NEIGHBOR", "cable_type": "copper_utp"}


@pytest.fixture
async def world(client, auth_headers, db_session):
    admin = await auth_headers("Administrator")
    site_a = await _make_site(client, admin)
    site_b = await _make_site(client, admin, site_a["org"])
    rack_a = await _make_rack(client, admin, auth_headers, site_a["room"])
    rack_b = await _make_rack(client, admin, auth_headers, site_b["room"])
    revision = await publish_revision(client, admin, ["p1", "p2", "p3"])
    a1 = await place(client, admin, revision["id"], site_a, rack_a, 1, "a1-sw")
    a2 = await place(client, admin, revision["id"], site_a, rack_a, 3, "a2-sw")
    b1 = await place(client, admin, revision["id"], site_b, rack_b, 1, "b1-sw")
    b2 = await place(client, admin, revision["id"], site_b, rack_b, 3, "b2-sw")
    _collector, integration = await collector_with_integration(client, admin)
    group = await _group(
        client, admin, allow=["cable:read", "cable:manage", "rack:read", "equipment:read"],
        sites=[{"site_id": site_a["site"], "rack_scope": "all"}],
    )
    user, headers = await _group_user(client, admin, [group])
    await _assign(db_session, user["id"], await _role(db_session, ["discovery:reconcile"]), "global", None)
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()
    assert {"discovery:reconcile", "cable:manage"} <= set(me["permission_codes"]), "precondition: the handler is reachable"
    return {"admin": admin, "h": headers, "integration": integration["id"], "a1": a1, "a2": a2, "b1": b1, "b2": b2, "db": db_session}


async def neighbor(world, local: str | None, remote: str | None, *, state="confirmed", status="active") -> str:
    nid = str(uuid.uuid4())
    now = datetime.now(UTC)
    await world["db"].execute(
        text(
            "INSERT INTO discovered_neighbor (id, integration_id, protocol, identity_key, first_seen_at, last_seen_at, status,"
            " local_port_name, remote_chassis_ident, remote_port_ident, reconciliation_state, local_port_id, remote_port_id,"
            " capabilities, raw_evidence, match_evidence, version, created_at, updated_at) VALUES (:id, :i, 'lldp', :k, :t, :t,"
            " :status, 'p1', 'x', 'y', :state, :lp, :rp, '[]'::jsonb, '{}'::jsonb, '{}'::jsonb, 1, :t, :t)"
        ),
        {"id": nid, "i": world["integration"], "k": uuid.uuid4().hex, "t": now, "status": status, "state": state, "lp": local, "rp": remote},
    )
    await world["db"].commit()
    return nid


async def cable_count(world) -> int:
    return (await world["db"].execute(text("SELECT count(*) FROM cable"))).scalar_one()


def normalized(response) -> tuple:
    body = response.json()
    return response.status_code, body["title"], UUID_RE.sub("<id>", body["detail"])


async def post(client, world, nid: str):
    return await client.post(f"{CABLES}/from-neighbor/{nid}", json=BODY, headers=world["h"])


def hidden_ids(world) -> list[str]:
    ids = [world["b1"]["id"], world["b2"]["id"], *world["b1"]["port_by_name"].values(), *world["b2"]["port_by_name"].values()]
    return [*ids, "b1-sw", "b2-sw"]


async def test_hidden_ports_are_a_plain_404_naming_only_the_supplied_neighbor(client, world):
    nid = await neighbor(world, world["b1"]["port_by_name"]["p1"], world["b2"]["port_by_name"]["p1"])
    response = await post(client, world, nid)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == f"DiscoveredNeighbor {nid} not found."
    assert [s for s in hidden_ids(world) if s in response.text] == []
    assert await cable_count(world) == 0


@pytest.mark.parametrize(
    "state,status,already_cabled",
    [("confirmed", "active", False), ("proposed", "active", False), ("confirmed", "stale", False), ("confirmed", "active", True)],
    ids=["confirmed-active", "unconfirmed", "stale", "already-cabled"],
)
async def test_neighbor_state_is_not_observable_for_hidden_ports(client, world, state, status, already_cabled):
    """Every state a hidden neighbor can be in answers exactly like a neighbor that does not exist."""
    local, remote = world["b1"]["port_by_name"]["p2"], world["b2"]["port_by_name"]["p2"]
    nid = await neighbor(world, local, remote, state=state, status=status)
    if already_cabled:
        cable = await create_cable(client, world["admin"], local, remote, label="LIVE-B", status="installed")
        await world["db"].execute(text("UPDATE cable SET source_neighbor_id = :n WHERE id = :c"), {"n": nid, "c": cable["id"]})
        await world["db"].commit()
    hidden = await post(client, world, nid)
    missing = await post(client, world, str(uuid.uuid4()))
    assert normalized(hidden) == normalized(missing) == (404, "Not Found", "DiscoveredNeighbor <id> not found.")


@pytest.mark.parametrize("which", ["local-visible", "remote-visible"])
async def test_one_hidden_port_hides_the_whole_neighbor(client, world, which):
    visible, hidden = world["a1"]["port_by_name"]["p3"], world["b1"]["port_by_name"]["p3"]
    nid = await neighbor(world, visible if which == "local-visible" else hidden, hidden if which == "local-visible" else visible)
    response = await post(client, world, nid)
    assert normalized(response) == (404, "Not Found", "DiscoveredNeighbor <id> not found.")
    assert [s for s in hidden_ids(world) if s in response.text] == []
    assert await cable_count(world) == 0


async def test_a_visible_confirmed_neighbor_still_becomes_a_cable(client, world):
    """Legitimate reconciliation is preserved: both ports inside the caller's site."""
    nid = await neighbor(world, world["a1"]["port_by_name"]["p1"], world["a2"]["port_by_name"]["p1"])
    created = await post(client, world, nid)
    assert created.status_code == 201, created.text
    assert created.json()["source"] == "discovery_confirmed" and created.json()["source_neighbor_id"] == nid
    again = await post(client, world, nid)
    assert again.status_code == 409  # conflict handling for a neighbor the caller may see is unchanged


async def test_state_conflicts_remain_visible_for_neighbors_inside_scope(client, world):
    unconfirmed = await neighbor(world, world["a1"]["port_by_name"]["p2"], world["a2"]["port_by_name"]["p2"], state="proposed")
    stale = await neighbor(world, world["a1"]["port_by_name"]["p3"], world["a2"]["port_by_name"]["p3"], status="stale")
    assert (await post(client, world, unconfirmed)).status_code == 409
    assert (await post(client, world, stale)).status_code == 409
    assert await cable_count(world) == 0


async def test_unrestricted_administrator_keeps_the_full_diagnostics(client, world):
    nid = await neighbor(world, world["b1"]["port_by_name"]["p1"], world["b2"]["port_by_name"]["p1"], state="proposed")
    response = await client.post(f"{CABLES}/from-neighbor/{nid}", json=BODY, headers=world["admin"])
    assert response.status_code == 409 and "confirmed" in response.json()["detail"]
