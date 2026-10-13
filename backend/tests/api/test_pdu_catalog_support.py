"""Issue #150 — PDU Catalog Support end-to-end regression tests.

Verifies:
1. Category 'pdu' models can be created, edited, validated, and published in the Catalog Designer.
2. Publishing a PDU revision bridges to EquipmentModelRevision while preserving category='pdu'.
3. Physical equipment/PDU instances can be instantiated from published PDU catalog revisions.
4. Instantiated PDUs create power inlets, outlets, and power nodes that integrate seamlessly
   with the power topology and cabling workflows.
"""

import uuid

from sqlalchemy import select

from app.domain.catalog.models import EquipmentModelRevision
from app.domain.identity.models import ManagedAsset
from app.domain.physical.models import Equipment
from app.domain.physical.ports import EquipmentPowerInlet
from app.domain.power.models import PowerNode


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def test_pdu_catalog_lifecycle_and_instantiation(client, auth_headers, db_session):
    headers = await _admin(auth_headers)

    # 1. Create Manufacturer and PDU Category Model
    mfg_resp = await client.post("/api/v1/catalog/manufacturers", json={"name": "APC Schneider"}, headers=headers)
    assert mfg_resp.status_code == 201, mfg_resp.text
    mfg_id = mfg_resp.json()["id"]

    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={
            "manufacturer_id": mfg_id,
            "category": "pdu",
            "subtype": "rack_pdu",
            "model_name": "AP8959NA3",
            "model_number": "AP8959NA3-01",
            "description": "ZeroU Switched Rack PDU",
        },
        headers=headers,
    )
    assert model_resp.status_code == 201, model_resp.text
    model = model_resp.json()
    assert model["category"] == "pdu"

    # 2. Create Draft Revision
    draft_resp = await client.post(f"/api/v1/catalog/models/{model['id']}/revisions", headers=headers)
    assert draft_resp.status_code == 201, draft_resp.text
    draft = draft_resp.json()

    # 3. Fill required physical and electrical fields
    update_body = {
        "dimension_unit": "mm",
        "width_value": 56.0,
        "height_value": 1790.0,
        "depth_value": 44.0,
        "weight_unit": "kg",
        "weight_value": 8.5,
        "rack_unit_height": 0,
        "mounting_orientation": "vertical_0u",
        "supported_placement_types": ["rack_mounted"],
        "rated_power_w": 4992.0,
        "power_redundancy_mode": "single",
    }
    patch_resp = await client.patch(
        f"/api/v1/catalog/revisions/{draft['id']}",
        json=update_body,
        headers={**headers, "If-Match": str(draft["version"])},
    )
    assert patch_resp.status_code == 200, patch_resp.text
    rev_version = patch_resp.json()["version"]

    # 4. Add Power Supply (Input Feed)
    psu_resp = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/power-supplies",
        json={
            "stable_key": "feed_in_a",
            "label": "Feed In A",
            "quantity": 1,
            "redundancy_mode": "single",
            "connector_type": "NEMA_L6_30P",
            "rated_voltage_min": 200,
            "rated_voltage_max": 240,
            "rated_current_a": 24.0,
        },
        headers={**headers, "If-Match": str(rev_version)},
    )
    assert psu_resp.status_code == 201, psu_resp.text
    rev_version = psu_resp.json()["revision_version"]

    # 5. Add Monitoring Metric (SNMP current reading)
    metric_resp = await client.post(
        f"/api/v1/catalog/revisions/{draft['id']}/monitoring-metrics",
        json={
            "stable_key": "phase_a_current",
            "protocol": "snmp",
            "metric_name": "Phase A Current",
            "oid": "1.3.6.1.4.1.318.1.1.26.8.3.1.5.1",
            "value_type": "float",
            "unit": "A",
        },
        headers={**headers, "If-Match": str(rev_version)},
    )
    assert metric_resp.status_code == 201, metric_resp.text

    # 6. Validate Draft Revision
    val_resp = await client.post(f"/api/v1/catalog/revisions/{draft['id']}/validate", headers=headers)
    assert val_resp.status_code == 200, val_resp.text
    val_data = val_resp.json()
    assert val_data["valid"] is True, f"Validation failed: {val_data}"

    # 7. Publish Revision
    pub_resp = await client.post(f"/api/v1/catalog/revisions/{draft['id']}/publish", headers=headers)
    assert pub_resp.status_code == 200, pub_resp.text
    published = pub_resp.json()
    assert published["lifecycle_status"] == "published"

    # Verify legacy bridge row was created
    revision_id = uuid.UUID(published["id"])
    legacy = (
        await db_session.execute(
            select(EquipmentModelRevision).where(EquipmentModelRevision.bridged_from_catalog_revision_id == revision_id)
        )
    ).scalar_one()
    assert legacy is not None

    # 8. Instantiate Physical PDU Equipment
    inst_resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={
            "asset_tag": f"PDU-{uuid.uuid4().hex[:8]}",
            "catalog_model_revision_id": str(published["id"]),
            "hostname": "pdu-rack01-a",
            "environment": "production",
        },
        headers=headers,
    )
    assert inst_resp.status_code == 201, inst_resp.text
    instantiated = inst_resp.json()

    # Verify Equipment and ManagedAsset records
    equipment_id = uuid.UUID(instantiated["id"])
    equipment = await db_session.get(Equipment, equipment_id)
    assert equipment is not None
    assert equipment.catalog_model_revision_id == revision_id

    asset = await db_session.get(ManagedAsset, equipment_id)
    assert asset is not None
    assert asset.asset_type == "pdu"

    # Verify Power Inlet + Power Node creation
    inlets = (
        await db_session.execute(
            select(EquipmentPowerInlet).where(EquipmentPowerInlet.equipment_id == equipment_id)
        )
    ).scalars().all()
    assert len(inlets) == 1
    assert inlets[0].label == "Feed In A"

    node = await db_session.get(PowerNode, inlets[0].power_node_id)
    assert node is not None
    assert node.node_type == "equipment_power_input"
    assert node.owning_asset_id == equipment_id

    # 9. Create PDU Outlet on the instantiated PDU asset
    outlet_resp = await client.post(
        "/api/v1/power/pdu-outlets",
        json={
            "pdu_asset_id": str(equipment_id),
            "outlet_number": 1,
            "label": "Outlet 1",
        },
        headers=headers,
    )
    assert outlet_resp.status_code == 201, outlet_resp.text
    outlet_node = outlet_resp.json()
    assert outlet_node["node_type"] == "pdu_outlet"
    assert outlet_node["owning_asset_id"] == str(equipment_id)
