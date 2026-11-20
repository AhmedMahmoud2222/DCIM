"""Phase 10B: POST /equipment/instantiate, GET /equipment/{id}/ports, and
POST /equipment/{id}/ports/connect, end to end over HTTP — matching this repo's
tests/api convention (test_catalog_designer_lifecycle.py, test_equipment.py).

These tests require a real PostgreSQL database (the `db_engine`/`race` fixtures use
`pg_advisory_xact_lock` and PostgreSQL-specific DDL). They cannot run against SQLite.
"""

import uuid

from sqlalchemy import text

from tests.api._network_inventory import make_device
from tests.api._phase2_helpers import create_room
from tests.integration.test_atomic_optimistic_concurrency import _scalar  # noqa: F401


async def _admin(auth_headers) -> dict:
    return await auth_headers("Administrator")


async def _make_manufacturer(client, headers) -> str:
    resp = await client.post("/api/v1/catalog/manufacturers", json={"name": f"Acme-{uuid.uuid4().hex[:8]}"}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _make_published_equipment_revision(
    client, headers, *, rack_unit_height: int = 2, port_count: int = 2, psu_quantity: int = 1
) -> dict:
    """A published, category='equipment' revision with `port_count` network ports and one
    power-supply template of `psu_quantity`, ready to instantiate."""
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "equipment", "model_name": f"Server-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    assert model_resp.status_code == 201, model_resp.text
    model_id = model_resp.json()["id"]

    draft_resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    assert draft_resp.status_code == 201, draft_resp.text
    revision = draft_resp.json()

    fields_resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={
            "dimension_unit": "mm", "width_value": 440, "height_value": rack_unit_height * 44.45, "depth_value": 600,
            "weight_unit": "kg", "weight_value": 10, "rack_unit_height": rack_unit_height,
            "supported_placement_types": ["rack_mounted"],
        },
        headers={**headers, "If-Match": str(revision["version"])},
    )
    assert fields_resp.status_code == 200, fields_resp.text
    revision = fields_resp.json()

    for i in range(port_count):
        port_resp = await client.post(
            f"/api/v1/catalog/revisions/{revision['id']}/network-ports",
            json={
                "stable_key": f"eth{i}", "display_name": f"eth{i}", "media_type": "copper",
                "supported_speeds_mbps": [1000], "connector_type": "rj45", "side": "rear",
            },
            headers={**headers, "If-Match": str(revision["version"])},
        )
        assert port_resp.status_code == 201, port_resp.text
        revision["version"] = port_resp.json()["revision_version"]

    if psu_quantity:
        psu_resp = await client.post(
            f"/api/v1/catalog/revisions/{revision['id']}/power-supplies",
            json={"stable_key": "psu1", "label": "PSU", "quantity": psu_quantity, "connector_type": "C14"},
            headers={**headers, "If-Match": str(revision["version"])},
        )
        assert psu_resp.status_code == 201, psu_resp.text
        revision["version"] = psu_resp.json()["revision_version"]

    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text
    return publish_resp.json()


async def test_instantiate_creates_ports_and_power_inlets_for_a_2u_server(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, rack_unit_height=2, port_count=2, psu_quantity=2)

    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"], "hostname": "srv-1"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["catalog_model_revision_id"] == revision["id"]
    assert len(body["ports"]) == 2
    assert {p["stable_key"] for p in body["ports"]} == {"eth0", "eth1"}
    assert len(body["power_inlets"]) == 2

    ports_resp = await client.get(f"/api/v1/equipment/{body['id']}/ports", headers=headers)
    assert ports_resp.status_code == 200, ports_resp.text
    assert len(ports_resp.json()["ports"]) == 2
    assert all(p["connection"] is None for p in ports_resp.json()["ports"])


async def test_instantiate_into_a_rack_slot_in_one_request_and_elevation_reflects_it(client, auth_headers):
    headers = await _admin(auth_headers)
    engineer_headers = await auth_headers("Engineer")
    revision = await _make_published_equipment_revision(client, headers, rack_unit_height=2, port_count=1, psu_quantity=0)
    room_id = await create_room(client, auth_headers)

    from tests.api._phase2_helpers import create_rack_model_revision

    rack_revision_id = await create_rack_model_revision(client, auth_headers, height_u=10)
    rack_resp = await client.post(
        "/api/v1/racks",
        json={"asset_tag": f"RACK-{uuid.uuid4().hex[:8]}", "model_revision_id": rack_revision_id, "name": "R1", "room_id": room_id},
        headers=engineer_headers,
    )
    assert rack_resp.status_code == 201, rack_resp.text
    rack = rack_resp.json()

    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={
            "asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"],
            "placement_type": "rack_mounted", "room_id": room_id, "rack_id": rack["id"], "u_start": 1, "u_end": 3,
            "side": "front",
        },
        headers=engineer_headers,
    )
    assert resp.status_code == 201, resp.text
    equipment_id = resp.json()["id"]
    assert resp.json()["placement"]["rack_id"] == rack["id"]

    elevation_resp = await client.get(f"/api/v1/racks/{rack['id']}/elevation", headers=engineer_headers)
    assert elevation_resp.status_code == 200, elevation_resp.text
    [slot] = elevation_resp.json()["slots"]
    assert slot["equipment_id"] == equipment_id
    assert slot["catalog_model_revision_id"] == revision["id"]


async def test_instantiate_rejects_a_draft_revision(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "equipment", "model_name": f"Draft-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    model_id = model_resp.json()["id"]
    draft_resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    revision = draft_resp.json()

    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": "SRV-DRAFT-REJECT", "catalog_model_revision_id": revision["id"]},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert "draft" in resp.json()["detail"]


async def test_instantiate_rejects_a_rack_category_model(client, auth_headers):
    headers = await _admin(auth_headers)
    manufacturer_id = await _make_manufacturer(client, headers)
    model_resp = await client.post(
        "/api/v1/catalog/models",
        json={"manufacturer_id": manufacturer_id, "category": "rack", "model_name": f"Rack-{uuid.uuid4().hex[:8]}"},
        headers=headers,
    )
    model_id = model_resp.json()["id"]
    draft_resp = await client.post(f"/api/v1/catalog/models/{model_id}/revisions", headers=headers)
    revision = draft_resp.json()
    fields_resp = await client.patch(
        f"/api/v1/catalog/revisions/{revision['id']}",
        json={"dimension_unit": "in", "width_value": 19, "height_value": 73.5, "depth_value": 39.4, "rack_unit_height": 42, "weight_unit": "lb", "weight_value": 220},
        headers={**headers, "If-Match": str(revision["version"])},
    )
    revision = fields_resp.json()
    publish_resp = await client.post(f"/api/v1/catalog/revisions/{revision['id']}/publish", headers=headers)
    assert publish_resp.status_code == 200, publish_resp.text

    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": "SRV-RACK-REJECT", "catalog_model_revision_id": publish_resp.json()["id"]},
        headers=headers,
    )
    assert resp.status_code == 409, resp.text
    assert "category" in resp.json()["detail"]


async def test_operator_cannot_instantiate_or_connect_ports_but_engineer_can(client, auth_headers):
    admin_headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, admin_headers, port_count=1, psu_quantity=0)

    operator_headers = await auth_headers("Operator")
    resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": "SRV-OPERATOR-DENY", "catalog_model_revision_id": revision["id"]},
        headers=operator_headers,
    )
    assert resp.status_code == 403, resp.text

    engineer_headers = await auth_headers("Engineer")
    server_resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"]},
        headers=engineer_headers,
    )
    assert server_resp.status_code == 201, server_resp.text
    panel_resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"PP-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"]},
        headers=engineer_headers,
    )
    assert panel_resp.status_code == 201, panel_resp.text

    server_port_id = server_resp.json()["ports"][0]["id"]
    panel_port_id = panel_resp.json()["ports"][0]["id"]

    deny_connect = await client.post(
        f"/api/v1/equipment/{server_resp.json()['id']}/ports/connect",
        json={"port_id": server_port_id, "target_port_id": panel_port_id},
        headers=operator_headers,
    )
    assert deny_connect.status_code == 403, deny_connect.text

    allow_connect = await client.post(
        f"/api/v1/equipment/{server_resp.json()['id']}/ports/connect",
        json={"port_id": server_port_id, "target_port_id": panel_port_id, "cable_id": "CBL-100"},
        headers=engineer_headers,
    )
    assert allow_connect.status_code == 201, allow_connect.text
    assert allow_connect.json()["target_port_id"] == panel_port_id
    assert allow_connect.json()["cable_id"] == "CBL-100"

    ports_resp = await client.get(f"/api/v1/equipment/{server_resp.json()['id']}/ports", headers=operator_headers)
    assert ports_resp.status_code == 200, ports_resp.text
    [connected_port] = ports_resp.json()["ports"]
    assert connected_port["connection"]["target_port_id"] == panel_port_id
    assert connected_port["connection"]["status"] == "active"


async def test_connect_port_requires_exactly_one_target(client, auth_headers):
    headers = await _admin(auth_headers)
    revision = await _make_published_equipment_revision(client, headers, port_count=1, psu_quantity=0)
    server_resp = await client.post(
        "/api/v1/equipment/instantiate",
        json={"asset_tag": f"SRV-{uuid.uuid4().hex[:8]}", "catalog_model_revision_id": revision["id"]},
        headers=headers,
    )
    port_id = server_resp.json()["ports"][0]["id"]

    resp = await client.post(
        f"/api/v1/equipment/{server_resp.json()['id']}/ports/connect", json={"port_id": port_id}, headers=headers
    )
    assert resp.status_code == 422, resp.text


async def _three_equipment_with_ports(client, headers):
    """Two source equipments and one target equipment, each with one rear port `p1`."""
    src_a = await make_device(client, headers, ["p1"], hostname="fan-src-a")
    src_b = await make_device(client, headers, ["p1"], hostname="fan-src-b")
    tgt_c = await make_device(client, headers, ["p1"], hostname="fan-tgt-c")
    return src_a, src_b, tgt_c


async def _connect(client, headers, equipment, port_id, *, target_port_id=None, target_power_node_id=None):
    return await client.post(
        f"/api/v1/equipment/{equipment['id']}/ports/connect",
        json={"port_id": port_id, "target_port_id": target_port_id, "target_power_node_id": target_power_node_id},
        headers=headers,
    )


async def test_connect_port_rejects_target_claimed_by_another_source(client, auth_headers, db_engine):
    """Fan-in must be rejected: A->C then B->C is a 409 and exactly one row targets C."""
    headers = await auth_headers("Administrator")
    src_a, src_b, tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    b_port = src_b["port_by_name"]["p1"]
    c_port = tgt_c["port_by_name"]["p1"]

    first = await _connect(client, headers, src_a, a_port, target_port_id=c_port)
    assert first.status_code == 201, first.text

    second = await _connect(client, headers, src_b, b_port, target_port_id=c_port)
    assert second.status_code == 409, second.text

    # Exactly one connection row claims C as its target; B holds no outgoing connection.
    target_rows = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=c_port
    )
    assert target_rows == 1
    b_rows = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE source_port_id = :s", s=b_port
    )
    assert b_rows == 0

    # The surviving connection is the first one: source A targets C.
    a_rows = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE source_port_id = :s AND target_port_id = :t",
        s=a_port, t=c_port,
    )
    assert a_rows == 1


async def test_connect_port_allows_reconnect_same_source_to_same_target(client, auth_headers, db_engine):
    """Reconnecting the same source port to the same target replaces nothing and is allowed."""
    headers = await auth_headers("Administrator")
    src_a, _src_b, tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    c_port = tgt_c["port_by_name"]["p1"]

    first = await _connect(client, headers, src_a, a_port, target_port_id=c_port)
    assert first.status_code == 201, first.text
    first_id = first.json()["id"]

    second = await _connect(client, headers, src_a, a_port, target_port_id=c_port)
    assert second.status_code == 201, second.text
    # Same logical row (reconnect replaces in place), not a duplicate.
    assert second.json()["id"] == first_id
    target_rows = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=c_port
    )
    assert target_rows == 1


async def test_connect_port_allows_reconnect_same_source_frees_previous_target(client, auth_headers, db_engine):
    """Reconnecting the same source port to a new target frees the previous target."""
    headers = await auth_headers("Administrator")
    src_a, src_b, tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    c_port = tgt_c["port_by_name"]["p1"]
    b_port = src_b["port_by_name"]["p1"]

    first = await _connect(client, headers, src_a, a_port, target_port_id=c_port)
    assert first.status_code == 201, first.text

    # Repoint A's port at B's port: C must be released and may be claimed by B.
    second = await _connect(client, headers, src_a, a_port, target_port_id=b_port)
    assert second.status_code == 201, second.text

    target_rows_on_c = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=c_port
    )
    assert target_rows_on_c == 0  # C is free now
    b_is_target = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=b_port
    )
    assert b_is_target == 1  # B is now the target of A's single connection


async def test_connect_port_rejects_reversed_link_duplicates_existing(client, auth_headers, db_engine):
    """A reversed link that reuses an occupied endpoint is rejected: A->C then C->A is a 409.

    Each port may participate in at most one logical connection, either as source or
    target. After A->C, both A (as source) and C (as target) are occupied, so C->A reuses
    occupied endpoints and must be rejected.
    """
    headers = await auth_headers("Administrator")
    src_a, _src_b, tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    c_port = tgt_c["port_by_name"]["p1"]

    first = await _connect(client, headers, src_a, a_port, target_port_id=c_port)
    assert first.status_code == 201, first.text

    # Reversed link reuses occupied endpoints (C is already a target; A is already a source).
    second = await _connect(client, headers, tgt_c, c_port, target_port_id=a_port)
    assert second.status_code == 409, second.text

    # Exactly one logical edge remains; no duplicate graph edge created.
    rows = await _scalar(db_engine, "SELECT count(*) FROM port_connection")
    assert rows == 1
    a_rows = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE source_port_id = :s AND target_port_id = :t",
        s=a_port, t=c_port,
    )
    assert a_rows == 1
    c_as_source = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE source_port_id = :s", s=c_port
    )
    assert c_as_source == 0  # C was never promoted to source


async def test_connect_port_rejects_source_occupied_as_target(client, auth_headers, db_engine):
    """A port already occupied as a destination may not be reused as a source: A->B then B->C.

    After A->B, B is occupied as a destination. Using B as a source for B->C switches B's
    role, which the endpoint-exclusivity invariant forbids.
    """
    headers = await auth_headers("Administrator")
    src_a, src_b, tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    b_port = src_b["port_by_name"]["p1"]
    c_port = tgt_c["port_by_name"]["p1"]

    first = await _connect(client, headers, src_a, a_port, target_port_id=b_port)
    assert first.status_code == 201, first.text

    # B is already occupied as a destination (of A->B); using B as a source is rejected.
    second = await _connect(client, headers, src_b, b_port, target_port_id=c_port)
    assert second.status_code == 409, second.text

    rows = await _scalar(db_engine, "SELECT count(*) FROM port_connection")
    assert rows == 1
    b_as_source = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE source_port_id = :s", s=b_port
    )
    assert b_as_source == 0


async def _make_pdu_outlet_power_node(db_engine) -> uuid.UUID:
    """Insert a standalone pdu_outlet PowerNode (+PDUOutlet row) via raw SQL and return its id.

    A pdu_outlet PowerNode needs an owning_asset_id pointing at a ManagedAsset of type
    'pdu' (asset_reference_exclusive constraint), plus a PDUOutlet child row.
    """
    pdu_asset_id = uuid.uuid4()
    node_id = uuid.uuid4()
    async with db_engine.connect() as conn:
        await conn.execute(
            text(
                "INSERT INTO managed_asset (id, asset_type, asset_tag, lifecycle_status, external_ids) "
                "VALUES (:id, 'pdu', :tag, 'planned', '{}')"
            ),
            {"id": pdu_asset_id, "tag": f"PDU-{uuid.uuid4().hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO power_node (id, node_type, owning_asset_id, label, retired_at) "
                "VALUES (:id, 'pdu_outlet', :owning, :label, NULL)"
            ),
            {"id": node_id, "owning": pdu_asset_id, "label": "PDU Outlet 1"},
        )
        await conn.execute(
            text(
                "INSERT INTO pdu_outlet (power_node_id, pdu_asset_id, outlet_number, label, state) "
                "VALUES (:node, :asset, 1, 'PDU Outlet 1', 'unknown')"
            ),
            {"node": node_id, "asset": pdu_asset_id},
        )
        await conn.commit()
    return node_id


async def test_connect_port_rejects_power_node_source_already_occupied_as_target(
    client, auth_headers, db_engine
):
    """Sequence B: A->B then B->PDU is rejected — B cannot switch from target to power-node source.

    After A->B, B is occupied as a destination. Using B as a source for a power-node
    relationship (B->PDU) switches B's role, which the endpoint-exclusivity invariant
    forbids. Symmetric with test_connect_port_rejects_source_occupied_as_target.
    """
    headers = await auth_headers("Administrator")
    src_a, src_b, _tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    b_port = src_b["port_by_name"]["p1"]
    pdu_node = await _make_pdu_outlet_power_node(db_engine)

    first = await _connect(client, headers, src_a, a_port, target_port_id=b_port)
    assert first.status_code == 201, first.text

    # B is already occupied as a destination (of A->B); using B as a power-node source is rejected.
    second = await _connect(client, headers, src_b, b_port, target_power_node_id=str(pdu_node))
    assert second.status_code == 409, second.text

    rows = await _scalar(db_engine, "SELECT count(*) FROM port_connection")
    assert rows == 1
    b_as_source = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE source_port_id = :s", s=b_port
    )
    assert b_as_source == 0
    b_as_target = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=b_port
    )
    assert b_as_target == 1  # A->B survives


async def test_connect_port_rejects_power_node_target_as_port_source(
    client, auth_headers, db_engine
):
    """Sequence A: B->PDU then A->B is rejected — B cannot be a power-node source then a port target.

    After B->PDU, B is occupied as a source. Using B as a target for A->B switches B's
    role, which the endpoint-exclusivity invariant forbids. Symmetric with Sequence B.
    """
    headers = await auth_headers("Administrator")
    src_a, src_b, _tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    b_port = src_b["port_by_name"]["p1"]
    pdu_node = await _make_pdu_outlet_power_node(db_engine)

    first = await _connect(client, headers, src_b, b_port, target_power_node_id=str(pdu_node))
    assert first.status_code == 201, first.text

    # B is already occupied as a source (of B->PDU); using B as a port target is rejected.
    second = await _connect(client, headers, src_a, a_port, target_port_id=b_port)
    assert second.status_code == 409, second.text

    rows = await _scalar(db_engine, "SELECT count(*) FROM port_connection")
    assert rows == 1
    b_as_target = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=b_port
    )
    assert b_as_target == 0
    b_as_source = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE source_port_id = :s", s=b_port
    )
    assert b_as_source == 1  # B->PDU survives


async def test_connect_port_concurrent_power_node_and_port_reverse_lets_exactly_one_win(client, auth_headers, db_engine, race):  # noqa: F811
    """Concurrent B->PDU and A->B: exactly one wins, no duplicate or cross-role occupancy.

    Both take the topology lock; the lock serialises them and exactly one connection row
    survives. B participates in exactly one logical connection (as source OR as target),
    never both.
    """
    headers = await auth_headers("Administrator")
    src_a, src_b, _tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    b_port = src_b["port_by_name"]["p1"]
    pdu_node = await _make_pdu_outlet_power_node(db_engine)

    calls = [
        (
            "POST",
            f"/api/v1/equipment/{src_b['id']}/ports/connect",
            {"json": {"port_id": b_port, "target_power_node_id": str(pdu_node)}, "headers": headers},
        ),
        (
            "POST",
            f"/api/v1/equipment/{src_a['id']}/ports/connect",
            {"json": {"port_id": a_port, "target_port_id": b_port}, "headers": headers},
        ),
    ]
    responses = await race(
        "SELECT pg_advisory_xact_lock(hashtext('dcim.network_topology'))", {}, calls
    )
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]

    # Exactly one persisted logical edge; B occupies exactly one role.
    rows = await _scalar(db_engine, "SELECT count(*) FROM port_connection")
    assert rows == 1
    b_as_source = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE source_port_id = :s", s=b_port
    )
    b_as_target = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=b_port
    )
    assert b_as_source + b_as_target == 1


async def test_connect_port_concurrent_target_fan_in_lets_exactly_one_win(client, auth_headers, db_engine, race):  # noqa: F811
    """Two concurrent requests to connect different sources to the same target: exactly one wins.

    Both take the topology lock, so they are provably in flight at the same time; the
    lock serialises them and exactly one connection row survives.
    """
    headers = await auth_headers("Administrator")
    src_a, src_b, tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    b_port = src_b["port_by_name"]["p1"]
    c_port = tgt_c["port_by_name"]["p1"]

    calls = [
        (
            "POST",
            f"/api/v1/equipment/{src_a['id']}/ports/connect",
            {"json": {"port_id": a_port, "target_port_id": c_port}, "headers": headers},
        ),
        (
            "POST",
            f"/api/v1/equipment/{src_b['id']}/ports/connect",
            {"json": {"port_id": b_port, "target_port_id": c_port}, "headers": headers},
        ),
    ]
    responses = await race(
        "SELECT pg_advisory_xact_lock(hashtext('dcim.network_topology'))", {}, calls
    )
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]

    target_rows = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=c_port
    )
    assert target_rows == 1
    source_rows = await _scalar(
        db_engine,
        "SELECT count(*) FROM port_connection WHERE source_port_id IN (:a, :b)",
        a=a_port, b=b_port,
    )
    assert source_rows == 1


async def test_connect_port_concurrent_reversed_endpoints_lets_exactly_one_win(client, auth_headers, db_engine, race):  # noqa: F811
    """A source-target role reversal during concurrent creation: exactly one winner.

    A->C and C->A run concurrently under the topology lock. Exactly one persists;
    connection-row counts confirm no duplicate logical edge is created. Audit/outbox
    non-emission for the rejected request is asserted separately by
    test_connect_port_rejection_persists_no_mutation.
    """
    headers = await auth_headers("Administrator")
    src_a, _src_b, tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    c_port = tgt_c["port_by_name"]["p1"]

    calls = [
        (
            "POST",
            f"/api/v1/equipment/{src_a['id']}/ports/connect",
            {"json": {"port_id": a_port, "target_port_id": c_port}, "headers": headers},
        ),
        (
            "POST",
            f"/api/v1/equipment/{tgt_c['id']}/ports/connect",
            {"json": {"port_id": c_port, "target_port_id": a_port}, "headers": headers},
        ),
    ]
    responses = await race(
        "SELECT pg_advisory_xact_lock(hashtext('dcim.network_topology'))", {}, calls
    )
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]

    # Exactly one persisted logical edge; no duplicate graph edge.
    rows = await _scalar(db_engine, "SELECT count(*) FROM port_connection")
    assert rows == 1
    source_count = await _scalar(
        db_engine,
        "SELECT count(*) FROM port_connection WHERE source_port_id IN (:a, :c)",
        a=a_port, c=c_port,
    )
    assert source_count == 1  # exactly one of A/C is a source; the other lost cleanly


async def test_connect_port_rejection_persists_no_mutation(client, auth_headers, db_engine):
    """A rejected fan-in connect leaves no new row, audit record or outbox event."""
    headers = await auth_headers("Administrator")
    src_a, src_b, tgt_c = await _three_equipment_with_ports(client, headers)
    a_port = src_a["port_by_name"]["p1"]
    b_port = src_b["port_by_name"]["p1"]
    c_port = tgt_c["port_by_name"]["p1"]

    first = await _connect(client, headers, src_a, a_port, target_port_id=c_port)
    assert first.status_code == 201, first.text

    target_before = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=c_port
    )
    # The route audits against the source port ID (entity_type="equipment_port", entity_id=port.id).
    audit_before = await _scalar(
        db_engine, "SELECT count(*) FROM audit_log WHERE entity_id = :e", e=b_port
    )
    outbox_before = await _scalar(
        db_engine, "SELECT count(*) FROM outbox_event WHERE aggregate_id = :e", e=b_port
    )

    second = await _connect(client, headers, src_b, b_port, target_port_id=c_port)
    assert second.status_code == 409, second.text

    target_after = await _scalar(
        db_engine, "SELECT count(*) FROM port_connection WHERE target_port_id = :t", t=c_port
    )
    assert target_after == target_before  # no new connection row
    audit_after = await _scalar(
        db_engine, "SELECT count(*) FROM audit_log WHERE entity_id = :e", e=b_port
    )
    outbox_after = await _scalar(
        db_engine, "SELECT count(*) FROM outbox_event WHERE aggregate_id = :e", e=b_port
    )
    assert audit_after == audit_before  # no audit record for the rejected connect
    assert outbox_after == outbox_before  # no outbox event
