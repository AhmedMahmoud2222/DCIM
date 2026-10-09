"""True-concurrency proof for the Issue #101 If-Match mutations, using the same lock-barrier harness as
`test_atomic_optimistic_concurrency.py`: a separate connection holds the row lock while two requests carrying
the same `If-Match: N` are provably in flight, then releases it. Exactly one request may consume version N."""

import pytest

from tests.api._cable_helpers import CABLES, create_cable
from tests.api._network_helpers import (
    collector_with_integration,
    create_device,
    create_vendor,
    device_doc,
    ingest_records,
    vendor_doc,
)
from tests.api._network_inventory import list_neighbors, make_device, neighbor_record
from tests.integration.test_atomic_optimistic_concurrency import _scalar, race  # noqa: F401


async def _vendor(client, headers):
    vendor = await create_vendor(client, headers)
    body = {k: v for k, v in vendor_doc(vendor["code"]).items() if k != "code"}
    return dict(
        id=vendor["id"], version=vendor["version"], table="vendor_profile",
        calls=[("PUT", f"/api/v1/network-profiles/vendors/{vendor['id']}", {"json": body | {"name": n}}) for n in ("one", "two")],
    )


async def _device(client, headers):
    vendor = await create_vendor(client, headers)
    device = await create_device(client, headers, vendor["id"])
    body = {k: v for k, v in device_doc(device["code"]).items() if k != "code"}
    return dict(
        id=device["id"], version=device["version"], table="device_profile",
        calls=[("PUT", f"/api/v1/network-profiles/devices/{device['id']}", {"json": body | {"priority": p}}) for p in (1, 2)],
    )


async def _cable(client, headers):
    left = await make_device(client, headers, ["p1"], hostname="race-l")
    right = await make_device(client, headers, ["p1"], hostname="race-r")
    cable = await create_cable(client, headers, left["port_by_name"]["p1"], right["port_by_name"]["p1"], label="RACE-1")
    return dict(
        id=cable["id"], version=cable["version"], table="cable",
        calls=[("PATCH", f"{CABLES}/{cable['id']}", {"json": {"notes": n}}) for n in ("one", "two")],
    )


async def _neighbor(client, headers):
    collector, integration = await collector_with_integration(client, headers)
    await ingest_records(client, collector, [neighbor_record(integration["id"])])
    (row,) = await list_neighbors(client, headers)
    return dict(
        id=row["id"], version=row["version"], table="discovered_neighbor",
        calls=[("POST", f"/api/v1/discovery/neighbors/{row['id']}/reject", {"json": {"reason": r}}) for r in ("one", "two")],
    )


@pytest.mark.parametrize("make", [_vendor, _device, _cable, _neighbor])
async def test_two_requests_with_the_same_if_match_cannot_both_win(client, auth_headers, db_engine, race, make):  # noqa: F811
    headers = await auth_headers("Administrator")
    resource = await make(client, headers)
    calls = [(m, url, {**kw, "headers": {**headers, "If-Match": str(resource["version"])}}) for m, url, kw in resource["calls"]]
    responses = await race(f"SELECT 1 FROM {resource['table']} WHERE id = :id FOR UPDATE", {"id": resource["id"]}, calls)
    assert sorted(r.status_code for r in responses) == [200, 409], [r.text for r in responses]
    version = await _scalar(db_engine, f"SELECT version FROM {resource['table']} WHERE id = :id", id=resource["id"])
    assert version == resource["version"] + 1
    audit = await _scalar(db_engine, "SELECT count(*) FROM audit_log WHERE entity_id = :id AND action NOT LIKE '%.create'", id=resource["id"])
    assert audit == 1  # the loser wrote no history


async def test_legacy_connect_and_cable_creation_for_one_port_cannot_both_win(client, auth_headers, db_engine, race):  # noqa: F811
    """Issue #101 review B3: `connect_port` takes the topology lock, so it serialises with cable operations."""
    headers = await auth_headers("Administrator")
    left = await make_device(client, headers, ["p1"], hostname="topo-l")
    right = await make_device(client, headers, ["p1", "p2"], hostname="topo-r")
    a, b, c = left["port_by_name"]["p1"], right["port_by_name"]["p1"], right["port_by_name"]["p2"]
    calls = [
        ("POST", CABLES, {"json": {"label": "TOPO-1", "cable_type": "copper_utp", "endpoint_a_port_id": a, "endpoint_b_port_id": b}, "headers": headers}),
        ("POST", f"/api/v1/equipment/{left['id']}/ports/connect", {"json": {"port_id": a, "target_port_id": c}, "headers": headers}),
    ]
    responses = await race("SELECT pg_advisory_xact_lock(hashtext('dcim.network_topology'))", {}, calls)
    assert sorted(r.status_code for r in responses) == [201, 409], [r.text for r in responses]
    cables = await _scalar(db_engine, "SELECT count(*) FROM cable WHERE label = 'TOPO-1'")
    target = await _scalar(db_engine, "SELECT target_port_id FROM port_connection WHERE source_port_id = :a", a=a)
    assert str(target) == (b if cables == 1 else c)  # the surviving connection agrees with the surviving cable
