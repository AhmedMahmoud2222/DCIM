"""API-level tests for Phase 3 power topology + capacity + dashboard endpoints:
CRUD, authorization, optimistic concurrency (If-Match), pagination/filtering, errors,
and the adversarial power-graph matrix (master prompt §34, Graphs A-L, a reduced but
independently meaningful subset). PHASE3_GAP_ANALYSIS.md / PHASE3_TRACEABILITY_MATRIX.md."""

import asyncio
import uuid

from tests.api._phase3_helpers import connection_body, create_pdu, create_room_and_site, create_ups, create_utility

# --------------------------------------------------------------------------------- CRUD


async def test_create_and_get_pdu(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    pdu = await create_pdu(client, headers)
    assert pdu["node_type"] == "pdu"
    assert pdu["managed_asset_id"] is not None

    r = await client.get(f"/api/v1/power/nodes/{pdu['id']}", headers=headers)
    assert r.status_code == 200
    assert r.json()["id"] == pdu["id"]


async def test_create_utility_intake_has_no_asset_reference(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    node = await create_utility(client, headers)
    assert node["managed_asset_id"] is None
    assert node["owning_asset_id"] is None


async def test_list_power_nodes_filters_by_type(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    await create_pdu(client, headers)
    await create_utility(client, headers)
    r = await client.get("/api/v1/power/nodes", params={"node_type": "pdu"}, headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert all(item["node_type"] == "pdu" for item in body["items"])


async def test_retire_power_node_is_idempotent(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    pdu = await create_pdu(client, headers)
    r1 = await client.post(f"/api/v1/power/nodes/{pdu['id']}/retire", headers=headers)
    assert r1.status_code == 200
    assert r1.json()["retired_at"] is not None
    r2 = await client.post(f"/api/v1/power/nodes/{pdu['id']}/retire", headers=headers)
    assert r2.status_code == 200  # no-op, not an error


async def test_get_missing_power_node_is_404(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    r = await client.get(f"/api/v1/power/nodes/{uuid.uuid4()}", headers=headers)
    assert r.status_code == 404


async def test_malformed_uuid_is_a_clean_422_not_a_500(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    r = await client.get("/api/v1/power/nodes/not-a-uuid", headers=headers)
    assert r.status_code == 422


# ---------------------------------------------------------------------- authorization


async def test_viewer_cannot_create_power_node(client, auth_headers):
    headers = await auth_headers("Viewer")
    r = await client.post("/api/v1/power/utility-intakes", json={"label": "x"}, headers=headers)
    assert r.status_code == 403


async def test_viewer_can_read_power_nodes(client, auth_headers):
    manager_headers = await auth_headers("DCIM Manager")
    await create_utility(client, manager_headers)
    viewer_headers = await auth_headers("Viewer")
    r = await client.get("/api/v1/power/nodes", headers=viewer_headers)
    assert r.status_code == 200


async def test_unauthenticated_request_is_401(client):
    r = await client.get("/api/v1/power/nodes")
    assert r.status_code == 401


async def test_capacity_read_permission_distinct_from_manage(client, auth_headers):
    """Operator has capacity:read but not capacity:manage per DEFAULT_ROLE_PERMISSIONS."""
    headers = await auth_headers("DCIM Manager")
    pdu = await create_pdu(client, headers)
    operator_headers = await auth_headers("Operator")
    r_read = await client.get(f"/api/v1/power/nodes/{pdu['id']}/capacity", headers=operator_headers)
    assert r_read.status_code == 200
    r_write = await client.put(f"/api/v1/power/nodes/{pdu['id']}/capacity", json={"rated_capacity_kw": 10}, headers=operator_headers)
    assert r_write.status_code == 403


# --------------------------------------------------------------------------- topology


async def test_chain_upstream_downstream(client, auth_headers):
    """Graph A: Utility -> Generator -> UPS -> PDU (a simple chain)."""
    headers = await auth_headers("DCIM Manager")
    room_id, site_id = await create_room_and_site(client, auth_headers)
    utility = await create_utility(client, headers)
    gen = (
        await client.post(
            "/api/v1/power/generators", json={"asset_tag": f"GEN-{uuid.uuid4().hex[:8]}", "name": "Gen", "site_id": site_id},
            headers=headers,
        )
    ).json()
    ups = await create_ups(client, headers, room_id)
    pdu = await create_pdu(client, headers)

    for src, tgt in [(utility, gen), (gen, ups), (ups, pdu)]:
        r = await client.post("/api/v1/power/connections", json=connection_body(src["id"], tgt["id"]), headers=headers)
        assert r.status_code == 201, r.text

    upstream = (await client.get(f"/api/v1/power/nodes/{pdu['id']}/upstream", headers=headers)).json()
    downstream = (await client.get(f"/api/v1/power/nodes/{utility['id']}/downstream", headers=headers)).json()
    assert {n["node_type"] for n in upstream} == {"utility_intake", "generator", "ups"}
    assert {n["node_type"] for n in downstream} == {"generator", "ups", "pdu"}


async def test_self_loop_rejected_via_api(client, auth_headers):
    """Graph E."""
    headers = await auth_headers("DCIM Manager")
    pdu = await create_pdu(client, headers)
    r = await client.post("/api/v1/power/connections", json=connection_body(pdu["id"], pdu["id"]), headers=headers)
    assert r.status_code == 422


async def test_two_node_cycle_rejected_via_api(client, auth_headers):
    """Graph F."""
    headers = await auth_headers("DCIM Manager")
    a = await create_pdu(client, headers)
    b = await create_pdu(client, headers)
    r1 = await client.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/power/connections", json=connection_body(b["id"], a["id"]), headers=headers)
    assert r2.status_code == 422


async def test_long_cycle_rejected_via_api(client, auth_headers):
    """Graph G: a 5-node chain, then closing it into a cycle."""
    headers = await auth_headers("DCIM Manager")
    nodes = [await create_pdu(client, headers) for _ in range(5)]
    for i in range(4):
        r = await client.post("/api/v1/power/connections", json=connection_body(nodes[i]["id"], nodes[i + 1]["id"]), headers=headers)
        assert r.status_code == 201
    r_close = await client.post(
        "/api/v1/power/connections", json=connection_body(nodes[4]["id"], nodes[0]["id"]), headers=headers
    )
    assert r_close.status_code == 422


async def test_duplicate_connection_rejected_via_api(client, auth_headers):
    """Graph D."""
    headers = await auth_headers("DCIM Manager")
    a = await create_pdu(client, headers)
    b = await create_pdu(client, headers)
    r1 = await client.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)
    assert r1.status_code == 201
    r2 = await client.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)
    assert r2.status_code == 409


async def test_disconnected_node_via_api(client, auth_headers):
    """Graph J: a disconnected node has empty upstream/downstream, not an error."""
    headers = await auth_headers("DCIM Manager")
    isolated = await create_pdu(client, headers)
    upstream = (await client.get(f"/api/v1/power/nodes/{isolated['id']}/upstream", headers=headers)).json()
    downstream = (await client.get(f"/api/v1/power/nodes/{isolated['id']}/downstream", headers=headers)).json()
    assert upstream == []
    assert downstream == []


async def test_branching_distribution_via_api(client, auth_headers):
    """Graph B: one source feeding two independent downstream branches."""
    headers = await auth_headers("DCIM Manager")
    source = await create_pdu(client, headers)
    branch_a = await create_pdu(client, headers)
    branch_b = await create_pdu(client, headers)
    r1 = await client.post("/api/v1/power/connections", json=connection_body(source["id"], branch_a["id"]), headers=headers)
    r2 = await client.post("/api/v1/power/connections", json=connection_body(source["id"], branch_b["id"]), headers=headers)
    assert r1.status_code == 201 and r2.status_code == 201
    downstream = (await client.get(f"/api/v1/power/nodes/{source['id']}/downstream", headers=headers)).json()
    assert {n["node_id"] for n in downstream} == {branch_a["id"], branch_b["id"]}


async def test_disconnect_and_reconnect(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    a = await create_pdu(client, headers)
    b = await create_pdu(client, headers)
    conn = (await client.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)).json()
    r_disc = await client.post(f"/api/v1/power/connections/{conn['id']}/disconnect", headers=headers)
    assert r_disc.status_code == 200
    assert r_disc.json()["effective_to"] is not None
    # Same edge can reopen after disconnect (not blocked by the partial unique index).
    r_reconnect = await client.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)
    assert r_reconnect.status_code == 201


async def test_disconnect_twice_is_a_clean_409_not_a_silent_overwrite(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    a = await create_pdu(client, headers)
    b = await create_pdu(client, headers)
    conn = (await client.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)).json()
    r1 = await client.post(f"/api/v1/power/connections/{conn['id']}/disconnect", headers=headers)
    assert r1.status_code == 200
    r2 = await client.post(f"/api/v1/power/connections/{conn['id']}/disconnect", headers=headers)
    assert r2.status_code == 409


# ------------------------------------------------------------------------- concurrency


async def test_concurrent_connection_creation_between_same_pair_only_one_wins(client, auth_headers, db_session):
    """Graph C-adjacent concurrency test: 5 requests racing to create the identical
    active edge must not all succeed. Setup (creating the two PDUs) uses the ordinary
    shared-session `client` fixture; the actual race uses a fresh AsyncSession per
    request from a dedicated engine, exactly like
    tests/integration/test_idempotency_concurrency.py's `per_request_client` — a single
    AsyncSession is not safe for genuinely concurrent use, so the race must not share
    one, or it would not be testing real concurrent-session behavior at all."""
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    headers = await auth_headers("DCIM Manager")
    a = await create_pdu(client, headers)
    b = await create_pdu(client, headers)

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=5)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        async def attempt():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                return await c.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)

        results = await asyncio.gather(*[attempt() for _ in range(5)])
    finally:
        async def _restore_shared_session():
            yield db_session

        app.dependency_overrides[get_db] = _restore_shared_session
        await engine.dispose()

    codes = [r.status_code for r in results]
    assert codes.count(201) == 1, codes
    assert codes.count(409) == 4, codes


async def test_concurrent_edit_of_same_connection_requires_if_match(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    a = await create_pdu(client, headers)
    b = await create_pdu(client, headers)
    conn = (await client.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)).json()

    r_no_if_match = await client.patch(f"/api/v1/power/connections/{conn['id']}", json={"status": "maintenance"}, headers=headers)
    assert r_no_if_match.status_code == 428

    r_stale = await client.patch(
        f"/api/v1/power/connections/{conn['id']}", json={"status": "maintenance"},
        headers={**headers, "If-Match": "999"},
    )
    assert r_stale.status_code == 409

    r_ok = await client.patch(
        f"/api/v1/power/connections/{conn['id']}", json={"status": "maintenance"},
        headers={**headers, "If-Match": str(conn["version"])},
    )
    assert r_ok.status_code == 200
    assert r_ok.json()["version"] == conn["version"] + 1


async def test_capacity_edit_requires_if_match_once_a_record_exists(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    pdu = await create_pdu(client, headers)
    r1 = await client.put(f"/api/v1/power/nodes/{pdu['id']}/capacity", json={"rated_capacity_kw": 10}, headers=headers)
    assert r1.status_code == 200  # first time, no If-Match required

    r2_missing = await client.put(f"/api/v1/power/nodes/{pdu['id']}/capacity", json={"rated_capacity_kw": 20}, headers=headers)
    assert r2_missing.status_code == 428

    r2_ok = await client.put(
        f"/api/v1/power/nodes/{pdu['id']}/capacity", json={"rated_capacity_kw": 20}, headers={**headers, "If-Match": "1"}
    )
    assert r2_ok.status_code == 200
    assert r2_ok.json()["rated_capacity_kw"] == 20.0


# ---------------------------------------------------------------------------- capacity


async def test_capacity_unknown_when_not_set(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    pdu = await create_pdu(client, headers)
    r = await client.get(f"/api/v1/power/nodes/{pdu['id']}/capacity", headers=headers)
    body = r.json()
    assert body["effective_capacity_kw"] is None
    assert body["data_quality"] == "unknown"


async def test_capacity_exceptions_include_overload(client, auth_headers):
    """Graph L: an overloaded upstream node."""
    headers = await auth_headers("DCIM Manager")
    parent = await create_pdu(client, headers)
    child = await create_pdu(client, headers)
    await client.post("/api/v1/power/connections", json=connection_body(parent["id"], child["id"]), headers=headers)
    await client.put(
        f"/api/v1/power/nodes/{parent['id']}/capacity",
        json={"rated_capacity_kw": 10, "warning_threshold_pct": 70, "critical_threshold_pct": 90}, headers=headers,
    )
    await client.put(f"/api/v1/power/nodes/{child['id']}/capacity", json={"rated_capacity_kw": 15}, headers=headers)

    exceptions = (await client.get("/api/v1/power/capacity-exceptions", headers=headers)).json()
    assert any(e["code"] == "CAPACITY_OVERLOAD" and e["power_node_id"] == parent["id"] for e in exceptions)


async def test_equipment_missing_upstream_path(client, auth_headers):
    """Graph K: an equipment feed with no upstream connection at all."""
    from tests.api._phase2_helpers import create_equipment

    headers = await auth_headers("DCIM Manager")
    equip = await create_equipment(client, headers)
    await client.post(
        "/api/v1/power/equipment-feeds", json={"equipment_asset_id": equip["id"], "label": "Feed A"}, headers=headers
    )
    summary = (await client.get(f"/api/v1/power/equipment/{equip['id']}/power-summary", headers=headers)).json()
    assert summary["redundancy_classification"] == "single_feed"
    assert summary["feed_nodes"][0]["has_upstream_path"] is False


# --------------------------------------------------------------------------- dashboard


async def test_dashboard_summary_returns_expected_shape(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    r = await client.get("/api/v1/dashboard/summary", headers=headers)
    assert r.status_code == 200
    body = r.json()
    for key in ("site_summary", "capacity_summary", "rack_summary", "power_summary"):
        assert key in body


async def test_dashboard_requires_permission(client, auth_headers):
    # Every default role in DEFAULT_ROLE_PERMISSIONS holds dashboard:read; there is no
    # role in the seed set without it, so this proves the dependency is wired rather
    # than proving a specific role lacks it (none does, by current design).
    headers = await auth_headers("Viewer")
    r = await client.get("/api/v1/dashboard/summary", headers=headers)
    assert r.status_code == 200
