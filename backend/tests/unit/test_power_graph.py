"""Unit-level (direct application-layer calls, real PostgreSQL session, no HTTP) tests
for app/application/power_graph.py — iterative traversal and cycle prevention.
PHASE3_GAP_ANALYSIS.md / PHASE3_IMPLEMENTATION_REPORT.md."""

import uuid

import pytest

from app.application.power_graph import (
    GraphTraversalBounded,
    WouldCreateCycle,
    assert_would_not_create_cycle,
    get_downstream_node_ids,
    get_upstream_node_ids,
)
from app.domain.power.models import PowerConnection, PowerNode


async def _make_node(db_session, node_type="utility_intake", label="n") -> uuid.UUID:
    # utility_intake is the one node_type the DB CHECK constraint allows with neither
    # managed_asset_id nor owning_asset_id set — the right choice for graph-shape-only
    # test fixtures that don't care about a node's real-world asset backing.
    node = PowerNode(node_type=node_type, label=label)
    db_session.add(node)
    await db_session.flush()
    return node.id


async def _connect(db_session, source_id, target_id, feed_label="single"):
    from datetime import UTC, datetime

    conn = PowerConnection(
        source_node_id=source_id, target_node_id=target_id, connection_type="feed", feed_label=feed_label,
        status="active", version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn)
    await db_session.flush()
    return conn


@pytest.mark.asyncio
async def test_downstream_traversal_simple_chain(db_session):
    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    c = await _make_node(db_session, label="c")
    await _connect(db_session, a, b)
    await _connect(db_session, b, c)

    downstream = await get_downstream_node_ids(db_session, a)
    assert downstream == {b, c}


@pytest.mark.asyncio
async def test_upstream_traversal_simple_chain(db_session):
    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    c = await _make_node(db_session, label="c")
    await _connect(db_session, a, b)
    await _connect(db_session, b, c)

    upstream = await get_upstream_node_ids(db_session, c)
    assert upstream == {a, b}


@pytest.mark.asyncio
async def test_downstream_traversal_branching(db_session):
    root = await _make_node(db_session, label="root")
    left = await _make_node(db_session, label="left")
    right = await _make_node(db_session, label="right")
    leaf = await _make_node(db_session, label="leaf")
    await _connect(db_session, root, left)
    await _connect(db_session, root, right)
    await _connect(db_session, left, leaf)

    downstream = await get_downstream_node_ids(db_session, root)
    assert downstream == {left, right, leaf}


@pytest.mark.asyncio
async def test_disconnected_node_has_no_downstream(db_session):
    isolated = await _make_node(db_session, label="isolated")
    assert await get_downstream_node_ids(db_session, isolated) == set()
    assert await get_upstream_node_ids(db_session, isolated) == set()


@pytest.mark.asyncio
async def test_retired_connection_effective_to_excluded_from_traversal(db_session):
    """A connection with effective_to set (disconnected) must not be walked."""
    from datetime import UTC, datetime

    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    conn = await _connect(db_session, a, b)
    conn.effective_to = datetime.now(UTC)
    await db_session.flush()

    assert await get_downstream_node_ids(db_session, a) == set()


@pytest.mark.asyncio
async def test_self_loop_rejected_by_cycle_check(db_session):
    a = await _make_node(db_session, label="a")
    with pytest.raises(WouldCreateCycle):
        await assert_would_not_create_cycle(db_session, source_node_id=a, target_node_id=a)


@pytest.mark.asyncio
async def test_two_node_cycle_rejected(db_session):
    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    await _connect(db_session, a, b)
    # a -> b already exists; b -> a would close a 2-node cycle.
    with pytest.raises(WouldCreateCycle):
        await assert_would_not_create_cycle(db_session, source_node_id=b, target_node_id=a)


@pytest.mark.asyncio
async def test_long_cycle_rejected(db_session):
    nodes = [await _make_node(db_session, label=f"n{i}") for i in range(6)]
    for i in range(5):
        await _connect(db_session, nodes[i], nodes[i + 1])
    # nodes[5] -> nodes[0] would close a 6-node cycle.
    with pytest.raises(WouldCreateCycle):
        await assert_would_not_create_cycle(db_session, source_node_id=nodes[5], target_node_id=nodes[0])


@pytest.mark.asyncio
async def test_valid_deep_chain_does_not_falsely_reject(db_session):
    """A genuinely deep, but acyclic, chain must not be mistaken for a cycle."""
    nodes = [await _make_node(db_session, label=f"deep{i}") for i in range(50)]
    for i in range(49):
        await _connect(db_session, nodes[i], nodes[i + 1])
    # Extending the chain further (not closing it) must succeed the check.
    extra = await _make_node(db_session, label="extra")
    await assert_would_not_create_cycle(db_session, source_node_id=nodes[49], target_node_id=extra)


@pytest.mark.asyncio
async def test_redundant_branch_is_not_a_cycle(db_session):
    """Two independent branches from the same source to different targets is ordinary
    redundant topology, not a cycle — must not be rejected."""
    source = await _make_node(db_session, label="source")
    branch_a = await _make_node(db_session, label="branch_a")
    branch_b = await _make_node(db_session, label="branch_b")
    await _connect(db_session, source, branch_a, feed_label="A")
    await assert_would_not_create_cycle(db_session, source_node_id=source, target_node_id=branch_b)


@pytest.mark.asyncio
async def test_traversal_bounded_on_pathological_depth(db_session, monkeypatch):
    """A deliberately tiny bound proves the traversal is genuinely enforced, not just
    documented — without needing to actually build thousands of real rows in a unit
    test."""
    import app.application.power_graph as power_graph

    monkeypatch.setattr(power_graph, "MAX_TRAVERSAL_DEPTH", 3)
    nodes = [await _make_node(db_session, label=f"bound{i}") for i in range(10)]
    for i in range(9):
        await _connect(db_session, nodes[i], nodes[i + 1])
    with pytest.raises(GraphTraversalBounded) as exc_info:
        await get_downstream_node_ids(db_session, nodes[0])
    assert exc_info.value.limit_kind == "depth"
