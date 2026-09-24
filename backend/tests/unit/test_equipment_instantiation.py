"""Phase 10B: app/application/equipment_instantiation_service.py exercised directly
against a real db_session, matching this repo's tests/unit convention (e.g.
test_power_capacity.py) of calling application functions without going through HTTP."""

import uuid

import pytest
from sqlalchemy import select

from app.application.catalog_designer_service import publish_revision
from app.application.equipment_instantiation_service import (
    InstantiationRejected,
    connect_port,
    instantiate_equipment,
    list_equipment_ports,
    list_equipment_power_inlets,
)
from app.domain.catalog.designer_models import (
    CatalogModel,
    CatalogModelRevision,
    Manufacturer,
    NetworkPortTemplate,
    PowerSupplyTemplate,
)
from app.domain.power.models import PowerNode


async def _make_equipment_model_revision(
    db_session, *, port_count: int = 2, psu_quantity: int = 2, rack_unit_height: int = 2
) -> tuple[CatalogModelRevision, uuid.UUID]:
    manufacturer = Manufacturer(name=f"Acme-{uuid.uuid4().hex[:8]}")
    db_session.add(manufacturer)
    await db_session.flush()
    model = CatalogModel(manufacturer_id=manufacturer.id, category="equipment", model_name=f"Server-{uuid.uuid4().hex[:8]}")
    db_session.add(model)
    await db_session.flush()

    from app.core.security import hash_password
    from app.domain.auth.models import User

    user = User(email=f"svc-{uuid.uuid4().hex[:8]}@test.local", full_name="Service", password_hash=hash_password("x"))
    db_session.add(user)
    await db_session.flush()

    revision = CatalogModelRevision(
        catalog_model_id=model.id, revision_number=1, lifecycle_status="draft",
        dimension_unit="mm", width_value=440, height_value=rack_unit_height * 44.45, depth_value=600,
        weight_unit="kg", weight_value=10, rack_unit_height=rack_unit_height,
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
                catalog_model_revision_id=revision.id, stable_key="psu1", label="PSU", quantity=psu_quantity,
                connector_type="C14",
            )
        )
    await db_session.flush()
    return revision, user.id


async def test_instantiate_creates_ports_and_power_inlets(db_session):
    revision, user_id = await _make_equipment_model_revision(db_session, port_count=2, psu_quantity=2)
    published = await publish_revision(db_session, revision_id=revision.id, user_id=user_id)
    await db_session.commit()

    equipment = await instantiate_equipment(
        db_session, asset_tag=f"SRV-{uuid.uuid4().hex[:8]}", catalog_model_revision_id=published.id,
        hostname="srv-1", ip_address=None, owner=None, service=None, environment=None, notes=None,
    )
    await db_session.commit()

    assert equipment.catalog_model_revision_id == published.id
    assert equipment.model_revision_id == published.legacy_equipment_model_revision_id

    ports = await list_equipment_ports(db_session, equipment_id=equipment.id)
    assert {p.stable_key for p in ports} == {"eth0", "eth1"}
    assert all(p.media_type == "copper" and p.connector_type == "rj45" for p in ports)

    inlets = await list_equipment_power_inlets(db_session, equipment_id=equipment.id)
    assert {i.stable_key for i in inlets} == {"psu1-1", "psu1-2"}
    for inlet in inlets:
        node = await db_session.get(PowerNode, inlet.power_node_id)
        assert node is not None
        assert node.node_type == "equipment_power_input"
        assert node.owning_asset_id == equipment.id


async def test_instantiate_snapshot_is_decoupled_from_later_draft_edits(db_session):
    """The architectural requirement this phase names explicitly: a future draft edit to
    the same catalog model must never retroactively alter already-deployed equipment."""
    revision, user_id = await _make_equipment_model_revision(db_session, port_count=1, psu_quantity=0)
    published = await publish_revision(db_session, revision_id=revision.id, user_id=user_id)
    await db_session.commit()

    equipment = await instantiate_equipment(
        db_session, asset_tag=f"SRV-{uuid.uuid4().hex[:8]}", catalog_model_revision_id=published.id,
        hostname=None, ip_address=None, owner=None, service=None, environment=None, notes=None,
    )
    await db_session.commit()
    [port] = await list_equipment_ports(db_session, equipment_id=equipment.id)
    original_display_name = port.display_name

    # Clone the published revision into a new draft and edit the cloned port's display
    # name — published revisions/templates are immutable (designer_models.py), so the
    # only way to "edit" is via a fresh draft clone, exactly how a real catalog admin
    # would revise a model after equipment has already been instantiated from it.
    from app.application.catalog_designer_service import clone_revision

    clone = await clone_revision(db_session, source_revision_id=published.id, user_id=user_id)
    await db_session.commit()
    cloned_port = (
        await db_session.execute(
            select(NetworkPortTemplate).where(NetworkPortTemplate.catalog_model_revision_id == clone.id)
        )
    ).scalar_one()
    cloned_port.display_name = "Renamed after deployment"
    await db_session.commit()

    await db_session.refresh(port)
    assert port.display_name == original_display_name


async def test_instantiate_rejects_draft_revision(db_session):
    revision, _user_id = await _make_equipment_model_revision(db_session)
    await db_session.commit()

    with pytest.raises(InstantiationRejected, match="draft"):
        await instantiate_equipment(
            db_session, asset_tag="SRV-REJECT", catalog_model_revision_id=revision.id, hostname=None, ip_address=None,
            owner=None, service=None, environment=None, notes=None,
        )


async def test_instantiate_rejects_retired_revision_by_default(db_session):
    revision, user_id = await _make_equipment_model_revision(db_session)
    published = await publish_revision(db_session, revision_id=revision.id, user_id=user_id)
    await db_session.commit()

    from datetime import UTC, datetime

    published.lifecycle_status = "retired"
    published.retired_at = datetime.now(UTC)
    published.retired_by_user_id = user_id
    published.retirement_reason = "superseded"
    await db_session.commit()

    with pytest.raises(InstantiationRejected, match="retired"):
        await instantiate_equipment(
            db_session, asset_tag="SRV-RETIRED", catalog_model_revision_id=published.id, hostname=None, ip_address=None,
            owner=None, service=None, environment=None, notes=None,
        )


async def test_instantiate_rejects_non_equipment_category(db_session):
    manufacturer = Manufacturer(name=f"Acme-{uuid.uuid4().hex[:8]}")
    db_session.add(manufacturer)
    await db_session.flush()
    model = CatalogModel(manufacturer_id=manufacturer.id, category="rack", model_name=f"Rack-{uuid.uuid4().hex[:8]}")
    db_session.add(model)
    await db_session.flush()

    from app.core.security import hash_password
    from app.domain.auth.models import User

    user = User(email=f"svc-{uuid.uuid4().hex[:8]}@test.local", full_name="Service", password_hash=hash_password("x"))
    db_session.add(user)
    await db_session.flush()

    revision = CatalogModelRevision(
        catalog_model_id=model.id, revision_number=1, lifecycle_status="draft", dimension_unit="in", width_value=19,
        height_value=73.5, depth_value=39.4, rack_unit_height=42, weight_unit="lb", weight_value=220,
        created_by_user_id=user.id,
    )
    db_session.add(revision)
    await db_session.flush()
    published = await publish_revision(db_session, revision_id=revision.id, user_id=user.id)
    await db_session.commit()

    with pytest.raises(InstantiationRejected, match="category"):
        await instantiate_equipment(
            db_session, asset_tag="RACK-REJECT", catalog_model_revision_id=published.id, hostname=None, ip_address=None,
            owner=None, service=None, environment=None, notes=None,
        )


async def test_connect_port_to_another_port_and_reconnect_replaces_row(db_session):
    revision, user_id = await _make_equipment_model_revision(db_session, port_count=1, psu_quantity=0)
    published = await publish_revision(db_session, revision_id=revision.id, user_id=user_id)
    await db_session.commit()

    server = await instantiate_equipment(
        db_session, asset_tag=f"SRV-{uuid.uuid4().hex[:8]}", catalog_model_revision_id=published.id, hostname=None,
        ip_address=None, owner=None, service=None, environment=None, notes=None,
    )
    patch_panel = await instantiate_equipment(
        db_session, asset_tag=f"PP-{uuid.uuid4().hex[:8]}", catalog_model_revision_id=published.id, hostname=None,
        ip_address=None, owner=None, service=None, environment=None, notes=None,
    )
    await db_session.commit()
    [server_port] = await list_equipment_ports(db_session, equipment_id=server.id)
    [panel_port_a] = await list_equipment_ports(db_session, equipment_id=patch_panel.id)

    connection = await connect_port(
        db_session, source_port_id=server_port.id, target_port_id=panel_port_a.id, target_power_node_id=None,
        cable_id="CBL-001", status="active",
    )
    await db_session.commit()
    assert connection.target_port_id == panel_port_a.id
    assert connection.cable_id == "CBL-001"

    # Reconnecting the same source port replaces the row rather than erroring.
    updated = await connect_port(
        db_session, source_port_id=server_port.id, target_port_id=panel_port_a.id, target_power_node_id=None,
        cable_id="CBL-002", status="faulted",
    )
    await db_session.commit()
    assert updated.id == connection.id
    assert updated.cable_id == "CBL-002"
    assert updated.status == "faulted"
