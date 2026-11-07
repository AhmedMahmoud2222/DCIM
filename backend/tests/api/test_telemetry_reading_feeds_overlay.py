"""Collector readings posted to /collectors/{id}/telemetry feed the cached latest-status row the rack overlay reads."""

import uuid
from datetime import UTC, datetime, timedelta

from tests.api.test_sec01_telemetry_batch_atomicity import _post, _setup
from tests.api.test_telemetry_port_status import _admin, _instantiate, _make_published_equipment_revision


async def _env_binding(client, headers, external_ref: str) -> tuple[dict, dict]:
    revision = await _make_published_equipment_revision(client, headers, port_count=0)
    equipment = await _instantiate(client, headers, revision["id"])
    resp = await client.post(
        "/api/v1/telemetry/bindings",
        json={"equipment_id": equipment["id"], "target_type": "environmental", "protocol": "snmp",
              "external_ref": external_ref, "label": None},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return equipment, resp.json()


def _record(integration, key, at, value, external="ext"):
    return {
        "dedup_key": key, "integration_id": integration["id"], "external_identifier": external,
        "source_identifier": "sensor", "occurred_at": at.isoformat(), "value": value,
    }


async def _latest(client, headers, equipment):
    resp = await client.get("/api/v1/telemetry/port-status/latest", params={"equipment_id": equipment["id"]}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_temperature_reading_updates_overlay_status(client, auth_headers, db_session):
    headers = await _admin(auth_headers)
    collector, integration = await _setup(client, auth_headers, db_session)
    equipment, binding = await _env_binding(client, headers, "ext")
    now = datetime.now(UTC)

    resp = await _post(client, collector, [_record(integration, uuid.uuid4().hex, now, 33.0)])
    assert resp.status_code == 200, resp.text
    assert resp.json()["results"][0]["status"] == "accepted"

    [item] = await _latest(client, headers, equipment)
    assert item["binding_id"] == binding["id"]
    assert item["status_level"] == "CRITICAL"
    assert item["payload"]["temperature_celsius"] == 33.0


async def test_older_reading_does_not_overwrite_newer_status(client, auth_headers, db_session):
    headers = await _admin(auth_headers)
    collector, integration = await _setup(client, auth_headers, db_session)
    equipment, _ = await _env_binding(client, headers, "ext")
    now = datetime.now(UTC)

    await _post(client, collector, [_record(integration, uuid.uuid4().hex, now, 20.0)])
    await _post(client, collector, [_record(integration, uuid.uuid4().hex, now - timedelta(minutes=5), 40.0)])

    [item] = await _latest(client, headers, equipment)
    assert item["status_level"] == "NORMAL"
    assert item["payload"]["temperature_celsius"] == 20.0


async def test_reading_without_matching_binding_stays_history_only(client, auth_headers, db_session):
    headers = await _admin(auth_headers)
    collector, integration = await _setup(client, auth_headers, db_session)
    equipment, _ = await _env_binding(client, headers, "other-ref")

    resp = await _post(client, collector, [_record(integration, uuid.uuid4().hex, datetime.now(UTC), 33.0)])
    assert resp.json()["results"][0]["status"] == "accepted"
    [item] = await _latest(client, headers, equipment)
    assert item["status_level"] is None


async def test_projection_fault_does_not_reject_the_stored_reading_or_its_batch_peers(
    client, auth_headers, db_session, monkeypatch
):
    headers = await _admin(auth_headers)
    collector, integration = await _setup(client, auth_headers, db_session)
    equipment, _ = await _env_binding(client, headers, "ext")
    now = datetime.now(UTC)

    async def faulty_projection(db, *, reading_id):
        raise RuntimeError("projection fault")

    monkeypatch.setattr("app.api.v1.telemetry.project_reading_to_latest_status", faulty_projection)
    first, second = uuid.uuid4().hex, uuid.uuid4().hex
    batch = [
        _record(integration, first, now, 33.0),
        _record(integration, second, now + timedelta(seconds=1), 20.0),
    ]
    resp = await _post(client, collector, batch)
    assert resp.status_code == 200, resp.text
    assert [result["status"] for result in resp.json()["results"]] == ["accepted", "accepted"]

    # The overlay was not fed, but both readings were stored: redelivering one is a duplicate, not a new reading.
    [item] = await _latest(client, headers, equipment)
    assert item["status_level"] is None
    again = await _post(client, collector, [_record(integration, first, now, 33.0)])
    assert again.json()["results"][0]["status"] == "duplicate"
