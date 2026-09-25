"""Phase 10C: pure threshold-evaluation functions and the port/power-inlet binding +
latest-status service, exercised directly against a real db_session (this repo's
tests/unit convention)."""

import uuid
from datetime import UTC, datetime

import pytest

from app.application.catalog_designer_service import publish_revision
from app.application.equipment_instantiation_service import (
    instantiate_equipment,
    list_equipment_ports,
    list_equipment_power_inlets,
)
from app.application.telemetry_service import (
    BindingNotFound,
    InvalidBindingTarget,
    create_port_telemetry_binding,
    evaluate_environmental_status,
    evaluate_link_status,
    evaluate_power_status,
    get_latest_status_for_equipment,
    get_latest_status_for_rack,
    record_latest_status,
)
from app.core.security import hash_password
from app.domain.auth.models import User
from app.domain.catalog.designer_models import (
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.domain.power.models import PowerCapacity

# ------------------------------------------------------------------ Threshold evaluation


@pytest.mark.parametrize(
    ("raw", "error_rate_pct", "expected"),
    [
        ("UP", 0.0, "UP"),
        ("UP", 0.5, "UP"),
        ("UP", 5.0, "DEGRADED"),
        ("DEGRADED", 0.0, "DEGRADED"),
        ("DOWN", 0.0, "DOWN"),
        ("DOWN", 50.0, "DOWN"),
    ],
)
def test_evaluate_link_status(raw, error_rate_pct, expected):
    assert evaluate_link_status(raw, error_rate_pct) == expected


def test_evaluate_link_status_rejects_unknown_state():
    with pytest.raises(ValueError, match="link state"):
        evaluate_link_status("FLAPPING", 0.0)


@pytest.mark.parametrize("rated_kw", [None, 0])
def test_evaluate_power_status_unknown_capacity_is_normal(rated_kw):
    assert evaluate_power_status(500.0, rated_kw) == "NORMAL"


def test_evaluate_power_status_boundaries():
    # 1kW rated: 79% -> NORMAL, 80% -> WARNING, 95% -> CRITICAL
    assert evaluate_power_status(790, 1.0) == "NORMAL"
    assert evaluate_power_status(800, 1.0) == "WARNING"
    assert evaluate_power_status(950, 1.0) == "CRITICAL"


def test_evaluate_environmental_status_boundaries():
    assert evaluate_environmental_status(26.9) == "NORMAL"
    assert evaluate_environmental_status(27.0) == "WARNING"
    assert evaluate_environmental_status(32.0) == "CRITICAL"


# --------------------------------------------------------------------------- Bindings


async def _make_equipment(db_session, *, port_count: int = 1, psu_quantity: int = 1):
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
        dimension_unit="mm", width_value=440, height_value=44.45, depth_value=600,
        weight_unit="kg", weight_value=10, rack_unit_height=1,
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
                catalog_model_revision_id=revision.id, stable_key="psu", label="PSU", quantity=psu_quantity, connector_type="C14",
            )
        )
    await db_session.flush()
    published = await publish_revision(db_session, revision_id=revision.id, user_id=user.id)
    await db_session.commit()
    equipment = await instantiate_equipment(
        db_session, asset_tag=f"SRV-{uuid.uuid4().hex[:8]}", catalog_model_revision_id=published.id, hostname="srv-1",
        ip_address=None, owner=None, service=None, environment=None, notes=None,
    )
    await db_session.commit()
    return equipment


async def test_create_network_port_binding(db_session):
    equipment = await _make_equipment(db_session, port_count=1, psu_quantity=0)
    [port] = await list_equipment_ports(db_session, equipment_id=equipment.id)
    binding = await create_port_telemetry_binding(
        db_session, equipment_id=equipment.id, target_type="network_port", equipment_port_id=port.id,
        equipment_power_inlet_id=None, protocol="snmp", external_ref="1.3.6.1.2.1.2.2.1.8.1", label="eth0 link",
    )
    await db_session.commit()
    assert binding.target_type == "network_port"
    assert binding.equipment_port_id == port.id


async def test_create_binding_rejects_mismatched_equipment(db_session):
    equipment = await _make_equipment(db_session, port_count=1, psu_quantity=0)
    other_equipment = await _make_equipment(db_session, port_count=1, psu_quantity=0)
    [port] = await list_equipment_ports(db_session, equipment_id=equipment.id)
    with pytest.raises(InvalidBindingTarget):
        await create_port_telemetry_binding(
            db_session, equipment_id=other_equipment.id, target_type="network_port", equipment_port_id=port.id,
            equipment_power_inlet_id=None, protocol="snmp", external_ref="oid", label=None,
        )


async def test_create_binding_rejects_type_reference_mismatch(db_session):
    equipment = await _make_equipment(db_session, port_count=1, psu_quantity=0)
    [port] = await list_equipment_ports(db_session, equipment_id=equipment.id)
    with pytest.raises(InvalidBindingTarget):
        await create_port_telemetry_binding(
            db_session, equipment_id=equipment.id, target_type="power_inlet", equipment_port_id=port.id,
            equipment_power_inlet_id=None, protocol="snmp", external_ref="oid", label=None,
        )


async def test_create_binding_missing_port_raises_not_found(db_session):
    equipment = await _make_equipment(db_session, port_count=0, psu_quantity=0)
    with pytest.raises(BindingNotFound):
        await create_port_telemetry_binding(
            db_session, equipment_id=equipment.id, target_type="network_port", equipment_port_id=uuid.uuid4(),
            equipment_power_inlet_id=None, protocol="snmp", external_ref="oid", label=None,
        )


async def test_record_and_read_latest_network_port_status(db_session):
    equipment = await _make_equipment(db_session, port_count=1, psu_quantity=0)
    [port] = await list_equipment_ports(db_session, equipment_id=equipment.id)
    binding = await create_port_telemetry_binding(
        db_session, equipment_id=equipment.id, target_type="network_port", equipment_port_id=port.id,
        equipment_power_inlet_id=None, protocol="snmp", external_ref="oid", label=None,
    )
    await db_session.commit()

    now = datetime.now(UTC)
    status = await record_latest_status(
        db_session, binding_id=binding.id,
        payload={"link_state": "UP", "bandwidth_util_pct": 12.5, "error_rate_pct": 0.0}, sampled_at=now,
    )
    await db_session.commit()
    assert status.status_level == "UP"

    # Ingesting again upserts in place -- still exactly one cached row per binding.
    later = datetime.now(UTC)
    await record_latest_status(
        db_session, binding_id=binding.id,
        payload={"link_state": "DOWN", "bandwidth_util_pct": 0.0, "error_rate_pct": 0.0}, sampled_at=later,
    )
    await db_session.commit()

    items = await get_latest_status_for_equipment(db_session, equipment_id=equipment.id)
    assert len(items) == 1
    assert items[0].status.status_level == "DOWN"


async def test_record_power_inlet_status_uses_capacity_for_threshold(db_session):
    equipment = await _make_equipment(db_session, port_count=0, psu_quantity=1)
    [inlet] = await list_equipment_power_inlets(db_session, equipment_id=equipment.id)
    db_session.add(
        PowerCapacity(power_node_id=inlet.power_node_id, rated_capacity_kw=1.0, version=1, effective_from=datetime.now(UTC))
    )
    await db_session.flush()
    binding = await create_port_telemetry_binding(
        db_session, equipment_id=equipment.id, target_type="power_inlet", equipment_port_id=None,
        equipment_power_inlet_id=inlet.id, protocol="pdu_outlet", external_ref="outlet-3", label=None,
    )
    await db_session.commit()

    status = await record_latest_status(
        db_session, binding_id=binding.id,
        payload={"current_amps": 8.0, "active_power_watts": 950.0, "voltage": 120.0}, sampled_at=datetime.now(UTC),
    )
    await db_session.commit()
    assert status.status_level == "CRITICAL"


async def test_get_latest_status_for_rack_scopes_to_rack_mounted_equipment(db_session):
    from app.application.placement_service import move_equipment
    from app.domain.catalog.models import RackModel, RackModelRevision
    from app.domain.identity.models import ManagedAsset
    from app.domain.location.models import Building, City, Country, Floor, Organization, Room, Site
    from app.domain.physical.models import Rack

    org = Organization(name=f"Org-{uuid.uuid4().hex[:6]}")
    db_session.add(org)
    await db_session.flush()
    country = Country(organization_id=org.id, name="US", iso_code="US")
    db_session.add(country)
    await db_session.flush()
    city = City(country_id=country.id, name="City")
    db_session.add(city)
    await db_session.flush()
    site = Site(city_id=city.id, code=f"S-{uuid.uuid4().hex[:6]}", name="Site", timezone="UTC")
    db_session.add(site)
    await db_session.flush()
    building = Building(site_id=site.id, code="B1", name="Bldg")
    db_session.add(building)
    await db_session.flush()
    floor = Floor(building_id=building.id, level_number=1, name="Floor")
    db_session.add(floor)
    await db_session.flush()
    room = Room(floor_id=floor.id, code="R1", name="Room", room_type="data_hall")
    db_session.add(room)
    await db_session.flush()

    rack_asset = ManagedAsset(asset_type="rack", asset_tag=f"RACK-{uuid.uuid4().hex[:6]}", lifecycle_status="active")
    db_session.add(rack_asset)
    await db_session.flush()
    rack_model = RackModel(manufacturer=f"RackCo-{uuid.uuid4().hex[:6]}", model_name="R42")
    db_session.add(rack_model)
    await db_session.flush()
    rack_revision = RackModelRevision(rack_model_id=rack_model.id, height_u=42, width_mm=600, depth_mm=1000)
    db_session.add(rack_revision)
    await db_session.flush()
    rack = Rack(id=rack_asset.id, model_revision_id=rack_revision.id, name="Rack-1")
    db_session.add(rack)
    await db_session.flush()
    await db_session.commit()

    equipment = await _make_equipment(db_session, port_count=1, psu_quantity=0)
    [port] = await list_equipment_ports(db_session, equipment_id=equipment.id)
    binding = await create_port_telemetry_binding(
        db_session, equipment_id=equipment.id, target_type="network_port", equipment_port_id=port.id,
        equipment_power_inlet_id=None, protocol="snmp", external_ref="oid", label=None,
    )
    await db_session.commit()

    await move_equipment(
        db_session, equipment_id=equipment.id, placement_type="rack_mounted", room_id=room.id, rack_id=rack.id,
        u_start=1, u_end=2, side="front",
    )
    await db_session.commit()

    items = await get_latest_status_for_rack(db_session, rack_id=rack.id)
    assert len(items) == 1
    assert items[0].binding.id == binding.id


# ------------------------------------------------- Latest-status ordering (out-of-order
# telemetry). `record_latest_status`'s own docstring states the contract these lock in:
# strictly-newer applies, strictly-older is rejected, equal is a first-writer-wins no-op.


async def _network_binding(db_session):
    equipment = await _make_equipment(db_session, port_count=1, psu_quantity=0)
    [port] = await list_equipment_ports(db_session, equipment_id=equipment.id)
    binding = await create_port_telemetry_binding(
        db_session, equipment_id=equipment.id, target_type="network_port", equipment_port_id=port.id,
        equipment_power_inlet_id=None, protocol="snmp", external_ref="oid", label=None,
    )
    await db_session.commit()
    return equipment, binding


def _link_payload(state: str, error_rate_pct: float = 0.0) -> dict:
    return {"link_state": state, "bandwidth_util_pct": 10.0, "error_rate_pct": error_rate_pct}


async def test_stale_sample_does_not_overwrite_a_newer_cached_status(db_session):
    """The regression this guard exists for: a poller's DOWN reading from T0 arriving
    *after* the recovery reading from T0+60s (a delayed retry, a re-queued batch) must
    not resurrect the old state. Without the ordering guard the cached row would read
    DOWN and the rack-elevation overlay would show a healthy link as failed."""
    equipment, binding = await _network_binding(db_session)
    t0 = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 25, 12, 1, 0, tzinfo=UTC)

    await record_latest_status(db_session, binding_id=binding.id, payload=_link_payload("UP"), sampled_at=newer)
    await db_session.commit()

    retained = await record_latest_status(
        db_session, binding_id=binding.id, payload=_link_payload("DOWN"), sampled_at=t0
    )
    await db_session.commit()

    assert retained.status_level == "UP"
    assert retained.sampled_at == newer
    assert retained.payload["link_state"] == "UP"
    [item] = await get_latest_status_for_equipment(db_session, equipment_id=equipment.id)
    assert item.status.status_level == "UP"
    assert item.status.sampled_at == newer


async def test_newer_sample_replaces_the_cached_status(db_session):
    equipment, binding = await _network_binding(db_session)
    older = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)
    newer = datetime(2026, 9, 25, 12, 0, 30, tzinfo=UTC)

    await record_latest_status(db_session, binding_id=binding.id, payload=_link_payload("UP"), sampled_at=older)
    await db_session.commit()
    applied = await record_latest_status(
        db_session, binding_id=binding.id, payload=_link_payload("DOWN"), sampled_at=newer
    )
    await db_session.commit()

    assert applied.status_level == "DOWN"
    assert applied.sampled_at == newer
    [item] = await get_latest_status_for_equipment(db_session, equipment_id=equipment.id)
    assert item.status.status_level == "DOWN"


async def test_duplicate_sample_at_an_equal_timestamp_is_a_no_op(db_session):
    """Equal `sampled_at` is a re-delivery of one sample, not new information. First
    writer wins, so the stored row is byte-identical whichever copy arrives second —
    the property that makes concurrent re-delivery deterministic."""
    equipment, binding = await _network_binding(db_session)
    sampled_at = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

    first = await record_latest_status(
        db_session, binding_id=binding.id, payload=_link_payload("UP"), sampled_at=sampled_at
    )
    await db_session.commit()
    first_received_at = first.received_at

    second = await record_latest_status(
        db_session, binding_id=binding.id, payload=_link_payload("DOWN"), sampled_at=sampled_at
    )
    await db_session.commit()

    assert second.status_level == "UP"
    assert second.payload["link_state"] == "UP"
    assert second.received_at == first_received_at, "a declined write must not touch received_at"


async def test_first_sample_for_a_binding_is_always_applied(db_session):
    """No stored row means nothing to be stale against — the insert path is untouched by
    the guard, which only ever runs on conflict."""
    equipment, binding = await _network_binding(db_session)
    old = datetime(2020, 1, 1, tzinfo=UTC)
    status = await record_latest_status(
        db_session, binding_id=binding.id, payload=_link_payload("DEGRADED", error_rate_pct=5.0), sampled_at=old
    )
    await db_session.commit()
    assert status.status_level == "DEGRADED"
    assert status.sampled_at == old


async def test_ordering_guard_is_scoped_per_binding(db_session):
    """A newer sample on one binding must not block an older-but-first sample on a
    different binding — the guard keys on the conflicting row, never globally."""
    equipment = await _make_equipment(db_session, port_count=2, psu_quantity=0)
    ports = await list_equipment_ports(db_session, equipment_id=equipment.id)
    bindings = []
    for index, port in enumerate(ports):
        bindings.append(
            await create_port_telemetry_binding(
                db_session, equipment_id=equipment.id, target_type="network_port", equipment_port_id=port.id,
                equipment_power_inlet_id=None, protocol="snmp", external_ref=f"oid-{index}", label=None,
            )
        )
    await db_session.commit()

    await record_latest_status(
        db_session, binding_id=bindings[0].id, payload=_link_payload("UP"),
        sampled_at=datetime(2026, 9, 25, 12, 5, 0, tzinfo=UTC),
    )
    second = await record_latest_status(
        db_session, binding_id=bindings[1].id, payload=_link_payload("DOWN"),
        sampled_at=datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC),
    )
    await db_session.commit()
    assert second.status_level == "DOWN"
    assert {i.status.status_level for i in await get_latest_status_for_equipment(db_session, equipment_id=equipment.id)} == {
        "UP",
        "DOWN",
    }
