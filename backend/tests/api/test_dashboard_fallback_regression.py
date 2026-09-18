"""PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md finding F-N1-FALLBACK-1 (HIGH, blocking):
when `load_power_graph_snapshot` reports `truncated=True` (the active graph exceeds
`MAX_BATCH_GRAPH_EDGES`), both dashboard endpoints fall back to the ORIGINAL per-node
functions (`derive_node_capacity_exceptions`/`equipment_power_summary`) -- which is
exactly the code path PHASE3_N1_CORRECTION_REPORT.md's own Section 2 proved crashes with
an uncaught `GraphTraversalBounded` -> 500 once a traversal (here, the equipment loop's
`get_upstream_node_ids` call) exceeds `MAX_TRAVERSAL_DEPTH`/`MAX_TRAVERSAL_NODES`.
Neither `get_dashboard_summary` nor `get_dashboard_exceptions` wraps its fallback-branch
calls in a `try/except GraphTraversalBounded`, so a graph large enough to trigger
BOTH bounds (>20,000 active edges AND a >500-node backbone chain somewhere in it -- not
mutually exclusive, and plausible at a scale only ~4x this phase's own stated 5,000+
power-node target, especially once A/B redundant feeds roughly double edge count) still
crashes the dashboard exactly as it did before the N+1 correction. The correction's
"n=5,000 crash is fixed" claim holds only strictly below the truncation threshold; above
it, the identical defect the correction was written to close is still fully reachable.

This is deliberately left as `xfail(strict=True)`, not silently fixed, per the audit's
own "no silent fixes" rule -- fixing it is out of this audit's scope. `strict=True`
means this test will start FAILING the suite (as "unexpectedly passing") the moment a
future correction actually closes this gap, which is the intended trigger to remove the
marker."""
import uuid
from datetime import UTC, datetime

import pytest

from app.domain.identity.models import ManagedAsset
from app.domain.power.models import PowerConnection, PowerNode


@pytest.mark.xfail(
    reason="F-N1-FALLBACK-1: truncation fallback reintroduces the uncaught "
    "GraphTraversalBounded->500 crash the N+1 correction claimed to eliminate "
    "(see PHASE3_FINAL_INDEPENDENT_REAUDIT_REPORT.md)",
    strict=True,
)
async def test_fallback_path_can_still_crash_with_graph_traversal_bounded(client, auth_headers, db_session, monkeypatch):
    import app.application.power_capacity as power_capacity

    # Force truncation cheaply in this test -- the real MAX_BATCH_GRAPH_EDGES=20,000
    # is reachable too (see report), just expensive to build in a unit test.
    monkeypatch.setattr(power_capacity, "MAX_BATCH_GRAPH_EDGES", 5)

    headers = await auth_headers("DCIM Manager")

    # Backbone chain longer than MAX_TRAVERSAL_DEPTH (500), matching the exact shape
    # that crashed get_dashboard_summary at n=5,000 in PHASE3_N1_CORRECTION_REPORT.md
    # Section 2 -- an equipment_power_input feed node hanging off the far end, so
    # equipment_power_summary's own get_upstream_node_ids call walks the whole chain.
    prev = None
    for i in range(510):
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

    from app.application.power_capacity import load_power_graph_snapshot
    snapshot = await load_power_graph_snapshot(db_session)
    assert snapshot.truncated, "test setup invalid -- snapshot must be truncated to exercise the fallback path"

    r = await client.get("/api/v1/dashboard/summary", headers=headers)
    assert r.status_code == 200, (
        f"fallback path crashed with status {r.status_code} -- the original "
        f"GraphTraversalBounded-escapes-uncaught defect is still reachable through the "
        f"truncation fallback"
    )
