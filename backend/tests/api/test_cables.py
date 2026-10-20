"""Physical cables: identity, endpoints, lifecycle, relation to logical port connections,
explicit creation from confirmed discovery, and trace."""

import uuid

from sqlalchemy import text

from tests.api._cable_helpers import CABLES, cable_body, create_cable, if_match
from tests.api._network_helpers import ingest_records
from tests.api._network_inventory import list_neighbors, make_device, neighbor_record, observing_device, set_mac


async def two_devices(client, headers):
    left = await make_device(client, headers, ["Eth1/1", "Eth1/2", "Eth1/3"], hostname="edge-sw-1", ip_address="192.0.2.50")
    right = await make_device(client, headers, ["Eth1/24", "Eth1/23"], hostname="core-sw-1", ip_address="10.0.0.1")
    return left, right


async def connections(db_session) -> list[tuple]:
    rows = await db_session.execute(text("SELECT source_port_id, target_port_id, status, cable_id FROM port_connection"))
    return [tuple(r) for r in rows]


async def test_create_cable_records_identity_endpoints_and_realizes_a_logical_connection(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    a, b = left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"]
    cable = await create_cable(
        client, headers, a, b, label="  PP1-A01  ", cable_type="copper_utp", connector_a="rj45", connector_b="rj45",
        length_m="2.50", route_metadata={"tray": "T-3", "via": ["patch-panel-1"]}, notes="blue cable",
    )
    assert cable["label"] == "PP1-A01" and cable["status"] == "planned" and cable["source"] == "manual"
    assert cable["version"] == 1 and cable["installed_at"] is None and cable["length_m"] == "2.50"
    ends = {e["end"]: e["port"] for e in cable["endpoints"]}
    assert ends["A"]["port_id"] == a and ends["A"]["equipment_hostname"] == "edge-sw-1" and ends["A"]["port_name"] == "Eth1/1"
    assert ends["B"]["port_id"] == b and ends["B"]["equipment_hostname"] == "core-sw-1"
    # the cable realized one logical connection, labelled with the cable
    assert await connections(db_session) == [(uuid.UUID(a), uuid.UUID(b), "planned", "PP1-A01")]
    assert cable["port_connection_id"] is not None
    assert (await client.get(f"{CABLES}/{cable['id']}", headers=headers)).json()["label"] == "PP1-A01"


async def test_lifecycle_install_then_remove_keeps_history_and_frees_ports(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    a, b = left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"]
    cable = await create_cable(client, headers, a, b, label="LC-1")
    installed = await client.post(f"{CABLES}/{cable['id']}/install", json={"installed_at": "2026-03-01T10:00:00Z"}, headers=headers | if_match(1))
    assert installed.status_code == 200 and installed.json()["status"] == "installed"
    assert installed.json()["installed_at"].startswith("2026-03-01T10:00:00") and installed.json()["version"] == 2
    assert (await connections(db_session))[0][2] == "active"
    again = await client.post(f"{CABLES}/{cable['id']}/install", json={}, headers=headers | if_match(2))
    assert again.status_code == 409

    early = await client.post(f"{CABLES}/{cable['id']}/remove", json={"removed_at": "2026-01-01T00:00:00Z"}, headers=headers | if_match(2))
    assert early.status_code == 422
    removed = await client.post(
        f"{CABLES}/{cable['id']}/remove", json={"removed_at": "2026-04-01T00:00:00Z", "reason": "decommissioned rack"}, headers=headers | if_match(2))
    assert removed.status_code == 200 and removed.json()["status"] == "removed"
    assert removed.json()["removed_at"].startswith("2026-04-01") and removed.json()["installed_at"].startswith("2026-03-01")
    assert await connections(db_session) == []  # the connection this cable created is released with it
    kept = await client.get(f"{CABLES}/{cable['id']}", headers=headers)
    assert kept.status_code == 200 and {e["port"]["port_id"] for e in kept.json()["endpoints"]} == {a, b}  # history survives

    assert (await client.patch(f"{CABLES}/{cable['id']}", json={"notes": "x"}, headers=headers | if_match(3))).status_code == 409
    assert (await client.post(f"{CABLES}/{cable['id']}/install", json={}, headers=headers | if_match(3))).status_code == 409
    assert (await client.post(f"{CABLES}/{cable['id']}/remove", json={}, headers=headers | if_match(3))).status_code == 409
    # ports and the label are free again
    reused = await create_cable(client, headers, a, b, label="lc-1")
    assert reused["status"] == "planned"
    audit = (await db_session.execute(text("SELECT action FROM audit_log WHERE entity_type = 'cable' ORDER BY timestamp"))).scalars().all()
    assert audit == ["cable.create", "cable.install", "cable.remove", "cable.create"]


async def test_one_live_cable_per_port_and_unique_live_label(client, auth_headers):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    ports = left["port_by_name"] | {f"r-{k}": v for k, v in right["port_by_name"].items()}
    await create_cable(client, headers, ports["Eth1/1"], ports["r-Eth1/24"], label="U-1")
    for a, b in ((ports["Eth1/1"], ports["Eth1/2"]), (ports["Eth1/2"], ports["r-Eth1/24"]), (ports["r-Eth1/24"], ports["Eth1/1"])):
        busy = await client.post(CABLES, json=cable_body(a, b), headers=headers)
        assert busy.status_code == 409 and "live cable" in busy.json()["detail"], busy.text
    clash = await client.post(CABLES, json=cable_body(ports["Eth1/2"], ports["r-Eth1/23"], label="u-1"), headers=headers)
    assert clash.status_code == 409 and "labelled" in clash.json()["detail"]
    ok = await client.post(CABLES, json=cable_body(ports["Eth1/2"], ports["r-Eth1/23"], label="U-2"), headers=headers)
    assert ok.status_code == 201


async def test_validation_media_ports_and_bounds(client, auth_headers):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    a, b = left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"]
    assert (await client.post(CABLES, json=cable_body(a, a), headers=headers)).status_code == 422
    assert (await client.post(CABLES, json=cable_body(a, str(uuid.uuid4())), headers=headers)).status_code == 404
    for bad in (
        {"cable_type": "fiber_sm"},            # copper ports
        {"cable_type": "laser"},
        {"label": "   "},
        {"label": ""},
        {"length_m": "0"},
        {"length_m": "-3"},
        {"length_m": "1.234"},
        {"route_metadata": {"a": {"b": {"c": {"d": {"e": 1}}}}}},
        {"route_metadata": {"x": "y" * 5000}},
        {"status": "removed"},
        {"status": "bogus"},
        {"connector_a": "x" * 33},
        {"notes": "n" * 2001},
    ):
        response = await client.post(CABLES, json=cable_body(a, b, **bad), headers=headers)
        assert response.status_code == 422, (bad, response.status_code)
    assert (await client.get(CABLES, headers=headers)).json()["total"] == 0


async def test_an_existing_logical_connection_is_linked_never_overwritten(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    a, b, c = left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"], right["port_by_name"]["Eth1/23"]
    connect = await client.post(
        f"/api/v1/equipment/{left['id']}/ports/connect", json={"port_id": a, "target_port_id": b, "cable_id": "OLD-LABEL", "status": "active"}, headers=headers)
    assert connect.status_code == 201
    other = await client.post(CABLES, json=cable_body(a, c), headers=headers)
    assert other.status_code == 409 and "logical connection" in other.json()["detail"]
    assert len(await connections(db_session)) == 1  # untouched
    linked = await create_cable(client, headers, b, a, label="LINKED")  # reversed ends still the same pair
    assert linked["port_connection_id"] is not None
    rows = await connections(db_session)
    assert rows == [(uuid.UUID(a), uuid.UUID(b), "active", "OLD-LABEL")]
    await client.post(f"{CABLES}/{linked['id']}/install", json={}, headers=headers | if_match(1))
    removed = await client.post(f"{CABLES}/{linked['id']}/remove", json={}, headers=headers | if_match(2))
    assert removed.status_code == 200
    assert len(await connections(db_session)) == 1  # a connection the cable did not create survives its removal


async def test_update_renames_keep_the_logical_label_in_step_and_bump_versions(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    cable = await create_cable(client, headers, left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"], label="OLD")
    other = await create_cable(client, headers, left["port_by_name"]["Eth1/2"], right["port_by_name"]["Eth1/23"], label="TAKEN")
    assert (await client.patch(f"{CABLES}/{cable['id']}", json={"notes": "n"}, headers=headers)).status_code == 428
    assert (await client.patch(f"{CABLES}/{cable['id']}", json={"notes": "n"}, headers=headers | if_match(9))).status_code == 409
    assert (await client.patch(f"{CABLES}/{cable['id']}", json={"label": other["label"]}, headers=headers | if_match(1))).status_code == 409
    assert (await client.patch(f"{CABLES}/{cable['id']}", json={"label": None}, headers=headers | if_match(1))).status_code == 422
    assert (await client.patch(f"{CABLES}/{cable['id']}", json={"cable_type": "fiber_mm"}, headers=headers | if_match(1))).status_code == 422
    ok = await client.patch(
        f"{CABLES}/{cable['id']}", json={"label": "NEW", "length_m": "7.25", "notes": "moved", "route_metadata": {"tray": "T-9"}},
        headers=headers | if_match(1))
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["label"] == "NEW" and body["length_m"] == "7.25" and body["version"] == 2 and body["route_metadata"] == {"tray": "T-9"}
    assert (await db_session.execute(text("SELECT cable_id FROM port_connection WHERE cable_id = 'NEW'"))).scalar_one() == "NEW"
    # endpoints are not editable through the API
    ignored = await client.patch(f"{CABLES}/{cable['id']}", json={"endpoint_a_port_id": left["port_by_name"]["Eth1/3"]}, headers=headers | if_match(2))
    assert ignored.status_code == 200
    assert {e["port"]["port_id"] for e in ignored.json()["endpoints"]} == {left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"]}


async def test_delete_is_only_for_never_installed_cables(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    a, b = left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"]
    planned = await create_cable(client, headers, a, b, label="DEL-1")
    assert (await client.delete(f"{CABLES}/{planned['id']}", headers=headers)).status_code == 428
    assert (await client.delete(f"{CABLES}/{planned['id']}", headers=headers | if_match(5))).status_code == 409
    assert (await client.delete(f"{CABLES}/{planned['id']}", headers=headers | if_match(1))).status_code == 204
    assert (await client.get(f"{CABLES}/{planned['id']}", headers=headers)).status_code == 404
    assert await connections(db_session) == []
    installed = await create_cable(client, headers, a, b, label="DEL-2", status="installed")
    assert installed["installed_at"] is not None
    assert (await client.delete(f"{CABLES}/{installed['id']}", headers=headers | if_match(1))).status_code == 409
    assert (await client.get(f"{CABLES}/{installed['id']}", headers=headers)).status_code == 200


async def test_list_filters_and_pagination(client, auth_headers):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    first = await create_cable(client, headers, left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"], label="ALPHA_1")
    await create_cable(client, headers, left["port_by_name"]["Eth1/2"], right["port_by_name"]["Eth1/23"], label="BETA%2", status="installed")
    await client.post(f"{CABLES}/{first['id']}/remove", json={}, headers=headers | if_match(1))
    listing = (await client.get(CABLES, params={"limit": 1}, headers=headers)).json()
    assert listing["total"] == 2 and len(listing["items"]) == 1 and listing["limit"] == 1
    assert [c["label"] for c in (await client.get(CABLES, params={"status": "installed"}, headers=headers)).json()["items"]] == ["BETA%2"]
    assert [c["label"] for c in (await client.get(CABLES, params={"label": "alpha"}, headers=headers)).json()["items"]] == ["ALPHA_1"]
    assert (await client.get(CABLES, params={"label": "%"}, headers=headers)).json()["total"] == 1  # a wildcard is a literal
    assert (await client.get(CABLES, params={"label": "_"}, headers=headers)).json()["total"] == 1
    assert (await client.get(CABLES, params={"port_id": left["port_by_name"]["Eth1/2"]}, headers=headers)).json()["total"] == 1
    assert (await client.get(CABLES, params={"equipment_id": right["id"]}, headers=headers)).json()["total"] == 2
    assert (await client.get(CABLES, params={"cable_type": "dac"}, headers=headers)).json()["total"] == 0
    assert (await client.get(CABLES, params={"status": "bogus"}, headers=headers)).status_code == 422


async def test_discovery_alone_never_creates_a_cable(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    await set_mac(db_session, right["id"], "00:50:56:3a:1b:2c")
    collector, integration = await observing_device(client, headers, left)
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1")])
    (neighbor,) = await list_neighbors(client, headers)
    assert neighbor["reconciliation_state"] == "proposed"
    confirmed = await client.post(f"/api/v1/discovery/neighbors/{neighbor['id']}/confirm", json={}, headers=headers | if_match(neighbor["version"]))
    assert confirmed.status_code == 200
    assert (await db_session.execute(text("SELECT count(*) FROM cable"))).scalar_one() == 0  # confirmed != cabled
    assert len(await connections(db_session)) == 0
    assert (await client.get(CABLES, headers=headers)).json()["total"] == 0


async def test_cable_from_a_confirmed_neighbor_is_explicit_and_traceable(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    await set_mac(db_session, right["id"], "00:50:56:3a:1b:2c")
    collector, integration = await observing_device(client, headers, left)
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1", occurred_at="2099-01-01T00:00:00Z")])
    (neighbor,) = await list_neighbors(client, headers)
    body = {"label": "FROM-LLDP-1", "cable_type": "copper_utp"}
    before_confirm = await client.post(f"{CABLES}/from-neighbor/{neighbor['id']}", json=body, headers=headers)
    assert before_confirm.status_code == 409 and "confirmed" in before_confirm.json()["detail"]
    await client.post(f"/api/v1/discovery/neighbors/{neighbor['id']}/confirm", json={}, headers=headers | if_match(neighbor["version"]))
    created = await client.post(f"{CABLES}/from-neighbor/{neighbor['id']}", json=body, headers=headers)
    assert created.status_code == 201, created.text
    cable = created.json()
    assert cable["source"] == "discovery_confirmed" and cable["source_neighbor_id"] == neighbor["id"] and cable["status"] == "installed"
    ends = {e["end"]: e["port"]["port_id"] for e in cable["endpoints"]}
    assert ends == {"A": left["port_by_name"]["Eth1/1"], "B": right["port_by_name"]["Eth1/24"]}
    again = await client.post(f"{CABLES}/from-neighbor/{neighbor['id']}", json=body | {"label": "DUP"}, headers=headers)
    assert again.status_code == 409

    trace = (await client.get(f"/api/v1/topology/ports/{left['port_by_name']['Eth1/1']}/trace", headers=headers)).json()
    assert trace["terminated"] == "end_of_path"
    hop = trace["path"][0]
    assert hop["link"]["kind"] == "cable" and hop["link"]["cable"]["label"] == "FROM-LLDP-1" and hop["link"]["cable"]["source"] == "discovery_confirmed"
    assert hop["hop"]["remote"]["equipment_hostname"] == "core-sw-1" and hop["hop"]["remote"]["port_name"] == "Eth1/24"
    assert trace["evidence"]["authoritative"] is False and trace["evidence"]["agreement"] == "agrees"
    assert trace["evidence"]["neighbors"][0]["neighbor_id"] == neighbor["id"]
    reverse = (await client.get(f"/api/v1/topology/ports/{right['port_by_name']['Eth1/24']}/trace", headers=headers)).json()
    assert reverse["path"][0]["hop"]["remote"]["equipment_hostname"] == "edge-sw-1" and reverse["path"][0]["link"]["near_end"] == "B"


async def test_stale_or_unconfirmed_neighbors_cannot_become_cables(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    await set_mac(db_session, right["id"], "00:50:56:3a:1b:2c")
    collector, integration = await observing_device(client, headers, left)
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1", occurred_at="2026-01-01T00:00:00Z")])
    (neighbor,) = await list_neighbors(client, headers)
    await client.post(f"/api/v1/discovery/neighbors/{neighbor['id']}/confirm", json={}, headers=headers | if_match(neighbor["version"]))
    stale = await client.post(f"{CABLES}/from-neighbor/{neighbor['id']}", json={"label": "S-1", "cable_type": "copper_utp"}, headers=headers)
    assert stale.status_code == 409 and "stale" in stale.json()["detail"]
    missing = await client.post(f"{CABLES}/from-neighbor/{uuid.uuid4()}", json={"label": "S-2", "cable_type": "copper_utp"}, headers=headers)
    assert missing.status_code == 404
    assert (await client.get(CABLES, headers=headers)).json()["total"] == 0


async def test_a_cable_contradicting_a_confirmed_adjacency_is_refused(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    await set_mac(db_session, right["id"], "00:50:56:3a:1b:2c")
    collector, integration = await observing_device(client, headers, left)
    await ingest_records(client, collector, [neighbor_record(
        integration["id"], chassis="00:50:56:3a:1b:2c", port="Eth1/24", management_address="10.0.0.1")])
    (neighbor,) = await list_neighbors(client, headers)
    await client.post(f"/api/v1/discovery/neighbors/{neighbor['id']}/confirm", json={}, headers=headers | if_match(neighbor["version"]))
    clash = await client.post(CABLES, json=cable_body(left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/23"]), headers=headers)
    assert clash.status_code == 409 and "discovery adjacency" in clash.json()["detail"]
    same_pair = await client.post(CABLES, json=cable_body(left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"]), headers=headers)
    assert same_pair.status_code == 201  # recording the physical cable the evidence describes is fine
    # and the neighbor can no longer be re-pointed elsewhere while the cable stands
    revoke = await client.post(f"/api/v1/discovery/neighbors/{neighbor['id']}/revoke", json={}, headers=headers | if_match(2))
    assert revoke.status_code == 200
    assert revoke.json()["reconciliation_state"] == "proposed"  # the cable describes the same pair: corroboration, not conflict
    assert {link["kind"] for link in revoke.json()["match_evidence"]["corroborated_by"]} == {"port_connection", "cable"}


async def test_trace_variants(client, auth_headers):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    trace = lambda port: client.get(f"/api/v1/topology/ports/{port}/trace", headers=headers)  # noqa: E731
    free = (await trace(left["port_by_name"]["Eth1/3"])).json()
    assert free["terminated"] == "no_link" and free["path"] == [] and free["start"]["equipment_hostname"] == "edge-sw-1"
    assert free["evidence"]["agreement"] == "no_evidence"
    # a logical connection without any physical cable is reported as such
    a, b = left["port_by_name"]["Eth1/2"], right["port_by_name"]["Eth1/23"]
    await client.post(f"/api/v1/equipment/{left['id']}/ports/connect", json={"port_id": a, "target_port_id": b, "cable_id": "LEGACY"}, headers=headers)
    logical = (await trace(a)).json()
    assert logical["path"][0]["link"]["kind"] == "port_connection" and "without a recorded physical cable" in logical["path"][0]["link"]["note"]
    assert logical["path"][0]["link"]["cable_label"] == "LEGACY" and logical["path"][0]["hop"]["remote"]["port_name"] == "Eth1/23"
    # a retired cable shows up as history
    cable = await create_cable(client, headers, left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"], label="OLD-HIST")
    await client.post(f"{CABLES}/{cable['id']}/remove", json={}, headers=headers | if_match(1))
    history = (await trace(left["port_by_name"]["Eth1/1"])).json()
    assert history["terminated"] == "no_link" and [c["label"] for c in history["previous_cables"]] == ["OLD-HIST"]
    assert (await trace(str(uuid.uuid4()))).status_code == 404


async def test_rbac_matrix(client, auth_headers):
    admin = await auth_headers("Administrator")
    left, right = await two_devices(client, admin)
    cable = await create_cable(client, admin, left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"], label="RB-1")
    for role, may_manage in (("Administrator", True), ("DCIM Manager", True), ("Engineer", True), ("Operator", False), ("Viewer", False)):
        headers = await auth_headers(role)
        assert (await client.get(CABLES, headers=headers)).status_code == 200, role
        assert (await client.get(f"{CABLES}/{cable['id']}", headers=headers)).status_code == 200, role
        assert (await client.get(f"/api/v1/topology/ports/{left['port_by_name']['Eth1/1']}/trace", headers=headers)).status_code == 200
        create = await client.post(CABLES, json=cable_body(left["port_by_name"]["Eth1/2"], right["port_by_name"]["Eth1/23"]), headers=headers)
        assert create.status_code in ((201,) if may_manage else (403,)), (role, create.text)
        if may_manage:
            assert (await client.delete(f"{CABLES}/{create.json()['id']}", headers=headers | if_match(1))).status_code == 204
        else:
            for method, path, kwargs in (
                ("patch", f"{CABLES}/{cable['id']}", {"json": {"notes": "x"}}),
                ("post", f"{CABLES}/{cable['id']}/install", {"json": {}}),
                ("post", f"{CABLES}/{cable['id']}/remove", {"json": {}}),
                ("delete", f"{CABLES}/{cable['id']}", {}),
                ("post", f"{CABLES}/from-neighbor/{uuid.uuid4()}", {"json": {"label": "x", "cable_type": "other"}}),
            ):
                response = await getattr(client, method)(path, headers=headers | if_match(1), **kwargs)
                assert response.status_code == 403, (role, method, path)
    assert (await client.get(CABLES)).status_code == 401
    assert (await client.get(f"/api/v1/topology/ports/{left['port_by_name']['Eth1/1']}/trace")).status_code == 401


async def test_creating_from_a_neighbor_also_needs_discovery_reconcile(client, auth_headers, db_session):
    """A role with cable:manage but no discovery:reconcile cannot turn evidence into a cable."""
    from tests.api.test_user_groups import _group, _group_user

    admin = await auth_headers("Administrator")
    group = await _group(client, admin, allow=["cable:read", "cable:manage"])
    _user, headers = await _group_user(client, admin, [group])
    response = await client.post(f"{CABLES}/from-neighbor/{uuid.uuid4()}", json={"label": "x", "cable_type": "other"}, headers=headers)
    assert response.status_code in (403, 404)


async def test_linking_existing_planned_connection_preserves_its_label_and_state(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    a, b = left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"]
    response = await client.post(
        f"/api/v1/equipment/{left['id']}/ports/connect",
        json={"port_id": a, "target_port_id": b, "cable_id": "LOGICAL", "status": "planned"}, headers=headers)
    assert response.status_code == 201
    cable = await create_cable(client, headers, a, b, label="PHYSICAL")
    updated = await client.patch(
        f"{CABLES}/{cable['id']}", json={"label": "RENAMED"}, headers=headers | if_match(1))
    assert updated.status_code == 200
    installed = await client.post(f"{CABLES}/{cable['id']}/install", json={}, headers=headers | if_match(2))
    assert installed.status_code == 200
    removed = await client.post(f"{CABLES}/{cable['id']}/remove", json={}, headers=headers | if_match(3))
    assert removed.status_code == 200
    assert await connections(db_session) == [(uuid.UUID(a), uuid.UUID(b), "planned", "LOGICAL")]


async def _connect(client, headers, equipment_id, source, target, **extra):
    return await client.post(
        f"/api/v1/equipment/{equipment_id}/ports/connect", json={"port_id": source, "target_port_id": target, **extra}, headers=headers)


async def test_legacy_connect_cannot_retarget_a_connection_a_live_cable_owns(client, auth_headers, db_session):
    """Issue #101 review B3: the legacy connect API used to rewrite a cable's connection in place."""
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    a, b, c = left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"], right["port_by_name"]["Eth1/23"]
    for status in ("planned", "installed"):
        cable = await create_cable(client, headers, a, b, label=f"OWN-{status}", status=status)
        before = await connections(db_session)
        refused = await _connect(client, headers, left["id"], a, c, cable_id="HIJACK", status="active")
        assert refused.status_code == 409, refused.text
        assert "OWN-" in refused.json()["detail"]
        same = await _connect(client, headers, left["id"], a, b, cable_id="RELABEL")  # even the same target: the cable owns the row
        assert same.status_code == 409
        assert await connections(db_session) == before  # A<->B intact, label and status untouched
        assert (await client.get(f"{CABLES}/{cable['id']}", headers=headers)).json()["port_connection_id"] == cable["port_connection_id"]
        removed = await client.post(f"{CABLES}/{cable['id']}/remove", json={}, headers=headers | if_match(cable["version"]))
        assert removed.status_code == 200, removed.text
        assert await connections(db_session) == []  # a removed cable frees the port for the legacy API again
        free = await _connect(client, headers, left["id"], a, c)
        assert free.status_code == 201
        await db_session.execute(text("DELETE FROM port_connection"))
        await db_session.commit()


async def test_removing_a_cable_never_deletes_a_connection_that_diverged_from_it(client, auth_headers, db_session):
    headers = await auth_headers("Administrator")
    left, right = await two_devices(client, headers)
    a, b, c = left["port_by_name"]["Eth1/1"], right["port_by_name"]["Eth1/24"], right["port_by_name"]["Eth1/23"]
    planned = await create_cable(client, headers, a, b, label="DIV-1")
    # State the legacy API could produce before the guard (or any out-of-band change): the row now says A -> C.
    await db_session.execute(
        text("UPDATE port_connection SET target_port_id = :c, cable_id = 'OPERATOR-OWN' WHERE id = :id"),
        {"c": uuid.UUID(c), "id": uuid.UUID(planned["port_connection_id"])})
    await db_session.commit()
    deleted = await client.delete(f"{CABLES}/{planned['id']}", headers=headers | if_match(planned["version"]))
    assert deleted.status_code == 204, deleted.text
    assert await connections(db_session) == [(uuid.UUID(a), uuid.UUID(c), "planned", "OPERATOR-OWN")]
    audit = (await db_session.execute(text("SELECT after FROM audit_log WHERE action = 'cable.delete'"))).scalars().all()
    assert audit and audit[-1]["port_connection"] == "kept_diverged"

    installed = await create_cable(client, headers, left["port_by_name"]["Eth1/2"], right["port_by_name"]["Eth1/24"], label="DIV-2", status="installed")
    await db_session.execute(
        text("UPDATE port_connection SET target_port_id = :c WHERE id = :id"),
        {"c": uuid.UUID(c), "id": uuid.UUID(installed["port_connection_id"])})
    await db_session.commit()
    removed = await client.post(f"{CABLES}/{installed['id']}/remove", json={}, headers=headers | if_match(installed["version"]))
    assert removed.status_code == 200
    assert len(await connections(db_session)) == 2  # both the operator's rows survive
    removed_audit = (await db_session.execute(text("SELECT after FROM audit_log WHERE action = 'cable.remove'"))).scalars().all()
    assert removed_audit[-1]["port_connection"] == "kept_diverged"
