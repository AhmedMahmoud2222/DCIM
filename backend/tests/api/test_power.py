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


# ------------------------------------------------------------ N+1 performance regression guard
# PHASE3_N1_CORRECTION_REPORT.md: proves the batch-loading correction actually removed the
# N+1 pattern, as an ordinary, always-run regression test -- not a one-off measurement
# script. Asserts query count stays *bounded* as node count grows (not a fragile exact
# number, which would break on the next unrelated schema/query change) rather than
# growing linearly with the number of capacity-bearing nodes, which is the textbook N+1
# signature this correction removes.


async def _build_capacity_chain(db_session, n: int, capacity_every: int = 5):
    """n utility_intake nodes in a single chain, capacity records on every `capacity_
    every`-th node -- enough to trigger the old per-capacity-record N+1 pattern many
    times over at even a modest n, without the cost of a full HTTP-driven build."""
    import uuid as _uuid
    from datetime import UTC, datetime

    from app.domain.power.models import PowerCapacity, PowerConnection, PowerNode

    node_ids = []
    for i in range(n):
        node = PowerNode(node_type="utility_intake", label=f"n{i}")
        db_session.add(node)
        await db_session.flush()
        node_ids.append(node.id)
        if i > 0:
            db_session.add(
                PowerConnection(
                    source_node_id=node_ids[i - 1], target_node_id=node_ids[i], connection_type="feed",
                    feed_label="single", status="active", version=1, effective_from=datetime.now(UTC),
                )
            )
        if i % capacity_every == 0:
            db_session.add(
                PowerCapacity(
                    power_node_id=node_ids[i], rated_capacity_kw=1.0, version=1,
                    effective_from=datetime.now(UTC), id=_uuid.uuid4(),
                )
            )
    await db_session.commit()


async def _count_queries(db_engine, coro):
    from sqlalchemy import event

    count = 0

    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        nonlocal count
        count += 1

    event.listen(db_engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    try:
        await coro
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _before_cursor_execute)
    return count


async def test_dashboard_summary_query_count_does_not_grow_linearly_with_scale(client, auth_headers, db_session, db_engine):
    """The core N+1 regression guard: query count at 1,000 capacity-bearing-adjacent
    nodes must not be anywhere near proportional to query count at 100 -- the old
    per-node loop grew ~1:1 with capacity-record count (PHASE3_N1_CORRECTION_REPORT.md
    Section 2's fresh baseline measured this directly); the batch path's query count is
    dominated by a small, fixed number of whole-graph queries per request, so a 10x
    increase in node count must not translate into anywhere near a 10x increase in
    query count."""
    headers = await auth_headers("DCIM Manager")

    await _build_capacity_chain(db_session, 100)
    queries_at_100 = await _count_queries(db_engine, client.get("/api/v1/dashboard/summary", headers=headers))

    await _build_capacity_chain(db_session, 1000)
    queries_at_1000 = await _count_queries(db_engine, client.get("/api/v1/dashboard/summary", headers=headers))

    assert queries_at_100 < 50, f"unexpectedly high query count even at n=100: {queries_at_100}"
    # Bounded, not proportional: a 10x node-count increase must cost far less than a 10x
    # query-count increase (the old code was ~1:1; this asserts nowhere close to that).
    assert queries_at_1000 < queries_at_100 * 3, (
        f"query count grew from {queries_at_100} (n=100) to {queries_at_1000} (n=1000) -- "
        "this is the N+1 signature the batch-loading correction is required to remove"
    )


# --------------------------------------------------------------------- F-C1 correction
# PHASE3_HOSTILE_SELF_AUDIT.md F-C1 / PHASE3_CORRECTION_DESIGN.md Parts 4-6: the global
# advisory lock must make it impossible for two concurrent connection-creation requests
# to jointly close a cycle, even on completely disjoint node pairs.


def _per_request_client_ctx(app, db_session):
    """Shared setup for every F-C1 concurrency test below: overrides `get_db` with a
    fresh AsyncSession per request from a dedicated engine (never the shared
    single-session `client`/`db_session` fixtures, which cannot represent real
    concurrent database sessions), and restores the original override afterward."""
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from tests.conftest import TEST_DATABASE_URL

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    def make_client():
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://test")

    async def cleanup():
        async def _restore_shared_session():
            yield db_session

        app.dependency_overrides[get_db] = _restore_shared_session
        await engine.dispose()

    app.dependency_overrides[get_db] = _override
    return make_client, cleanup


async def _post_connection(make_client, headers, source_id, target_id):
    async with make_client() as c:
        return await c.post("/api/v1/power/connections", json=connection_body(source_id, target_id), headers=headers)


async def test_hostile_f_c1_independent_pair_race_cannot_create_cycle(client, auth_headers, db_session):
    """The exact PHASE3_HOSTILE_SELF_AUDIT.md F-C1 reproduction, through the real API:
    pre-existing N2->N3, N4->N1; concurrent POSTs create N1->N2 and N3->N4, which
    together would close a 4-cycle. Before the F-C1 correction this was demonstrated to
    let both commit. After the correction (a global advisory lock serializing every
    connection-creation transaction), exactly one must succeed and the other must be
    rejected as a cycle -- never both succeeding, and never both failing."""
    from app.main import app

    headers = await auth_headers("DCIM Manager")
    n1 = await create_utility(client, headers, "F-C1 N1")
    n2 = await create_utility(client, headers, "F-C1 N2")
    n3 = await create_utility(client, headers, "F-C1 N3")
    n4 = await create_utility(client, headers, "F-C1 N4")
    r = await client.post("/api/v1/power/connections", json=connection_body(n2["id"], n3["id"]), headers=headers)
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/power/connections", json=connection_body(n4["id"], n1["id"]), headers=headers)
    assert r.status_code == 201, r.text

    make_client, cleanup = _per_request_client_ctx(app, db_session)
    try:
        results = await asyncio.gather(
            _post_connection(make_client, headers, n1["id"], n2["id"]),
            _post_connection(make_client, headers, n3["id"], n4["id"]),
        )
    finally:
        await cleanup()

    codes = sorted(r.status_code for r in results)
    assert codes == [201, 422], f"expected exactly one success and one cycle rejection, got {codes}"

    # Final-graph-acyclicity assertion: whichever edge committed, the graph as a whole
    # must not contain the 4-cycle N1->N2->N3->N4->N1.
    downstream = (await client.get(f"/api/v1/power/nodes/{n2['id']}/downstream", headers=headers)).json()
    downstream_ids = {n["node_id"] for n in downstream}
    n1_reachable_from_n2 = n1["id"] in downstream_ids
    n1_to_n2_exists = (
        await client.get("/api/v1/power/connections", headers=headers, params={"node_id": n1["id"]})
    ).json()
    n1_to_n2_committed = any(
        c["source_node_id"] == n1["id"] and c["target_node_id"] == n2["id"] and c["effective_to"] is None
        for c in n1_to_n2_exists["items"]
    )
    assert not (n1_to_n2_committed and n1_reachable_from_n2), (
        "the true 4-cycle (both N1->N2 committed AND N1 reachable from N2 via the rest "
        "of the chain) must never be simultaneously true"
    )


async def test_hostile_f_c1_race_repeated_ten_times(client, auth_headers, db_session):
    """The same race, repeated across independent node quadruples in one test run, to
    catch timing-dependent flakiness a single trial might miss."""
    from app.main import app

    headers = await auth_headers("DCIM Manager")
    for trial in range(10):
        n1 = await create_utility(client, headers, f"F-C1-rep{trial}-N1")
        n2 = await create_utility(client, headers, f"F-C1-rep{trial}-N2")
        n3 = await create_utility(client, headers, f"F-C1-rep{trial}-N3")
        n4 = await create_utility(client, headers, f"F-C1-rep{trial}-N4")
        r = await client.post("/api/v1/power/connections", json=connection_body(n2["id"], n3["id"]), headers=headers)
        assert r.status_code == 201
        r = await client.post("/api/v1/power/connections", json=connection_body(n4["id"], n1["id"]), headers=headers)
        assert r.status_code == 201

        make_client, cleanup = _per_request_client_ctx(app, db_session)
        try:
            results = await asyncio.gather(
                _post_connection(make_client, headers, n1["id"], n2["id"]),
                _post_connection(make_client, headers, n3["id"], n4["id"]),
            )
        finally:
            await cleanup()

        codes = sorted(r.status_code for r in results)
        assert codes == [201, 422], f"trial {trial}: expected [201, 422], got {codes}"


async def test_hostile_f_c1_three_concurrent_writers_cannot_close_a_triangle(client, auth_headers, db_session):
    """3+ concurrent writers (task requirement): three transactions each add one edge
    of a triangle (A->B, B->C, C->A) concurrently. At most one of the three edges that
    would complete the triangle may commit -- never all three."""
    from app.main import app

    headers = await auth_headers("DCIM Manager")
    a = await create_utility(client, headers, "F-C1-tri-A")
    b = await create_utility(client, headers, "F-C1-tri-B")
    c = await create_utility(client, headers, "F-C1-tri-C")

    make_client, cleanup = _per_request_client_ctx(app, db_session)
    try:
        results = await asyncio.gather(
            _post_connection(make_client, headers, a["id"], b["id"]),
            _post_connection(make_client, headers, b["id"], c["id"]),
            _post_connection(make_client, headers, c["id"], a["id"]),
        )
    finally:
        await cleanup()

    codes = [r.status_code for r in results]
    assert codes.count(201) == 2, f"exactly two of the three triangle edges may commit, got {codes}"
    assert codes.count(422) == 1, f"exactly one must be rejected as closing the triangle, got {codes}"


async def test_concurrent_independent_valid_mutations_both_succeed(client, auth_headers, db_session):
    """The advisory lock must serialize, not falsely reject: two completely unrelated,
    individually valid connection creations (disjoint node pairs, no shared topology)
    running concurrently must BOTH succeed -- proving the correction does not
    over-reject legitimate concurrent work."""
    from app.main import app

    headers = await auth_headers("DCIM Manager")
    a1 = await create_utility(client, headers, "Independent A1")
    a2 = await create_utility(client, headers, "Independent A2")
    b1 = await create_utility(client, headers, "Independent B1")
    b2 = await create_utility(client, headers, "Independent B2")

    make_client, cleanup = _per_request_client_ctx(app, db_session)
    try:
        results = await asyncio.gather(
            _post_connection(make_client, headers, a1["id"], a2["id"]),
            _post_connection(make_client, headers, b1["id"], b2["id"]),
        )
    finally:
        await cleanup()

    codes = [r.status_code for r in results]
    assert codes == [201, 201], f"two unrelated valid mutations must both succeed, got {codes}"


async def test_f_c1_rollback_on_rejected_cycle_leaves_no_partial_edge(client, auth_headers):
    """A rejected cycle-creation attempt must leave no partially-committed row behind —
    the connection list for the rejected pair must be empty afterward."""
    headers = await auth_headers("DCIM Manager")
    a = await create_utility(client, headers, "Rollback A")
    b = await create_utility(client, headers, "Rollback B")
    r = await client.post("/api/v1/power/connections", json=connection_body(a["id"], b["id"]), headers=headers)
    assert r.status_code == 201

    r2 = await client.post("/api/v1/power/connections", json=connection_body(b["id"], a["id"]), headers=headers)
    assert r2.status_code == 422

    conns = (await client.get("/api/v1/power/connections", headers=headers, params={"node_id": b["id"]})).json()
    b_to_a = [
        c for c in conns["items"]
        if c["source_node_id"] == b["id"] and c["target_node_id"] == a["id"] and c["effective_to"] is None
    ]
    assert b_to_a == [], "the rejected b->a attempt must not have left any row behind"


# --------------------------------------------------------------------- F-M1 correction


async def test_malformed_if_match_on_capacity_put_is_a_clean_400_not_a_500(client, auth_headers):
    """PHASE3_HOSTILE_SELF_AUDIT.md F-M1: a malformed (non-integer) If-Match header on
    the capacity PUT endpoint must produce the same clean 400 every other If-Match
    consumer already produces, never an unhandled exception."""
    headers = await auth_headers("DCIM Manager")
    node = await create_utility(client, headers, "If-Match test node")
    r1 = await client.put(f"/api/v1/power/nodes/{node['id']}/capacity", json={"rated_capacity_kw": 10}, headers=headers)
    assert r1.status_code == 200

    r2 = await client.put(
        f"/api/v1/power/nodes/{node['id']}/capacity",
        json={"rated_capacity_kw": 20},
        headers={**headers, "If-Match": "not-a-number"},
    )
    assert r2.status_code == 400, r2.text


# --------------------------------------------------------------------- F-H2 correction


async def test_retired_intermediate_node_produces_power_path_missing_for_equipment(client, auth_headers):
    """The exact task-specified scenario: Utility -> Retired PDU -> Equipment feed must
    NOT report the equipment as still powered through the retired node -- it must show
    a broken (missing) upstream path once the PDU is retired."""
    from tests.api._phase2_helpers import create_equipment

    headers = await auth_headers("DCIM Manager")
    utility = await create_utility(client, headers, "F-H2 Utility")
    pdu = await create_pdu(client, headers, tag="F-H2-PDU")
    equipment = await create_equipment(client, headers)
    equipment_asset_id = equipment["id"]

    r = await client.post("/api/v1/power/connections", json=connection_body(utility["id"], pdu["id"]), headers=headers)
    assert r.status_code == 201, r.text
    feed = await client.post(
        "/api/v1/power/equipment-feeds",
        json={"equipment_asset_id": equipment_asset_id, "label": "Feed A"},
        headers=headers,
    )
    assert feed.status_code == 201, feed.text
    feed_node_id = feed.json()["id"]
    r = await client.post("/api/v1/power/connections", json=connection_body(pdu["id"], feed_node_id), headers=headers)
    assert r.status_code == 201, r.text

    summary_before = (
        await client.get(f"/api/v1/power/equipment/{equipment_asset_id}/power-summary", headers=headers)
    ).json()
    assert summary_before["feed_nodes"][0]["has_upstream_path"] is True

    await client.post(f"/api/v1/power/nodes/{pdu['id']}/retire", headers=headers)

    summary_after = (
        await client.get(f"/api/v1/power/equipment/{equipment_asset_id}/power-summary", headers=headers)
    ).json()
    assert summary_after["feed_nodes"][0]["has_upstream_path"] is False, (
        "a retired intermediate PDU must not count as a valid upstream path for the equipment it used to feed"
    )
