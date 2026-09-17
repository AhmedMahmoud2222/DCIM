"""Correctness oracle for the N+1 batch-loading correction (PHASE3_N1_CORRECTION_REPORT.md).

Every test below builds one topology, then asserts the NEW snapshot-based batch path
(`load_power_graph_snapshot` + `*_from_snapshot`) produces IDENTICAL results to the
OLD, already-proven-correct per-call path (`compute_allocated_kw`/`get_capacity_figures`/
`derive_node_capacity_exceptions`/`equipment_power_summary`), for the same database
state — written and run BEFORE the batch path replaced anything callers actually use
(`dashboard.py`), per the task's own "build the oracle before implementing" requirement.

Two cases (bounded-traversal-exceeded, pre-existing cycle) are DELIBERATELY divergence
cases, not equality cases — the batch path's whole-graph bound
(`MAX_BATCH_GRAPH_EDGES`) is a documented, intentional semantic change from the old
per-node bound (`MAX_TRAVERSAL_NODES`/`MAX_TRAVERSAL_DEPTH`), and the batch path's
explicit cycle-safety (`visiting` set) is a documented, intentional improvement over the
old recursive `node_value` closure's total lack of one. Both are asserted explicitly
below, not silently skipped."""

import uuid
from datetime import UTC, datetime

import pytest

from app.application.power_capacity import (
    classify_equipment_redundancy_from_snapshot,
    compute_allocated_kw,
    compute_allocated_kw_from_snapshot,
    derive_node_capacity_exceptions,
    derive_node_capacity_exceptions_from_snapshot,
    equipment_power_summary,
    get_capacity_figures,
    get_capacity_figures_from_snapshot,
    load_equipment_feed_batch,
    load_power_graph_snapshot,
)
from app.domain.identity.models import ManagedAsset
from app.domain.power.models import PowerCapacity, PowerConnection, PowerNode


async def _make_node(db_session, label="n", node_type="utility_intake", owning_asset_id=None) -> uuid.UUID:
    node = PowerNode(node_type=node_type, label=label, owning_asset_id=owning_asset_id)
    db_session.add(node)
    await db_session.flush()
    return node.id


async def _connect(db_session, source_id, target_id, feed_label="single"):
    conn = PowerConnection(
        source_node_id=source_id, target_node_id=target_id, connection_type="feed", feed_label=feed_label,
        status="active", version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn)
    await db_session.flush()
    return conn


async def _set_capacity(db_session, node_id, *, rated=None, configured=None, warning=None, critical=None):
    cap = PowerCapacity(
        power_node_id=node_id, rated_capacity_kw=rated, configured_capacity_kw=configured,
        warning_threshold_pct=warning, critical_threshold_pct=critical, version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(cap)
    await db_session.flush()
    return cap


async def _retire(db_session, node_id: uuid.UUID) -> None:
    node = await db_session.get(PowerNode, node_id)
    node.retired_at = datetime.now(UTC)
    await db_session.flush()


async def _make_equipment_asset(db_session, tag=None) -> uuid.UUID:
    asset = ManagedAsset(asset_type="equipment", asset_tag=tag or f"EQ-{uuid.uuid4().hex[:10]}", lifecycle_status="active")
    db_session.add(asset)
    await db_session.flush()
    return asset.id


async def _assert_figures_and_exceptions_match(db_session, node_id: uuid.UUID, label: str = "test-node"):
    old_figures = await get_capacity_figures(db_session, node_id)
    old_exceptions = await derive_node_capacity_exceptions(db_session, node_id, label)

    snapshot = await load_power_graph_snapshot(db_session)
    assert not snapshot.truncated
    memo: dict = {}
    new_figures = get_capacity_figures_from_snapshot(snapshot, node_id, memo)
    new_exceptions = derive_node_capacity_exceptions_from_snapshot(snapshot, node_id, label, memo)

    assert new_figures == old_figures, f"figures diverged for {label}: old={old_figures} new={new_figures}"
    old_codes = [(e.code, e.severity, e.observed_value, e.threshold) for e in old_exceptions]
    new_codes = [(e.code, e.severity, e.observed_value, e.threshold) for e in new_exceptions]
    assert new_codes == old_codes, f"exceptions diverged for {label}: old={old_codes} new={new_codes}"
    return old_figures, old_exceptions


async def _assert_equipment_summary_matches(db_session, asset_id: uuid.UUID):
    old_summary = await equipment_power_summary(db_session, asset_id)

    snapshot = await load_power_graph_snapshot(db_session)
    assert not snapshot.truncated
    feed_nodes_by_asset, feed_label_by_node = await load_equipment_feed_batch(db_session, [asset_id])
    memo: dict = {}
    new_summary = classify_equipment_redundancy_from_snapshot(
        snapshot, asset_id, feed_nodes_by_asset.get(asset_id, []), feed_label_by_node, memo
    )

    assert new_summary.redundancy_classification == old_summary.redundancy_classification
    assert new_summary.effective_demand_kw == old_summary.effective_demand_kw
    assert new_summary.data_quality == old_summary.data_quality
    # feed_nodes is a list of dicts keyed identically by both paths -- compare as sets of
    # sorted-item tuples so ordering (which neither path documents or guarantees) never
    # causes a false mismatch.
    old_feeds = {tuple(sorted(f.items())) for f in old_summary.feed_nodes}
    new_feeds = {tuple(sorted(f.items())) for f in new_summary.feed_nodes}
    assert new_feeds == old_feeds, f"feed_nodes diverged: old={old_summary.feed_nodes} new={new_summary.feed_nodes}"
    return old_summary


# ------------------------------------------------------------------------- 1. linear chain
@pytest.mark.asyncio
async def test_oracle_linear_chain(db_session):
    a = await _make_node(db_session, "a")
    b = await _make_node(db_session, "b")
    c = await _make_node(db_session, "c")
    await _connect(db_session, a, b)
    await _connect(db_session, b, c)
    await _set_capacity(db_session, c, rated=10)
    await _assert_figures_and_exceptions_match(db_session, a)
    await _assert_figures_and_exceptions_match(db_session, b)


# ------------------------------------------------------------------------- 2. branching
@pytest.mark.asyncio
async def test_oracle_branching(db_session):
    root = await _make_node(db_session, "root")
    c1 = await _make_node(db_session, "c1")
    c2 = await _make_node(db_session, "c2")
    c3 = await _make_node(db_session, "c3")
    for child in (c1, c2, c3):
        await _connect(db_session, root, child)
    await _set_capacity(db_session, c1, rated=5)
    await _set_capacity(db_session, c2, rated=7)
    await _set_capacity(db_session, c3, rated=9)
    await _set_capacity(db_session, root, rated=100)
    await _assert_figures_and_exceptions_match(db_session, root)


# ------------------------------------------------------------------------- 3. multiple upstream sources (diamond)
@pytest.mark.asyncio
async def test_oracle_diamond_multiple_upstream_sources(db_session):
    top = await _make_node(db_session, "top")
    left = await _make_node(db_session, "left")
    right = await _make_node(db_session, "right")
    bottom = await _make_node(db_session, "bottom")
    await _connect(db_session, top, left)
    await _connect(db_session, top, right)
    await _connect(db_session, left, bottom)
    await _connect(db_session, right, bottom)
    await _set_capacity(db_session, bottom, rated=20)
    await _set_capacity(db_session, top, rated=100)
    await _assert_figures_and_exceptions_match(db_session, top)
    await _assert_figures_and_exceptions_match(db_session, left)


# ------------------------------------------------------------------------- 4. A/B redundant feeds (healthy)
@pytest.mark.asyncio
async def test_oracle_ab_redundant_feeds_healthy(db_session):
    asset_id = await _make_equipment_asset(db_session)
    utility_a = await _make_node(db_session, "utility_a")
    utility_b = await _make_node(db_session, "utility_b")
    feed_a = await _make_node(db_session, "feed_a", node_type="equipment_power_input", owning_asset_id=asset_id)
    feed_b = await _make_node(db_session, "feed_b", node_type="equipment_power_input", owning_asset_id=asset_id)
    await _connect(db_session, utility_a, feed_a, feed_label="A")
    await _connect(db_session, utility_b, feed_b, feed_label="B")
    await _set_capacity(db_session, feed_a, rated=10)
    await _set_capacity(db_session, feed_b, rated=10)
    summary = await _assert_equipment_summary_matches(db_session, asset_id)
    assert summary.redundancy_classification == "dual_feed_healthy"


# ------------------------------------------------------------------------- 5. shared upstream (degraded)
@pytest.mark.asyncio
async def test_oracle_shared_upstream_degrades_redundancy(db_session):
    asset_id = await _make_equipment_asset(db_session)
    shared_utility = await _make_node(db_session, "shared_utility")
    feed_a = await _make_node(db_session, "feed_a", node_type="equipment_power_input", owning_asset_id=asset_id)
    feed_b = await _make_node(db_session, "feed_b", node_type="equipment_power_input", owning_asset_id=asset_id)
    await _connect(db_session, shared_utility, feed_a, feed_label="A")
    await _connect(db_session, shared_utility, feed_b, feed_label="B")
    summary = await _assert_equipment_summary_matches(db_session, asset_id)
    assert summary.redundancy_classification == "degraded"


# ------------------------------------------------------------------------- 6. retired intermediate node
@pytest.mark.asyncio
async def test_oracle_retired_intermediate_node(db_session):
    utility = await _make_node(db_session, "utility")
    retired_pdu = await _make_node(db_session, "retired_pdu")
    live_sibling = await _make_node(db_session, "live_sibling")
    await _connect(db_session, utility, retired_pdu)
    await _connect(db_session, utility, live_sibling)
    await _set_capacity(db_session, retired_pdu, rated=999)
    await _set_capacity(db_session, live_sibling, rated=10)
    await _retire(db_session, retired_pdu)
    await _assert_figures_and_exceptions_match(db_session, utility)


# ------------------------------------------------------------------------- 7. retired leaf
@pytest.mark.asyncio
async def test_oracle_retired_leaf_own_traversal_still_visible(db_session):
    """Model A root exemption: a retired node's OWN figures remain directly queryable
    (historical/audit inspection), even though it is excluded as an ancestor's bridge."""
    utility = await _make_node(db_session, "utility")
    leaf = await _make_node(db_session, "leaf")
    await _connect(db_session, utility, leaf)
    await _set_capacity(db_session, leaf, rated=5)
    await _retire(db_session, leaf)
    await _assert_figures_and_exceptions_match(db_session, leaf)
    await _assert_figures_and_exceptions_match(db_session, utility)


# ------------------------------------------------------------------------- 8. unknown capacity
@pytest.mark.asyncio
async def test_oracle_unknown_capacity_no_record(db_session):
    node = await _make_node(db_session, "node")
    await _assert_figures_and_exceptions_match(db_session, node)


# ------------------------------------------------------------------------- 9. capacity overload
@pytest.mark.asyncio
async def test_oracle_capacity_overload(db_session):
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=100, critical=90, warning=70)
    await _set_capacity(db_session, child, rated=95)
    old_figures, old_exceptions = await _assert_figures_and_exceptions_match(db_session, parent)
    assert {e.code for e in old_exceptions} == {"CAPACITY_OVERLOAD"}


# ------------------------------------------------------------------------- 10. near-limit capacity
@pytest.mark.asyncio
async def test_oracle_near_limit_capacity(db_session):
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=100, critical=90, warning=70)
    await _set_capacity(db_session, child, rated=75)
    old_figures, old_exceptions = await _assert_figures_and_exceptions_match(db_session, parent)
    assert {e.code for e in old_exceptions} == {"CAPACITY_NEAR_LIMIT"}


# ------------------------------------------------------------------------- 11. disconnected node
@pytest.mark.asyncio
async def test_oracle_disconnected_node(db_session):
    node = await _make_node(db_session, "isolated")
    await _set_capacity(db_session, node, rated=42)
    await _assert_figures_and_exceptions_match(db_session, node)


# ------------------------------------------------------------------------- bonus: F-H2 equipment redundancy
# through a retired upstream bridge (Utility -> Retired PDU -> Equipment feed) -- the
# highest-risk case for the batch rewrite's equipment-redundancy path specifically,
# since `_upstream_ids_from_snapshot`'s retirement filtering is new code, not a
# refactor of something already covered by cases 6/7 above (which only exercise the
# downstream `children_of` filtering, not the upstream `parents_of` filtering
# `equipment_power_summary`'s "has_upstream_path" actually depends on).
@pytest.mark.asyncio
async def test_oracle_equipment_fed_only_through_retired_upstream_bridge(db_session):
    asset_id = await _make_equipment_asset(db_session)
    utility = await _make_node(db_session, "utility")
    retired_pdu = await _make_node(db_session, "retired_pdu")
    feed = await _make_node(db_session, "feed", node_type="equipment_power_input", owning_asset_id=asset_id)
    await _connect(db_session, utility, retired_pdu)
    await _connect(db_session, retired_pdu, feed, feed_label="single")
    await _retire(db_session, retired_pdu)

    summary = await _assert_equipment_summary_matches(db_session, asset_id)
    assert summary.feed_nodes[0]["has_upstream_path"] is False, (
        "a feed reachable only through a retired bridge must show no upstream path in both paths"
    )


# ------------------------------------------------------------------------- 12. bounded-traversal-exceeded
# DELIBERATE DIVERGENCE (documented, not a defect): the batch path's whole-graph bound
# (MAX_BATCH_GRAPH_EDGES) is checked once for the whole snapshot, not per-root like the
# old per-node MAX_TRAVERSAL_NODES/DEPTH bound -- so a small test graph that trips the
# old bound (monkeypatched low here, exactly like
# tests/unit/test_power_capacity.py's own F-H1 regression test) does NOT trip the new
# bound at all, and the batch path correctly resolves a value the old path could not.
# This is the exact, intentional semantic change PHASE3_INDEPENDENT_VALIDATION_REPORT.md
# Section 17 named in advance ("converting today's per-node bound into a whole-dashboard
# bound"), not a silent behavior change discovered after the fact.
@pytest.mark.asyncio
async def test_oracle_bounded_traversal_documented_divergence(db_session, monkeypatch):
    # `compute_allocated_kw` imports MAX_TRAVERSAL_NODES by value (`from
    # app.application.power_graph import MAX_TRAVERSAL_NODES`), so the name that
    # actually governs its bound-check lives in power_capacity's own namespace, not
    # power_graph's -- patch it there, not on power_graph itself.
    import app.application.power_capacity as power_capacity

    monkeypatch.setattr(power_capacity, "MAX_TRAVERSAL_NODES", 2)

    root = await _make_node(db_session, "root")
    await _set_capacity(db_session, root, rated=100)
    prev = root
    for i in range(5):
        child = await _make_node(db_session, f"chain{i}")
        await _connect(db_session, prev, child)
        prev = child
    await _set_capacity(db_session, prev, rated=5)  # capacity exists at the chain's END

    old_figures = await get_capacity_figures(db_session, root)
    assert old_figures.data_quality == "unknown", "sanity check: old path really does hit its per-node bound here"

    snapshot = await load_power_graph_snapshot(db_session)
    assert not snapshot.truncated, "5 nodes is far below MAX_BATCH_GRAPH_EDGES -- the whole-graph bound must not fire"
    new_figures = get_capacity_figures_from_snapshot(snapshot, root, {})
    assert new_figures.data_quality == "known", (
        "documented divergence: the batch path is not bounded per-root, so it correctly "
        "resolves a value the old per-node-bounded path could not -- this is the intended "
        "effect of the whole-graph bound replacing the per-node one, not a bug"
    )


# ------------------------------------------------------------------------- 13. pre-existing cycle (raw SQL bypass)
# DELIBERATE DIVERGENCE (documented, not a defect): F-C1's advisory lock prevents any
# API-driven mutation from ever creating a cycle; this constructs one directly via the
# ORM session (bypassing the API entirely, the same "direct SQL bypass" class of attack
# PHASE3_CORRECTION_HOSTILE_REAUDIT.md already exercised for the cycle-prevention check
# itself) specifically to prove the batch path's explicit cycle guard does not hang or
# crash on data the application itself would never produce, but a bulk-import or manual
# repair script conceivably could.
@pytest.mark.asyncio
async def test_oracle_preexisting_cycle_batch_path_does_not_hang_or_crash(db_session):
    a = await _make_node(db_session, "a")
    b = await _make_node(db_session, "b")
    c = await _make_node(db_session, "c")
    await _connect(db_session, a, b)
    await _connect(db_session, b, c)
    await _connect(db_session, c, a)  # closes the cycle -- never possible through the API
    await _set_capacity(db_session, a, rated=10)

    snapshot = await load_power_graph_snapshot(db_session)
    assert not snapshot.truncated
    # Must return promptly with SOME (value, quality) tuple -- not hang, not raise
    # RecursionError. The exact quality is intentionally not asserted as "known" or
    # "unknown" here -- what matters is that a cycle cannot make this call fail.
    value, quality = compute_allocated_kw_from_snapshot(snapshot, a, {})
    assert quality in ("known", "unknown", "not_applicable")
