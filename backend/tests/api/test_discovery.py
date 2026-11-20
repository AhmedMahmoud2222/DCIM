"""API-level tests for Phase 8 discovery / reconciliation boundary (master prompt §7):
proves discovery creates only DiscoveredDevice + ReconciliationDiff rows, never touches
authoritative inventory directly, and that linking to an existing ManagedAsset is a
human decision made through the one permitted function."""

import uuid

from sqlalchemy import func, select, text

from app.domain.identity.models import ManagedAsset
from tests.api._network_helpers import collector_with_integration, ingest_records
from tests.api._phase8_helpers import create_integration, register_collector, sign_request


async def _ingest_one(client, collector, integration, external_identifier="dev-1"):
    body = {
        "batch_id": f"b-{uuid.uuid4().hex[:8]}",
        "records": [
            {
                "dedup_key": f"d-{uuid.uuid4().hex[:8]}",
                "integration_id": integration["id"],
                "external_identifier": external_identifier,
                "occurred_at": "2026-01-01T00:00:00Z",
                "raw_attributes": {"vendor": "acme"},
            }
        ],
    }
    import json

    raw_body = json.dumps(body).encode()
    headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)
    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200, resp.text
    parsed = resp.json()
    assert parsed["results"][0]["status"] == "accepted", parsed  # catches a silently-rejected record, not just a 200
    return parsed


async def _discover_one_device(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    external_id = f"dev-{uuid.uuid4().hex[:8]}"
    await _ingest_one(client, collector, integration, external_identifier=external_id)
    return headers, external_id


async def test_ingest_creates_discovered_device_and_pending_diff(client, auth_headers):
    headers, external_id = await _discover_one_device(client, auth_headers)

    devices_resp = await client.get("/api/v1/discovery/devices", headers=headers)
    assert devices_resp.status_code == 200
    matching = [d for d in devices_resp.json() if d["external_identifier"] == external_id]
    assert len(matching) == 1
    assert matching[0]["status"] == "new"
    assert matching[0]["matched_managed_asset_id"] is None

    diffs_resp = await client.get("/api/v1/discovery/reconciliation", headers=headers, params={"status_filter": "pending"})
    assert diffs_resp.status_code == 200
    matching_diffs = [d for d in diffs_resp.json() if d["discovered_device_id"] == matching[0]["id"]]
    assert len(matching_diffs) == 1
    assert matching_diffs[0]["diff_type"] == "new_device"
    assert matching_diffs[0]["status"] == "pending"


async def test_discovery_never_creates_managed_asset_rows(client, auth_headers, db_session):
    before = (await db_session.execute(select(func.count()).select_from(ManagedAsset))).scalar_one()
    await _discover_one_device(client, auth_headers)
    after = (await db_session.execute(select(func.count()).select_from(ManagedAsset))).scalar_one()
    assert after == before  # discovery alone must never mutate authoritative inventory


async def test_accept_links_to_existing_managed_asset(client, auth_headers):
    headers, external_id = await _discover_one_device(client, auth_headers)

    asset_resp = await client.post(
        "/api/v1/managed-assets", json={"asset_type": "sensor", "asset_tag": f"tag-{uuid.uuid4().hex[:8]}"}, headers=headers,
    )
    assert asset_resp.status_code == 201
    asset = asset_resp.json()

    diffs = (await client.get("/api/v1/discovery/reconciliation", headers=headers, params={"status_filter": "pending"})).json()
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    device = next(d for d in devices if d["external_identifier"] == external_id)
    diff = next(d for d in diffs if d["discovered_device_id"] == device["id"])

    resp = await client.post(
        f"/api/v1/discovery/reconciliation/{diff['id']}/accept", json={"matched_managed_asset_id": asset["id"]}, headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    device_after = next(
        d for d in (await client.get("/api/v1/discovery/devices", headers=headers)).json() if d["id"] == device["id"]
    )
    assert device_after["status"] == "reconciled"
    assert device_after["matched_managed_asset_id"] == asset["id"]


async def test_accept_rejects_nonexistent_managed_asset(client, auth_headers):
    headers, external_id = await _discover_one_device(client, auth_headers)
    diffs = (await client.get("/api/v1/discovery/reconciliation", headers=headers, params={"status_filter": "pending"})).json()
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    device = next(d for d in devices if d["external_identifier"] == external_id)
    diff = next(d for d in diffs if d["discovered_device_id"] == device["id"])

    resp = await client.post(
        f"/api/v1/discovery/reconciliation/{diff['id']}/accept",
        json={"matched_managed_asset_id": str(uuid.uuid4())}, headers=headers,
    )
    assert resp.status_code == 404

    # The diff must remain pending -- a rejected accept must not silently decide it.
    diff_after = next(
        d for d in (await client.get("/api/v1/discovery/reconciliation", headers=headers, params={"status_filter": "pending"})).json()
        if d["id"] == diff["id"]
    )
    assert diff_after["status"] == "pending"


async def test_reject_leaves_device_unlinked(client, auth_headers):
    headers, external_id = await _discover_one_device(client, auth_headers)
    diffs = (await client.get("/api/v1/discovery/reconciliation", headers=headers, params={"status_filter": "pending"})).json()
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    device = next(d for d in devices if d["external_identifier"] == external_id)
    diff = next(d for d in diffs if d["discovered_device_id"] == device["id"])

    resp = await client.post(
        f"/api/v1/discovery/reconciliation/{diff['id']}/reject", json={"reason": "not a real device"}, headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "rejected"

    device_after = next(
        d for d in (await client.get("/api/v1/discovery/devices", headers=headers)).json() if d["id"] == device["id"]
    )
    assert device_after["status"] == "new"
    assert device_after["matched_managed_asset_id"] is None


async def test_deciding_an_already_decided_diff_is_rejected(client, auth_headers):
    headers, external_id = await _discover_one_device(client, auth_headers)
    diffs = (await client.get("/api/v1/discovery/reconciliation", headers=headers, params={"status_filter": "pending"})).json()
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    device = next(d for d in devices if d["external_identifier"] == external_id)
    diff = next(d for d in diffs if d["discovered_device_id"] == device["id"])

    resp1 = await client.post(f"/api/v1/discovery/reconciliation/{diff['id']}/reject", json={}, headers=headers)
    assert resp1.status_code == 200

    resp2 = await client.post(f"/api/v1/discovery/reconciliation/{diff['id']}/accept", json={}, headers=headers)
    assert resp2.status_code == 409


async def test_viewer_can_read_but_not_reconcile(client, auth_headers):
    manager_headers = await auth_headers("DCIM Manager")
    viewer_headers = await auth_headers("Viewer")
    _headers, external_id = await _discover_one_device(client, auth_headers)

    resp = await client.get("/api/v1/discovery/devices", headers=viewer_headers)
    assert resp.status_code == 200

    diffs = (
        await client.get("/api/v1/discovery/reconciliation", headers=manager_headers, params={"status_filter": "pending"})
    ).json()
    devices = (await client.get("/api/v1/discovery/devices", headers=manager_headers)).json()
    device = next(d for d in devices if d["external_identifier"] == external_id)
    diff = next(d for d in diffs if d["discovered_device_id"] == device["id"])

    resp = await client.post(f"/api/v1/discovery/reconciliation/{diff['id']}/accept", json={}, headers=viewer_headers)
    assert resp.status_code == 403


# --- DISC-ORDER-01: observation-order contract through the collector ingest API ------------------------------------


async def _device_state(db_session, external_identifier):
    row = (await db_session.execute(text(
        "SELECT last_seen_at, raw_attributes, status, profile_match FROM discovered_device WHERE external_identifier = :e"),
        {"e": external_identifier})).mappings().one()
    return dict(row)


async def _event_and_diff_counts(db_session):
    return (await db_session.execute(text(
        "SELECT (SELECT count(*) FROM discovered_device), (SELECT count(*) FROM reconciliation_diff), "
        "(SELECT count(*) FROM outbox_event WHERE event_type IN ('DeviceDiscovered', 'ReconciliationRequired'))"))).one()


def _timed_record(integration_id, external_identifier, occurred_at, dedup_key=None, **raw):
    return {
        "dedup_key": dedup_key or uuid.uuid4().hex, "integration_id": integration_id,
        "external_identifier": external_identifier, "occurred_at": occurred_at, "raw_attributes": raw,
    }


async def test_stale_collector_record_is_acknowledged_but_never_overwrites_newer_device_state(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    newer = _timed_record(integration["id"], "10.9.0.1", "2026-03-01T12:00:00Z", v="newer")
    older = _timed_record(integration["id"], "10.9.0.1", "2026-03-01T11:00:00Z", v="older")

    first = await ingest_records(client, collector, [newer])
    assert first["results"][0]["status"] == "accepted"
    counts_before = await _event_and_diff_counts(db_session)
    before = await _device_state(db_session, "10.9.0.1")

    stale = await ingest_records(client, collector, [older])  # different dedup key, older observation
    assert stale["results"][0]["status"] == "accepted"  # acknowledged: the edge may drop it
    await db_session.rollback()  # end the read snapshot so the next read sees the committed state
    assert await _device_state(db_session, "10.9.0.1") == before
    assert before["raw_attributes"]["v"] == "newer"
    assert before["last_seen_at"].isoformat().startswith("2026-03-01T12:00:00")
    assert tuple(await _event_and_diff_counts(db_session)) == tuple(counts_before)

    replay = await ingest_records(client, collector, [older])  # same dedup key again: idempotency ledger unchanged
    assert replay["results"][0]["status"] == "duplicate"


async def test_newer_collector_record_for_a_known_device_is_applied(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    await ingest_records(client, collector, [_timed_record(integration["id"], "10.9.0.2", "2026-03-01T11:00:00Z", v="older")])
    counts_before = await _event_and_diff_counts(db_session)
    result = await ingest_records(client, collector, [_timed_record(integration["id"], "10.9.0.2", "2026-03-01T12:00:00Z", v="newer")])
    assert result["results"][0]["status"] == "accepted"
    await db_session.rollback()
    state = await _device_state(db_session, "10.9.0.2")
    assert state["raw_attributes"]["v"] == "newer" and state["last_seen_at"].isoformat().startswith("2026-03-01T12:00:00")
    assert tuple(await _event_and_diff_counts(db_session)) == tuple(counts_before)  # update path emits no creation events


async def test_central_poll_uses_the_driver_observation_time_so_a_slow_poll_cannot_overwrite_a_newer_edge_observation(
    client, auth_headers, db_session, monkeypatch
):
    from datetime import UTC, datetime, timedelta

    from app.application.drivers import icmp as icmp_driver

    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp", target_host="127.0.0.1")
    now = datetime.now(UTC)
    edge = _timed_record(integration["id"], "127.0.0.1", (now - timedelta(minutes=10)).isoformat(), v="edge")
    assert (await ingest_records(client, collector, [edge]))["results"][0]["status"] == "accepted"

    # A poll whose acquisition started before the edge observation (slow, in-flight) finishes later.
    monkeypatch.setattr(icmp_driver, "utcnow", lambda: now - timedelta(hours=1))
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/poll-now", headers=headers)
    assert resp.status_code == 200 and resp.json()[0]["succeeded"] is True
    await db_session.rollback()
    state = await _device_state(db_session, "127.0.0.1")
    assert state["raw_attributes"]["v"] == "edge"  # not overwritten by the older poll result

    # A poll observed after the edge observation is applied normally.
    monkeypatch.setattr(icmp_driver, "utcnow", lambda: now - timedelta(minutes=1))
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/poll-now", headers=headers)
    assert resp.status_code == 200 and resp.json()[0]["succeeded"] is True
    await db_session.rollback()
    state = await _device_state(db_session, "127.0.0.1")
    assert state["raw_attributes"] == {"protocol": "icmp", "target_host": "127.0.0.1"}
    assert state["last_seen_at"] == now - timedelta(minutes=1)


async def test_replayed_far_future_collector_record_under_a_new_dedup_key_does_not_advance_the_device(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await collector_with_integration(client, headers, integration_type="icmp")
    far = "2099-01-01T00:00:00Z"
    first = _timed_record(integration["id"], "10.9.0.3", far, v="original")
    assert (await ingest_records(client, collector, [first]))["results"][0]["status"] == "accepted"
    await db_session.rollback()
    before = await _device_state(db_session, "10.9.0.3")

    replay = _timed_record(integration["id"], "10.9.0.3", far, v="replay")  # same observation, new dedup key
    assert (await ingest_records(client, collector, [replay]))["results"][0]["status"] == "accepted"
    await db_session.rollback()
    after = await _device_state(db_session, "10.9.0.3")
    assert after["last_seen_at"] == before["last_seen_at"]  # did not creep forward with the wall clock
    assert after["raw_attributes"]["v"] == "original"
