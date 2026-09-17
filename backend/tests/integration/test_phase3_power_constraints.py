"""Direct DB-level verification of Phase 3's power topology/capacity invariants
(ARCHITECTURE_REVIEW.md §13/§13a/§14; migration 0006) — bypasses the API/application
layer entirely, inserting through the ORM so each constraint is proven to be enforced by
PostgreSQL itself. Mirrors test_phase2_placement_constraints.py's style."""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy.exc

from app.domain.power.models import PowerCapacity, PowerConnection, PowerNode


async def _make_node(db_session, node_type="utility_intake", label="n") -> PowerNode:
    node = PowerNode(node_type=node_type, label=label)
    db_session.add(node)
    await db_session.flush()
    return node


@pytest.mark.asyncio
async def test_self_loop_rejected_at_db_level(db_session):
    node = await _make_node(db_session)
    conn = PowerConnection(
        source_node_id=node.id, target_node_id=node.id, connection_type="feed", feed_label="single",
        status="active", version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="no_self_loop"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_duplicate_active_edge_rejected_at_db_level(db_session):
    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    conn1 = PowerConnection(
        source_node_id=a.id, target_node_id=b.id, connection_type="feed", feed_label="single", status="active",
        version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn1)
    await db_session.flush()

    conn2 = PowerConnection(
        source_node_id=a.id, target_node_id=b.id, connection_type="feed", feed_label="single", status="active",
        version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn2)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="uq_power_connection_active_edge"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_two_different_feed_labels_between_same_nodes_is_allowed(db_session):
    """A and B feeds between the same pair of nodes is legitimate redundant topology,
    not a duplicate — must NOT be rejected."""
    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    db_session.add(
        PowerConnection(
            source_node_id=a.id, target_node_id=b.id, connection_type="feed", feed_label="A", status="active",
            version=1, effective_from=datetime.now(UTC),
        )
    )
    await db_session.flush()
    db_session.add(
        PowerConnection(
            source_node_id=a.id, target_node_id=b.id, connection_type="feed", feed_label="B", status="active",
            version=1, effective_from=datetime.now(UTC),
        )
    )
    await db_session.flush()  # must not raise


@pytest.mark.asyncio
async def test_reopening_a_disconnected_edge_is_allowed(db_session):
    """A previously-disconnected (effective_to set) identical edge does not block a new
    active one — the partial unique index only covers effective_to IS NULL rows."""
    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    old = PowerConnection(
        source_node_id=a.id, target_node_id=b.id, connection_type="feed", feed_label="single", status="active",
        version=1, effective_from=datetime.now(UTC), effective_to=datetime.now(UTC),
    )
    db_session.add(old)
    await db_session.flush()
    new = PowerConnection(
        source_node_id=a.id, target_node_id=b.id, connection_type="feed", feed_label="single", status="active",
        version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(new)
    await db_session.flush()  # must not raise


@pytest.mark.asyncio
async def test_orphan_source_node_rejected_by_fk(db_session):
    b = await _make_node(db_session, label="b")
    conn = PowerConnection(
        source_node_id=uuid.uuid4(), target_node_id=b.id, connection_type="feed", feed_label="single",
        status="active", version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn)
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        await db_session.flush()


@pytest.mark.asyncio
async def test_invalid_connection_type_rejected(db_session):
    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    conn = PowerConnection(
        source_node_id=a.id, target_node_id=b.id, connection_type="not_a_real_type", feed_label="single",
        status="active", version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="connection_type_allowed"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_negative_voltage_rejected(db_session):
    a = await _make_node(db_session, label="a")
    b = await _make_node(db_session, label="b")
    conn = PowerConnection(
        source_node_id=a.id, target_node_id=b.id, connection_type="feed", feed_label="single", status="active",
        version=1, effective_from=datetime.now(UTC), voltage=-208,
    )
    db_session.add(conn)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="voltage_positive"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_power_node_neither_asset_nor_utility_rejected(db_session):
    """A non-utility_intake node with neither managed_asset_id nor owning_asset_id set
    must be rejected — the module docstring's trichotomy, enforced at the DB level."""
    node = PowerNode(node_type="power_circuit", label="orphaned")
    db_session.add(node)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="asset_reference_exclusive"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_negative_rated_capacity_rejected(db_session):
    node = await _make_node(db_session)
    cap = PowerCapacity(power_node_id=node.id, rated_capacity_kw=-10, version=1, effective_from=datetime.now(UTC))
    db_session.add(cap)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="rated_capacity_kw_non_negative"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_warning_threshold_above_critical_rejected(db_session):
    node = await _make_node(db_session)
    cap = PowerCapacity(
        power_node_id=node.id, warning_threshold_pct=95, critical_threshold_pct=80, version=1,
        effective_from=datetime.now(UTC),
    )
    db_session.add(cap)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="warning_le_critical"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_duplicate_current_capacity_record_rejected(db_session):
    """At most one *current* (effective_to IS NULL) capacity record per node."""
    node = await _make_node(db_session)
    db_session.add(PowerCapacity(power_node_id=node.id, rated_capacity_kw=10, version=1, effective_from=datetime.now(UTC)))
    await db_session.flush()
    db_session.add(PowerCapacity(power_node_id=node.id, rated_capacity_kw=20, version=1, effective_from=datetime.now(UTC)))
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="uq_power_capacity_current_per_node"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_invalid_percentage_threshold_rejected(db_session):
    node = await _make_node(db_session)
    cap = PowerCapacity(power_node_id=node.id, warning_threshold_pct=150, version=1, effective_from=datetime.now(UTC))
    db_session.add(cap)
    with pytest.raises(sqlalchemy.exc.IntegrityError, match="warning_threshold_pct_range"):
        await db_session.flush()
