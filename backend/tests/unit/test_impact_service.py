"""Phase 10C: app/application/impact_service.py exercised directly against a real
db_session (this repo's tests/unit convention — see test_power_graph.py and
test_equipment_instantiation.py). Covers the graph-traversal engine's two headline
scenarios from the phase spec — a single-corded equipment losing all power vs. a
dual-corded equipment degrading to its surviving feed — plus the network-side analog and
the bounds each traversal shares with power_graph.py's own."""

import uuid
from datetime import UTC, datetime

import pytest

from app.application.catalog_designer_service import publish_revision
from app.application.equipment_instantiation_service import (
    connect_port,
    instantiate_equipment,
    list_equipment_ports,
    list_equipment_power_inlets,
)
from app.application.impact_service import ImpactTargetNotFound, simulate_network_port_failure, simulate_power_node_failure
from app.core.security import hash_password
from app.domain.auth.models import User
from app.domain.catalog.designer_models import (
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.domain.power.models import PowerConnection, PowerNode


async def _make_revision(db_session, *, port_count: int, psu_quantity: int) -> tuple[CatalogModelRevision, uuid.UUID]:
    manufacturer = Manufacturer(name=f"Acme-{uuid.uuid4().hex[:8]}")
    db_session.add(manufacturer)
    await db_session.flush()
    model = CatalogModel(manufacturer_id=manufacturer.id, category="equipment", model_name=f"Server-{uuid.uuid4().hex[:8]}")
    db_session.add(model)
    await db_session.flush()
    user = User(email=f"svc-{uuid.uuid4().hex[:8]}@test.local", full_name="Service", password_hash=hash_password("x"))
    db_session.add(user)
    await db_session.flush()
    revision = CatalogModelRevision(
        catalog_model_id=model.id, revision_number=1, lifecycle_status="draft",
        dimension_unit="mm", width_value=440, height_value=88.9, depth_value=600,
        weight_unit="kg", weight_value=10, rack_unit_height=2,
        supported_placement_types=["rack_mounted"], created_by_user_id=user.id,
    )
    db_session.add(revision)
    await db_session.flush()
    for i in range(port_count):
        db_session.add(
            NetworkPortTemplate(
                catalog_model_revision_id=revision.id, stable_key=f"eth{i}", display_name=f"eth{i}",
                media_type="copper", supported_speeds_mbps=[1000], connector_type="rj45", side="rear", sort_order=i,
            )
        )
    if psu_quantity:
        db_session.add(
            PowerSupplyTemplate(
                catalog_model_revision_id=revision.id, stable_key="psu", label="PSU", quantity=psu_quantity,
                connector_type="C14",
            )
        )
    await db_session.flush()
    return revision, user.id


async def _instantiate(db_session, *, port_count: int, psu_quantity: int):
    revision, user_id = await _make_revision(db_session, port_count=port_count, psu_quantity=psu_quantity)
    pub = await publish_revision(db_session, revision_id=revision.id, user_id=user_id)
    await db_session.commit()
    equipment = await instantiate_equipment(
        db_session, asset_tag=f"SRV-{uuid.uuid4().hex[:8]}", catalog_model_revision_id=pub.id, hostname=f"host-{uuid.uuid4().hex[:6]}",
        ip_address=None, owner=None, service="prod-api", environment=None, notes=None,
    )
    await db_session.commit()
    return equipment


async def _connect_power(db_session, source_id, target_id, feed_label="single"):
    conn = PowerConnection(
        source_node_id=source_id, target_node_id=target_id, connection_type="feed", feed_label=feed_label,
        status="active", version=1, effective_from=datetime.now(UTC),
    )
    db_session.add(conn)
    await db_session.flush()
    return conn


async def _make_upstream_node(db_session, label="outlet") -> uuid.UUID:
    # utility_intake is the one node_type the DB CHECK constraint allows with neither
    # managed_asset_id nor owning_asset_id set (test_power_graph.py's own convention) —
    # the right choice for a graph-shape-only fixture that stands in for a PDU outlet or
    # UPS without needing a real backing ManagedAsset.
    node = PowerNode(node_type="utility_intake", label=label)
    db_session.add(node)
    await db_session.flush()
    return node.id


# ------------------------------------------------------------------------------- Power


async def test_power_node_failure_single_corded_equipment_is_full_outage(db_session):
    equipment = await _instantiate(db_session, port_count=0, psu_quantity=1)
    [inlet] = await list_equipment_power_inlets(db_session, equipment_id=equipment.id)
    outlet = await _make_upstream_node(db_session)
    await _connect_power(db_session, outlet, inlet.power_node_id)
    await db_session.commit()

    result = await simulate_power_node_failure(db_session, outlet)

    assert len(result.directly_impacted) == 1
    item = result.directly_impacted[0]
    assert item.equipment_id == equipment.id
    assert item.impact_type == "power_loss"
    assert item.hop == 1
    assert item.service == "prod-api"
    assert result.indirectly_impacted == []
    assert result.lost_redundancy_paths == []
    assert result.affected_services == ["prod-api"]


async def test_power_node_failure_dual_corded_equipment_degrades_to_surviving_feed(db_session):
    equipment = await _instantiate(db_session, port_count=0, psu_quantity=2)
    inlet_a, inlet_b = await list_equipment_power_inlets(db_session, equipment_id=equipment.id)
    outlet_a = await _make_upstream_node(db_session, label="Outlet A")
    outlet_b = await _make_upstream_node(db_session, label="Outlet B")
    await _connect_power(db_session, outlet_a, inlet_a.power_node_id, feed_label="A")
    await _connect_power(db_session, outlet_b, inlet_b.power_node_id, feed_label="B")
    await db_session.commit()

    result = await simulate_power_node_failure(db_session, outlet_a)

    assert len(result.directly_impacted) == 1
    item = result.directly_impacted[0]
    assert item.impact_type == "degraded_redundancy"
    assert inlet_a.label in item.message
    assert inlet_b.label in item.message
    assert len(result.lost_redundancy_paths) == 1
    assert inlet_b.label in result.lost_redundancy_paths[0]


async def test_power_node_failure_upstream_node_is_indirect_hop(db_session):
    """Failing a node two hops upstream of the equipment (e.g. a UPS feeding the PDU
    outlet, rather than the outlet itself) classifies the equipment as indirectly, not
    directly, impacted."""
    equipment = await _instantiate(db_session, port_count=0, psu_quantity=1)
    [inlet] = await list_equipment_power_inlets(db_session, equipment_id=equipment.id)
    ups_node = PowerNode(node_type="utility_intake", label="UPS-1")
    db_session.add(ups_node)
    await db_session.flush()
    outlet = await _make_upstream_node(db_session)
    await _connect_power(db_session, ups_node.id, outlet)
    await _connect_power(db_session, outlet, inlet.power_node_id)
    await db_session.commit()

    result = await simulate_power_node_failure(db_session, ups_node.id)

    assert result.directly_impacted == []
    assert len(result.indirectly_impacted) == 1
    assert result.indirectly_impacted[0].equipment_id == equipment.id
    assert result.indirectly_impacted[0].hop == 2


async def test_power_node_failure_targeted_at_the_inlet_itself_is_direct(db_session):
    """An operator simulating failure of the equipment's own inlet/PSU node directly
    (rather than an upstream PDU outlet or breaker) — e.g. from the rack elevation's
    power marker — must classify that equipment as directly, not indirectly, impacted:
    an equipment_power_input is always a leaf with no outgoing PowerConnection of its
    own, so it never appears as anyone's "direct child" the way an outlet does."""
    equipment = await _instantiate(db_session, port_count=0, psu_quantity=1)
    [inlet] = await list_equipment_power_inlets(db_session, equipment_id=equipment.id)
    outlet = await _make_upstream_node(db_session)
    await _connect_power(db_session, outlet, inlet.power_node_id)
    await db_session.commit()

    result = await simulate_power_node_failure(db_session, inlet.power_node_id)

    assert len(result.directly_impacted) == 1
    assert result.directly_impacted[0].equipment_id == equipment.id
    assert result.directly_impacted[0].hop == 1
    assert result.indirectly_impacted == []


async def test_power_node_failure_unmodeled_node_has_no_impact(db_session):
    isolated = PowerNode(node_type="utility_intake", label="isolated")
    db_session.add(isolated)
    await db_session.flush()
    await db_session.commit()

    result = await simulate_power_node_failure(db_session, isolated.id)
    assert result.directly_impacted == []
    assert result.indirectly_impacted == []
    assert result.affected_services == []


async def test_power_node_failure_missing_node_raises(db_session):
    with pytest.raises(ImpactTargetNotFound):
        await simulate_power_node_failure(db_session, uuid.uuid4())


# ----------------------------------------------------------------------------- Network


async def test_network_port_failure_isolates_single_homed_equipment(db_session):
    switch = await _instantiate(db_session, port_count=1, psu_quantity=0)
    server = await _instantiate(db_session, port_count=1, psu_quantity=0)
    [switch_port] = await list_equipment_ports(db_session, equipment_id=switch.id)
    [server_port] = await list_equipment_ports(db_session, equipment_id=server.id)
    await connect_port(
        db_session, source_port_id=switch_port.id, target_port_id=server_port.id, target_power_node_id=None,
        cable_id=None, status="active",
    )
    await db_session.commit()

    result = await simulate_network_port_failure(db_session, switch_port.id)

    assert len(result.directly_impacted) == 1
    item = result.directly_impacted[0]
    assert item.equipment_id == server.id
    assert item.impact_type == "network_isolated"
    assert item.hop == 1
    assert result.lost_redundancy_paths == []


async def test_network_port_failure_degrades_dual_homed_equipment(db_session):
    switch_a = await _instantiate(db_session, port_count=1, psu_quantity=0)
    switch_b = await _instantiate(db_session, port_count=1, psu_quantity=0)
    server = await _instantiate(db_session, port_count=2, psu_quantity=0)
    [switch_a_port] = await list_equipment_ports(db_session, equipment_id=switch_a.id)
    [switch_b_port] = await list_equipment_ports(db_session, equipment_id=switch_b.id)
    server_port_a, server_port_b = await list_equipment_ports(db_session, equipment_id=server.id)

    await connect_port(db_session, source_port_id=switch_a_port.id, target_port_id=server_port_a.id, target_power_node_id=None, cable_id=None, status="active")
    await connect_port(db_session, source_port_id=switch_b_port.id, target_port_id=server_port_b.id, target_power_node_id=None, cable_id=None, status="active")
    await db_session.commit()

    result = await simulate_network_port_failure(db_session, switch_a_port.id)

    assert len(result.directly_impacted) == 1
    item = result.directly_impacted[0]
    assert item.equipment_id == server.id
    assert item.impact_type == "network_degraded"
    assert len(result.lost_redundancy_paths) == 1


async def test_network_port_failure_transitive_hop_through_patch_panel(db_session):
    """A patch panel's single instantiated port acts as a passthrough here: it is the
    *target* of the switch's connection and the *source* of the connection onward to the
    server — nothing in `PortConnection`'s schema forbids a port from being both (only
    `source_port_id` itself is unique per row), which is exactly how a passive panel
    passes one physical link through to another."""
    switch = await _instantiate(db_session, port_count=1, psu_quantity=0)
    panel = await _instantiate(db_session, port_count=1, psu_quantity=0)
    server = await _instantiate(db_session, port_count=1, psu_quantity=0)
    [switch_port] = await list_equipment_ports(db_session, equipment_id=switch.id)
    [panel_port] = await list_equipment_ports(db_session, equipment_id=panel.id)
    [server_port] = await list_equipment_ports(db_session, equipment_id=server.id)

    await connect_port(db_session, source_port_id=switch_port.id, target_port_id=panel_port.id, target_power_node_id=None, cable_id=None, status="active")
    await connect_port(db_session, source_port_id=panel_port.id, target_port_id=server_port.id, target_power_node_id=None, cable_id=None, status="active")
    await db_session.commit()

    result = await simulate_network_port_failure(db_session, switch_port.id)

    by_equipment = {item.equipment_id: item for item in [*result.directly_impacted, *result.indirectly_impacted]}
    assert by_equipment[panel.id].hop == 1
    assert by_equipment[panel.id] in result.directly_impacted
    assert by_equipment[server.id].hop == 2
    assert by_equipment[server.id] in result.indirectly_impacted


async def test_network_port_failure_missing_port_raises(db_session):
    with pytest.raises(ImpactTargetNotFound):
        await simulate_network_port_failure(db_session, uuid.uuid4())
