"""Unit-level (direct application-layer calls, real PostgreSQL session) tests for
app/application/power_capacity.py — capacity roll-up, unit handling, redundancy
classification, and unknown-data semantics. PHASE3_GAP_ANALYSIS.md."""

import uuid
from datetime import UTC, datetime

import pytest

from app.application.power_capacity import (
    compute_allocated_kw,
    derive_node_capacity_exceptions,
    get_capacity_figures,
)
from app.domain.power.models import PowerCapacity, PowerConnection, PowerNode


async def _make_node(db_session, label="n") -> uuid.UUID:
    node = PowerNode(node_type="utility_intake", label=label)
    db_session.add(node)
    await db_session.flush()
    return node.id


async def _connect(db_session, source_id, target_id):
    conn = PowerConnection(
        source_node_id=source_id, target_node_id=target_id, connection_type="feed", feed_label="single",
        status="active", version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn)
    await db_session.flush()


async def _set_capacity(db_session, node_id, *, rated=None, configured=None, warning=None, critical=None):
    cap = PowerCapacity(
        power_node_id=node_id, rated_capacity_kw=rated, configured_capacity_kw=configured,
        warning_threshold_pct=warning, critical_threshold_pct=critical, version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(cap)
    await db_session.flush()
    return cap


@pytest.mark.asyncio
async def test_effective_capacity_prefers_configured_over_rated(db_session):
    node = await _make_node(db_session)
    await _set_capacity(db_session, node, rated=100, configured=80)
    figures = await get_capacity_figures(db_session, node)
    assert figures.effective_capacity_kw == 80.0


@pytest.mark.asyncio
async def test_effective_capacity_falls_back_to_rated_when_configured_unset(db_session):
    node = await _make_node(db_session)
    await _set_capacity(db_session, node, rated=100, configured=None)
    figures = await get_capacity_figures(db_session, node)
    assert figures.effective_capacity_kw == 100.0


@pytest.mark.asyncio
async def test_no_capacity_record_is_unknown_not_zero(db_session):
    node = await _make_node(db_session)
    figures = await get_capacity_figures(db_session, node)
    assert figures.effective_capacity_kw is None
    assert figures.data_quality == "unknown"
    # Never fabricated as zero:
    assert figures.effective_capacity_kw != 0


@pytest.mark.asyncio
async def test_allocated_kw_sums_direct_children_with_their_own_capacity(db_session):
    parent = await _make_node(db_session, "parent")
    child_a = await _make_node(db_session, "child_a")
    child_b = await _make_node(db_session, "child_b")
    await _connect(db_session, parent, child_a)
    await _connect(db_session, parent, child_b)
    await _set_capacity(db_session, child_a, rated=10)
    await _set_capacity(db_session, child_b, rated=15)

    allocated, quality = await compute_allocated_kw(db_session, parent)
    assert allocated == 25.0
    assert quality == "known"


@pytest.mark.asyncio
async def test_allocated_kw_expands_past_children_with_no_capacity_record(db_session):
    """A child with no capacity record of its own contributes its OWN children's sum
    instead — bottom-up expansion, not double-counted against the child's own (absent)
    record."""
    parent = await _make_node(db_session, "parent")
    middle = await _make_node(db_session, "middle")  # no capacity record
    leaf = await _make_node(db_session, "leaf")
    await _connect(db_session, parent, middle)
    await _connect(db_session, middle, leaf)
    await _set_capacity(db_session, leaf, rated=42)

    allocated, quality = await compute_allocated_kw(db_session, parent)
    assert allocated == 42.0
    assert quality == "known"


@pytest.mark.asyncio
async def test_allocated_kw_does_not_double_count_past_a_node_with_its_own_record(db_session):
    """A node WITH its own capacity record represents everything below it already —
    expanding past it too would double-count."""
    parent = await _make_node(db_session, "parent")
    middle = await _make_node(db_session, "middle")
    leaf = await _make_node(db_session, "leaf")
    await _connect(db_session, parent, middle)
    await _connect(db_session, middle, leaf)
    await _set_capacity(db_session, middle, rated=30)  # middle has its own record
    await _set_capacity(db_session, leaf, rated=999)  # must NOT also be summed

    allocated, quality = await compute_allocated_kw(db_session, parent)
    assert allocated == 30.0
    assert quality == "known"


@pytest.mark.asyncio
async def test_allocated_kw_unknown_when_a_branch_bottoms_out_unresolved(db_session):
    parent = await _make_node(db_session, "parent")
    dead_end = await _make_node(db_session, "dead_end")  # leaf, no capacity record
    await _connect(db_session, parent, dead_end)

    allocated, quality = await compute_allocated_kw(db_session, parent)
    assert allocated is None
    assert quality == "unknown"


@pytest.mark.asyncio
async def test_allocated_kw_not_applicable_for_a_leaf(db_session):
    leaf = await _make_node(db_session, "leaf")
    allocated, quality = await compute_allocated_kw(db_session, leaf)
    assert allocated is None
    assert quality == "not_applicable"


@pytest.mark.asyncio
async def test_available_and_utilization_computed_when_both_known(db_session):
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=100)
    await _set_capacity(db_session, child, rated=60)

    figures = await get_capacity_figures(db_session, parent)
    assert figures.effective_capacity_kw == 100.0
    assert figures.allocated_kw == 60.0
    assert figures.available_kw == 40.0
    assert figures.utilization_pct == 60.0
    assert figures.data_quality == "known"


@pytest.mark.asyncio
async def test_overload_is_negative_available_not_clamped(db_session):
    """§8: negative available capacity explicitly represents overload, never clamped."""
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=50)
    await _set_capacity(db_session, child, rated=80)

    figures = await get_capacity_figures(db_session, parent)
    assert figures.available_kw == -30.0
    assert figures.utilization_pct == 160.0


@pytest.mark.asyncio
async def test_zero_effective_capacity_never_divides_by_zero(db_session):
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=0)
    await _set_capacity(db_session, child, rated=5)

    figures = await get_capacity_figures(db_session, parent)
    assert figures.utilization_pct is None  # not_applicable, never a ZeroDivisionError
    assert figures.data_quality == "not_applicable"


@pytest.mark.asyncio
async def test_capacity_overload_exception_at_or_above_critical_threshold(db_session):
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=100, critical=90, warning=70)
    await _set_capacity(db_session, child, rated=95)  # 95% >= 90% critical

    exceptions = await derive_node_capacity_exceptions(db_session, parent, "test-node")
    codes = {e.code for e in exceptions}
    assert "CAPACITY_OVERLOAD" in codes


@pytest.mark.asyncio
async def test_capacity_near_limit_exception_between_warning_and_critical(db_session):
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=100, critical=90, warning=70)
    await _set_capacity(db_session, child, rated=75)  # 75% between 70 and 90

    exceptions = await derive_node_capacity_exceptions(db_session, parent, "test-node")
    codes = {e.code for e in exceptions}
    assert codes == {"CAPACITY_NEAR_LIMIT"}


@pytest.mark.asyncio
async def test_capacity_healthy_utilization_raises_no_exception(db_session):
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=100, critical=90, warning=70)
    await _set_capacity(db_session, child, rated=20)  # 20%, healthy

    exceptions = await derive_node_capacity_exceptions(db_session, parent, "test-node")
    assert exceptions == []


@pytest.mark.asyncio
async def test_capacity_unknown_exception_when_no_capacity_data_at_all(db_session):
    node = await _make_node(db_session, "node")
    await _set_capacity(db_session, node)  # a record exists but both fields are None
    exceptions = await derive_node_capacity_exceptions(db_session, node, "test-node")
    codes = {e.code for e in exceptions}
    assert codes == {"CAPACITY_UNKNOWN"}


@pytest.mark.asyncio
async def test_default_thresholds_apply_when_node_has_none_configured(db_session):
    """§13: thresholds fall back to the module defaults (80% warning / 95% critical)
    when the node's own record doesn't specify one."""
    parent = await _make_node(db_session, "parent")
    child = await _make_node(db_session, "child")
    await _connect(db_session, parent, child)
    await _set_capacity(db_session, parent, rated=100)  # no thresholds set
    await _set_capacity(db_session, child, rated=85)  # 85% -> above default warning (80)

    exceptions = await derive_node_capacity_exceptions(db_session, parent, "test-node")
    codes = {e.code for e in exceptions}
    assert "CAPACITY_NEAR_LIMIT" in codes


# ------------------------------------------------------------------------- F-H1 regression
# PHASE3_HOSTILE_SELF_AUDIT.md F-H1 / PHASE3_CORRECTION_DESIGN.md Part 7: a node whose OWN
# capacity is known but whose ALLOCATION is unknown (bounded traversal) must never produce
# an empty exception list -- it must produce CAPACITY_UNKNOWN.


@pytest.mark.asyncio
async def test_capacity_unknown_emitted_when_capacity_known_but_allocation_bounded(db_session, monkeypatch):
    """The exact hostile-audit F-H1 reproduction: root has a known rated_capacity_kw:
    its allocation roll-up is forced to hit the traversal bound (simulating a large
    real deployment), which must still surface CAPACITY_UNKNOWN -- not an empty list."""
    import app.application.power_graph as power_graph

    monkeypatch.setattr(power_graph, "MAX_TRAVERSAL_NODES", 2)

    root = await _make_node(db_session, "root")
    await _set_capacity(db_session, root, rated=100)
    prev = root
    for i in range(5):
        child = await _make_node(db_session, f"chain{i}")
        await _connect(db_session, prev, child)
        prev = child

    figures = await get_capacity_figures(db_session, root)
    assert figures.effective_capacity_kw == 100.0, "root's own capacity must remain known"
    assert figures.allocated_kw is None
    assert figures.data_quality == "unknown"

    exceptions = await derive_node_capacity_exceptions(db_session, root, "test-node")
    codes = {e.code for e in exceptions}
    assert codes == {"CAPACITY_UNKNOWN"}, (
        f"a node with known capacity but unknown allocation must surface CAPACITY_UNKNOWN, got {codes}"
    )


@pytest.mark.asyncio
async def test_capacity_unknown_does_not_also_report_overload_or_near_limit(db_session, monkeypatch):
    """CAPACITY_UNKNOWN must be the only exception for a node whose utilization
    genuinely cannot be computed -- it must never coexist with a fabricated
    OVERLOAD/NEAR_LIMIT conclusion drawn from incomplete data."""
    import app.application.power_graph as power_graph

    monkeypatch.setattr(power_graph, "MAX_TRAVERSAL_NODES", 2)

    root = await _make_node(db_session, "root")
    await _set_capacity(db_session, root, rated=1, critical=1, warning=1)  # tiny thresholds
    prev = root
    for i in range(5):
        child = await _make_node(db_session, f"chain{i}")
        await _connect(db_session, prev, child)
        prev = child

    exceptions = await derive_node_capacity_exceptions(db_session, root, "test-node")
    codes = {e.code for e in exceptions}
    assert codes == {"CAPACITY_UNKNOWN"}
    assert "CAPACITY_OVERLOAD" not in codes
    assert "CAPACITY_NEAR_LIMIT" not in codes


# ------------------------------------------------------------------------- F-H2 regression
# PHASE3_HOSTILE_SELF_AUDIT.md F-H2 / PHASE3_CORRECTION_DESIGN.md Part 8 (Model A): a
# retired node's own capacity must not contribute to an ancestor's allocated_kw roll-up.


async def _retire(db_session, node_id: uuid.UUID) -> None:
    node = await db_session.get(PowerNode, node_id)
    node.retired_at = datetime.now(UTC)
    await db_session.flush()


@pytest.mark.asyncio
async def test_retired_pdu_capacity_excluded_from_ancestor_allocated_kw(db_session):
    """Utility -> PDU (retired, 5kW) must not count that 5kW toward the utility's own
    allocated_kw once the PDU is retired -- retirement must have an immediate,
    operationally meaningful effect without a separate manual disconnect step."""
    utility = await _make_node(db_session, "utility")
    pdu = await _make_node(db_session, "pdu")
    await _connect(db_session, utility, pdu)
    await _set_capacity(db_session, pdu, rated=5)

    allocated, quality = await compute_allocated_kw(db_session, utility)
    assert allocated == 5.0 and quality == "known", "sanity check: allocation counts the live PDU"

    await _retire(db_session, pdu)

    allocated_after, quality_after = await compute_allocated_kw(db_session, utility)
    assert allocated_after is None or allocated_after == 0.0, (
        f"a retired PDU's capacity must not count toward its ancestor's allocated_kw, got {allocated_after}"
    )
    assert quality_after == "not_applicable", (
        "with its only child retired, the utility has nothing left to allocate -- "
        f"expected not_applicable, got {quality_after!r}"
    )


@pytest.mark.asyncio
async def test_retired_intermediate_does_not_poison_ancestor_with_unknown_quality(db_session):
    """A retired node with no capacity record and no live children must not spuriously
    appear as an 'unknown' child in its parent's allocation sum (a phantom child that
    was never excluded from `children_of` would corrupt this to 'unknown')."""
    utility = await _make_node(db_session, "utility")
    retired_intermediate = await _make_node(db_session, "retired_intermediate")
    live_sibling = await _make_node(db_session, "live_sibling")
    await _connect(db_session, utility, retired_intermediate)
    await _connect(db_session, utility, live_sibling)
    await _set_capacity(db_session, live_sibling, rated=10)
    await _retire(db_session, retired_intermediate)

    allocated, quality = await compute_allocated_kw(db_session, utility)
    assert quality == "known", f"a retired sibling with no capacity must not poison quality to 'unknown', got {quality}"
    assert allocated == 10.0, f"only the live sibling's capacity should count, got {allocated}"
