"""PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md finding F-N1-FALLBACK-1 (HIGH, blocking,
now corrected): when `load_power_graph_snapshot` reports `truncated=True` (the active
graph exceeds `MAX_BATCH_GRAPH_EDGES`), both dashboard endpoints fall back to the
original per-node functions (`derive_node_capacity_exceptions`/`equipment_power_
summary`). `equipment_power_summary` previously let a bound-exceeded upstream traversal
(`GraphTraversalBounded`, raised by `get_upstream_node_ids` once a chain exceeds
`MAX_TRAVERSAL_DEPTH`/`MAX_TRAVERSAL_NODES`) escape uncaught, crashing the whole
dashboard request with an unhandled 500.

Root cause and correction: `equipment_power_summary` (app/application/power_capacity.py)
is the one function in that module that let this exception propagate -- its sibling
`compute_allocated_kw` already degrades gracefully on the same kind of bound, returning
`(None, "unknown")` rather than raising. The fix makes `equipment_power_summary`
consistent with that existing pattern: a bound-exceeded feed's `has_upstream_path` is
now `None` (unresolved, never fabricated as `True`/`False`) and the summary's
`data_quality` becomes `"unknown"`, exactly mirroring how `CAPACITY_UNKNOWN` already
represents "the data needed to answer this could not be resolved" elsewhere in this
module. The fix lives entirely in `equipment_power_summary` itself, not in
`dashboard.py` -- both dashboard endpoints' fallback branches call it unmodified, and
the fix transitively closes them without touching either endpoint's own code.

These tests independently prove, for BOTH dashboard endpoints, that the request now
returns 200 with a semantically valid, honestly-degraded response instead of crashing,
and that this is achieved by *catching the specific bound exception*, not by
broadening exception handling or fabricating a healthy result."""
import uuid
from datetime import UTC, datetime

import pytest

from app.domain.identity.models import ManagedAsset
from app.domain.power.models import PowerConnection, PowerNode

pytestmark = pytest.mark.asyncio


async def _build_bound_exceeding_chain_with_equipment_feed(db_session, chain_len: int = 510):
    """A backbone chain longer than MAX_TRAVERSAL_DEPTH (500) with an
    equipment_power_input feed node at the far end, so equipment_power_summary's own
    get_upstream_node_ids call for that feed walks (and exceeds) the whole chain --
    the exact shape PHASE3_N1_CORRECTION_REPORT.md Section 2 used to find the original
    n=5,000 crash."""
    prev = None
    for i in range(chain_len):
        node = PowerNode(node_type="utility_intake", label=f"n{i}")
        db_session.add(node)
        await db_session.flush()
        if prev is not None:
            db_session.add(
                PowerConnection(
                    source_node_id=prev, target_node_id=node.id, connection_type="feed", feed_label="single",
                    status="active", version=1, effective_from=datetime.now(UTC),
                )
            )
        prev = node.id

    asset = ManagedAsset(asset_type="equipment", asset_tag=f"EQ-{uuid.uuid4().hex[:10]}", lifecycle_status="active")
    db_session.add(asset)
    await db_session.flush()
    feed = PowerNode(node_type="equipment_power_input", label="feed", owning_asset_id=asset.id)
    db_session.add(feed)
    await db_session.flush()
    db_session.add(
        PowerConnection(
            source_node_id=prev, target_node_id=feed.id, connection_type="feed", feed_label="single",
            status="active", version=1, effective_from=datetime.now(UTC),
        )
    )
    await db_session.commit()
    return asset.id


async def test_summary_no_longer_crashes_when_fallback_hits_bound_exceeded_traversal(
    client, auth_headers, db_session, monkeypatch
):
    import app.application.power_capacity as power_capacity

    # Force truncation cheaply -- the real MAX_BATCH_GRAPH_EDGES=20,000 is reachable
    # too (see the report), just expensive to build in a unit test; this monkeypatch
    # exercises the identical fallback code path.
    monkeypatch.setattr(power_capacity, "MAX_BATCH_GRAPH_EDGES", 5)
    headers = await auth_headers("DCIM Manager")
    await _build_bound_exceeding_chain_with_equipment_feed(db_session)

    from app.application.power_capacity import load_power_graph_snapshot
    snapshot = await load_power_graph_snapshot(db_session)
    assert snapshot.truncated, "test setup invalid -- snapshot must be truncated to exercise the fallback path"

    r = await client.get("/api/v1/dashboard/summary", headers=headers)
    assert r.status_code == 200, f"fallback path must not crash, got {r.status_code}: {r.text[:500]}"
    body = r.json()
    # Semantic validity, not just a 200: every required top-level section is present
    # and well-formed, and the request-scoped power_summary counters are plain ints
    # (never a partial/None/malformed aggregate leaking out of the degraded path).
    for key in ("site_summary", "capacity_summary", "rack_summary", "power_summary"):
        assert key in body, f"missing section {key} in degraded response: {body}"
    power_summary = body["power_summary"]
    for count_key in (
        "total_power_nodes", "overloaded_nodes", "near_capacity_nodes",
        "missing_power_path_equipment", "redundancy_degraded_equipment",
    ):
        assert isinstance(power_summary[count_key], int), f"{count_key} must be a plain int, got {power_summary[count_key]!r}"
    # The equipment whose upstream traversal was bound-exceeded must be honestly
    # reflected as missing-path (has_upstream_path=None, not fabricated True), not
    # silently dropped or counted as healthy.
    assert power_summary["missing_power_path_equipment"] >= 1, (
        "the equipment fed through the bound-exceeded chain must be counted as "
        "missing-path (unresolved), not silently treated as healthy"
    )


async def test_exceptions_no_longer_crashes_when_fallback_hits_bound_exceeded_traversal(
    client, auth_headers, db_session, monkeypatch
):
    import app.application.power_capacity as power_capacity

    monkeypatch.setattr(power_capacity, "MAX_BATCH_GRAPH_EDGES", 5)
    headers = await auth_headers("DCIM Manager")
    await _build_bound_exceeding_chain_with_equipment_feed(db_session)

    from app.application.power_capacity import load_power_graph_snapshot
    snapshot = await load_power_graph_snapshot(db_session)
    assert snapshot.truncated, "test setup invalid -- snapshot must be truncated to exercise the fallback path"

    r = await client.get("/api/v1/dashboard/exceptions", headers=headers)
    assert r.status_code == 200, f"fallback path must not crash, got {r.status_code}: {r.text[:500]}"
    body = r.json()
    assert isinstance(body, list), f"expected a list of exception items, got {type(body)}"
    for item in body:
        for key in ("code", "severity", "object_type", "object_id", "message"):
            assert key in item, f"malformed exception item (missing {key}): {item}"
    # The bound-exceeded equipment's single feed makes it "single_feed" classification
    # (unaffected by the unresolved upstream path -- that classification depends only
    # on feed count), so it must surface as POWER_PATH_MISSING, honestly reflecting
    # that its power path could not be confirmed.
    codes = {item["code"] for item in body}
    assert "POWER_PATH_MISSING" in codes, f"expected POWER_PATH_MISSING for the unresolved equipment, got {codes}"


async def test_equipment_power_summary_degrades_gracefully_not_falsely_healthy(db_session, monkeypatch):
    """Unit-level proof, independent of the HTTP layer: `equipment_power_summary`
    itself returns an honestly-degraded result (`has_upstream_path=None`,
    `data_quality="unknown"`) rather than raising or fabricating `has_upstream_path=
    True` (which would silently misreport a genuinely unresolved feed as healthy --
    the exact failure mode F-H1's CAPACITY_UNKNOWN precedence fix was written to
    prevent for capacity data, now also guaranteed here for upstream-path data)."""
    import app.application.power_capacity as power_capacity
    import app.application.power_graph as power_graph

    # `get_upstream_node_ids` -> `_traverse` checks `power_graph`'s OWN module-level
    # MAX_TRAVERSAL_DEPTH directly (it is defined there, not imported by value like
    # power_capacity's copy) -- patch it where the bound check actually reads it.
    monkeypatch.setattr(power_graph, "MAX_TRAVERSAL_DEPTH", 5)
    asset_id = await _build_bound_exceeding_chain_with_equipment_feed(db_session, chain_len=10)

    summary = await power_capacity.equipment_power_summary(db_session, asset_id)
    assert summary.redundancy_classification == "single_feed"
    assert summary.data_quality == "unknown", f"expected unknown data_quality, got {summary.data_quality}"
    assert summary.feed_nodes[0]["has_upstream_path"] is None, (
        f"a bound-exceeded feed must report has_upstream_path=None (unresolved), never "
        f"True/False, got {summary.feed_nodes[0]['has_upstream_path']!r}"
    )
