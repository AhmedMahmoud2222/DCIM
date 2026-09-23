"""Operator physical-link workflow: API, RBAC and transaction regressions."""

import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.domain.audit.models import AuditLog
from app.domain.identity.models import ManagedAsset
from app.domain.network.models import NetworkConnection, NetworkDevice, NetworkInterface
from app.domain.outbox.models import OutboxEvent


async def _topology_fixture(db_session, *, devices: int = 3):
    """Create a small authoritative physical inventory directly for API setup only."""
    result = []
    for index in range(devices):
        asset = ManagedAsset(
            asset_type="network_device",
            asset_tag=f"NET-TEST-{uuid.uuid4().hex[:10]}",
            lifecycle_status="active",
        )
        db_session.add(asset)
        await db_session.flush()
        db_session.add(NetworkDevice(id=asset.id, name=f"Switch {index + 1}", device_type="access_switch"))
        interface = NetworkInterface(device_id=asset.id, name="Ethernet1", role="data")
        db_session.add(interface)
        await db_session.flush()
        result.append((asset, interface))
    await db_session.commit()
    return result


async def _create(client, headers, a_id: str, b_id: str, cable_label: str | None = None):
    body = {"interface_a_id": a_id, "interface_b_id": b_id, "cable_label": cable_label}
    return await client.post("/api/v1/network/connections", json=body, headers=headers)


async def test_manager_creates_authoritative_connection_with_audit_and_outbox(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session)

    response = await _create(client, headers, str(devices[0][1].id), str(devices[1][1].id), "CAB-01")

    assert response.status_code == 201, response.text
    connection = response.json()
    assert connection["source"] == "operator"
    assert connection["is_authoritative"] is True
    assert connection["cable_label"] == "CAB-01"
    assert (await db_session.execute(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(connection["id"])))).scalar_one().action == "network.connection.create"
    assert (await db_session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == uuid.UUID(connection["id"])))).scalar_one().event_type == "NetworkConnectionCreated"


async def test_network_topology_refreshes_after_disconnect(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session)
    created = await _create(client, headers, str(devices[0][1].id), str(devices[1][1].id))
    connection_id = created.json()["id"]

    assert len((await client.get("/api/v1/network/topology", headers=headers)).json()["connections"]) == 1
    deleted = await client.delete(f"/api/v1/network/connections/{connection_id}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get("/api/v1/network/topology", headers=headers)).json()["connections"] == []
    assert (await db_session.execute(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(connection_id)))).scalars().all()[-1].action == "network.connection.disconnect"


async def test_viewer_cannot_create_or_disconnect_connections(client, auth_headers, db_session):
    manager = await auth_headers("DCIM Manager")
    viewer = await auth_headers("Viewer")
    devices = await _topology_fixture(db_session)

    assert (await _create(client, viewer, str(devices[0][1].id), str(devices[1][1].id))).status_code == 403
    created = await _create(client, manager, str(devices[0][1].id), str(devices[1][1].id))
    assert (await client.delete(f"/api/v1/network/connections/{created.json()['id']}", headers=viewer)).status_code == 403


async def test_connection_rejects_missing_endpoint_and_true_self_connection(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session, devices=2)
    first_interface_id = str(devices[0][1].id)

    assert (await _create(client, headers, first_interface_id, str(uuid.uuid4()))).status_code == 404
    assert (await _create(client, headers, first_interface_id, first_interface_id)).status_code == 409


async def test_connection_between_distinct_interfaces_on_the_same_device_is_allowed(client, auth_headers, db_session):
    """Domain decision: only a literal self-connection (identical interface on both
    ends) is prohibited. docs/NETWORK_3D_INCREMENT.md and migration 0015's own DB
    constraints (`no_self_connection` on interface_a_id <> interface_b_id; the
    single-connection trigger keyed on interface identity, not device identity) never
    state or imply a same-device restriction, and realistic physical topologies
    (a loopback test cable, a stacking link between two ports on one chassis) require
    two distinct interfaces on the same device to be linkable."""
    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session, devices=1)
    second_same_device = NetworkInterface(device_id=devices[0][0].id, name="Ethernet2", role="data")
    db_session.add(second_same_device)
    await db_session.commit()
    first_interface_id = str(devices[0][1].id)
    second_interface_id = str(second_same_device.id)

    response = await _create(client, headers, first_interface_id, second_interface_id)

    assert response.status_code == 201, response.text
    connection = response.json()
    assert {connection["interface_a_id"], connection["interface_b_id"]} == {first_interface_id, second_interface_id}


async def test_occupied_interface_and_reversed_duplicate_are_actionable_conflicts(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session)
    first_interface_id = str(devices[0][1].id)
    second_interface_id = str(devices[1][1].id)
    third_interface_id = str(devices[2][1].id)
    created = await _create(client, headers, first_interface_id, second_interface_id)
    assert created.status_code == 201

    reversed_duplicate = await _create(client, headers, second_interface_id, first_interface_id)
    occupied = await _create(client, headers, first_interface_id, third_interface_id)
    assert reversed_duplicate.status_code == 409
    assert occupied.status_code == 409
    assert "already connected" in reversed_duplicate.json()["detail"].lower()
    assert "already connected" in occupied.json()["detail"].lower()


async def test_concurrent_creates_for_the_same_ports_allow_exactly_one_connection(client, auth_headers, db_session):
    """The endpoint locks interfaces, while migration 0015's trigger remains the
    PostgreSQL backstop for a reversed-order race."""
    import httpx
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session, devices=2)
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=6, max_overflow=2)
    sessions = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def override_db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = override_db
    try:
        async def attempt(reverse: bool):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as isolated_client:
                a, b = (devices[1][1], devices[0][1]) if reverse else (devices[0][1], devices[1][1])
                return await _create(isolated_client, headers, str(a.id), str(b.id))

        responses = await asyncio.gather(*[attempt(index % 2 == 1) for index in range(6)])
    finally:
        async def restore_db():
            yield db_session

        app.dependency_overrides[get_db] = restore_db
        await engine.dispose()

    statuses = [response.status_code for response in responses]
    assert statuses.count(201) == 1, statuses
    assert statuses.count(409) == 5, statuses
    assert len((await db_session.execute(select(NetworkConnection))).scalars().all()) == 1


async def test_failed_audit_rolls_back_connection_and_outbox(client, auth_headers, db_session, monkeypatch):
    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session, devices=2)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr("app.api.v1.network.write_audit_log", fail_audit)
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await _create(client, headers, str(devices[0][1].id), str(devices[1][1].id))

    assert (await db_session.execute(select(NetworkConnection))).scalars().all() == []
    assert (await db_session.execute(select(OutboxEvent))).scalars().all() == []


async def _seed_connection(db_session, devices, *, source: str, is_authoritative: bool) -> str:
    connection = NetworkConnection(
        interface_a_id=devices[0][1].id, interface_b_id=devices[1][1].id, source=source, is_authoritative=is_authoritative
    )
    db_session.add(connection)
    await db_session.commit()
    await db_session.refresh(connection)
    return str(connection.id)


# The full source/authority matrix: only source="operator" AND is_authoritative=True may
# be disconnected through this endpoint. `is_authoritative` alone is not a sufficient
# guard — it is a column independent of `source`, and nothing in the domain model
# prevents a collector/import/demo row from carrying is_authoritative=True (e.g. a
# discovery pipeline that promotes a confirmed observation). Every other combination,
# including that one, must be protected.
SOURCE_AUTHORITY_MATRIX = [
    ("operator", True, "disconnect_succeeds"),
    ("operator", False, "protected"),
    ("collector", False, "protected"),
    ("collector", True, "protected"),
    ("import", False, "protected"),
    ("import", True, "protected"),
    ("demo", False, "protected"),
    ("demo", True, "protected"),
]


@pytest.mark.parametrize("source,is_authoritative,expected", SOURCE_AUTHORITY_MATRIX)
async def test_operator_disconnect_enforces_the_full_source_and_authority_predicate(
    client, auth_headers, db_session, source, is_authoritative, expected
):
    """Provenance policy: the operator disconnect workflow only removes connections that
    are both operator-sourced and authoritative. A collector/import/demo observation —
    even one incorrectly or exceptionally flagged is_authoritative=True — is evidence of
    a real physical link the inventory hasn't yet reconciled, not something an operator
    physically undid; deleting it here would silently make that evidence disappear."""
    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session, devices=2)
    connection_id = await _seed_connection(db_session, devices, source=source, is_authoritative=is_authoritative)

    response = await client.delete(f"/api/v1/network/connections/{connection_id}", headers=headers)

    if expected == "disconnect_succeeds":
        assert response.status_code == 204, response.text
        assert (
            await db_session.execute(select(NetworkConnection).where(NetworkConnection.id == uuid.UUID(connection_id)))
        ).scalar_one_or_none() is None
        return

    assert response.status_code == 409, response.text
    body = response.json()
    detail = body["detail"].lower()
    assert source in detail
    assert "not an operator action" in detail or "protected" in detail
    # No stack trace, exception type, or other internal detail leaks into the response.
    assert "traceback" not in detail and "exception" not in detail
    # The connection survives, untouched, and no disconnect audit/outbox event exists.
    assert (
        await db_session.execute(select(NetworkConnection).where(NetworkConnection.id == uuid.UUID(connection_id)))
    ).scalar_one()
    disconnect_audit = (
        await db_session.execute(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(connection_id), AuditLog.action == "network.connection.disconnect"))
    ).scalars().all()
    assert disconnect_audit == []
    disconnect_outbox = (
        await db_session.execute(select(OutboxEvent).where(OutboxEvent.aggregate_id == uuid.UUID(connection_id), OutboxEvent.event_type == "NetworkConnectionDisconnected"))
    ).scalars().all()
    assert disconnect_outbox == []


async def test_operator_disconnect_succeeds_for_operator_authoritative_connection(client, auth_headers, db_session):
    """Contrast case for the provenance policy: an operator-created, authoritative
    connection remains disconnectable — the protection is scoped to non-operator or
    non-authoritative links, not to every connection."""
    headers = await auth_headers("DCIM Manager")
    devices = await _topology_fixture(db_session, devices=2)
    created = await _create(client, headers, str(devices[0][1].id), str(devices[1][1].id))
    connection_id = created.json()["id"]

    response = await client.delete(f"/api/v1/network/connections/{connection_id}", headers=headers)

    assert response.status_code == 204
    assert (await db_session.execute(select(NetworkConnection).where(NetworkConnection.id == uuid.UUID(connection_id)))).scalar_one_or_none() is None
