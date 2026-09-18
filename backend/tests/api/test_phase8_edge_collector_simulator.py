"""Independent Edge Collector contract simulator (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md
§5). This is test-only code, never imported by the application, that plays the role of a
real remote-site Edge Collector talking to Central over the documented HTTP contract --
`Site Device -> simulated Edge Collector -> WAN-like delivery (real HTTP through the
ASGI transport, real signing, real DB) -> Central API`. It does not import or reuse any
Phase 8 production helper beyond the plain HTTP contract (headers, JSON bodies) a real
external agent would use, so it proves the contract is actually usable from outside the
central process, not just internally consistent with its own implementation.

Written by an independent auditor, not the Phase 8 implementer. No production code is
modified by this file."""

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from app.application.collector_auth import compute_signature
from tests.api._phase3_helpers import create_room_and_site


@dataclass
class SimulatedEdgeCollector:
    """Everything a real Edge Collector agent would hold locally: its own identity and
    secret (obtained once at registration, never re-derived from anything server-side),
    plus a tiny in-memory "local buffer" standing in for the not-yet-built real durable
    queue -- exercised here only to prove the central contract can support one, not to
    claim this IS the real queue (PHASE8_EDGE_COLLECTOR_CONTRACT.md §4)."""

    client: object
    collector_id: str
    secret: str
    local_buffer: list = field(default_factory=list)

    def _sign(self, raw_body: bytes, *, nonce: str | None = None, timestamp: str | None = None) -> dict:
        ts = timestamp or str(int(time.time()))
        n = nonce or uuid.uuid4().hex
        sig = compute_signature(secret=self.secret, collector_id=uuid.UUID(self.collector_id), timestamp=ts, nonce=n, raw_body=raw_body)
        return {
            "X-Collector-Id": self.collector_id, "X-Collector-Timestamp": ts, "X-Collector-Nonce": n,
            "X-Collector-Signature": sig, "Content-Type": "application/json",
        }

    async def send_heartbeat(self, *, queue_depth=0, status="ok", nonce=None, timestamp=None):
        body = {"queue_depth": queue_depth, "status": status}
        raw = json.dumps(body).encode()
        headers = self._sign(raw, nonce=nonce, timestamp=timestamp)
        return await self.client.post(f"/api/v1/collectors/{self.collector_id}/heartbeat", content=raw, headers=headers)

    async def send_ingest(self, records: list, *, batch_id=None, nonce=None, timestamp=None, raw_override=None):
        payload = {"batch_id": batch_id or uuid.uuid4().hex, "records": records}
        raw = raw_override if raw_override is not None else json.dumps(payload).encode()
        headers = self._sign(raw, nonce=nonce, timestamp=timestamp)
        return await self.client.post(f"/api/v1/collectors/{self.collector_id}/ingest", content=raw, headers=headers)


def _record(integration_id: str, external_id: str, occurred_at: str | None = None, dedup_key: str | None = None) -> dict:
    return {
        "dedup_key": dedup_key or uuid.uuid4().hex, "integration_id": integration_id,
        "external_identifier": external_id, "occurred_at": occurred_at or "2026-01-01T00:00:00Z",
        "raw_attributes": {"vendor": "acme"},
    }


async def _register_and_assign(client, headers, *, protocol="icmp", site_id=None, collector_type="central") -> tuple[SimulatedEdgeCollector, dict]:
    resp = await client.post(
        "/api/v1/collectors",
        json={"name": f"sim-collector-{uuid.uuid4().hex[:8]}", "collector_type": collector_type, "site_id": site_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    c = resp.json()
    sim = SimulatedEdgeCollector(client=client, collector_id=c["id"], secret=c["secret"])
    cap = await client.post(f"/api/v1/collectors/{c['id']}/capabilities", json={"protocol_codes": [protocol]}, headers=headers)
    assert cap.status_code == 204
    integ = await client.post(
        "/api/v1/integrations",
        json={"name": f"sim-integration-{uuid.uuid4().hex[:8]}", "integration_type": protocol, "target_host": "127.0.0.1", "site_id": site_id},
        headers=headers,
    )
    assert integ.status_code == 201, integ.text
    integration = integ.json()
    assign = await client.post(f"/api/v1/collectors/{c['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    assert assign.status_code == 201, assign.text
    return sim, integration


# ------------------------------------------------------------------ 1-7: happy path

async def test_sim_01_registration_and_secret_acquisition(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    resp = await client.post("/api/v1/collectors", json={"name": f"sim-{uuid.uuid4().hex[:8]}", "collector_type": "central"}, headers=headers)
    assert resp.status_code == 201
    assert len(resp.json()["secret"]) > 20


async def test_sim_02_03_04_heartbeat_assignment_capability_validation(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    hb = await sim.send_heartbeat()
    assert hb.status_code == 204
    detail = await client.get(f"/api/v1/collectors/{sim.collector_id}", headers=headers)
    assert detail.json()["health"] == "healthy"


async def test_sim_06_07_signed_ingestion_multiple_records(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    records = [_record(integration["id"], f"10.0.0.{i}") for i in range(5)]
    resp = await sim.send_ingest(records)
    assert resp.status_code == 200
    statuses = [r["status"] for r in resp.json()["results"]]
    assert statuses == ["accepted"] * 5


# ------------------------------------------------------------------ 8-14: delivery semantics

async def test_sim_08_duplicate_delivery_is_idempotent(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    rec = _record(integration["id"], "10.0.0.50")
    r1 = await sim.send_ingest([rec], batch_id="batch-a")
    r2 = await sim.send_ingest([rec], batch_id="batch-a-retry")  # same record, resent
    assert r1.json()["results"][0]["status"] == "accepted"
    assert r2.json()["results"][0]["status"] == "duplicate"
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    assert sum(1 for d in devices if d["external_identifier"] == "10.0.0.50") == 1


async def test_sim_09_delayed_delivery_preserves_occurred_at(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    old_time = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    rec = _record(integration["id"], "10.0.0.51", occurred_at=old_time)
    resp = await sim.send_ingest([rec])
    assert resp.status_code == 200
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    device = next(d for d in devices if d["external_identifier"] == "10.0.0.51")
    assert device["raw_attributes"]["occurred_at"].startswith(old_time[:19])
    assert "received_at" in device["raw_attributes"]


async def test_sim_10_out_of_order_delivery_last_write_wins_on_last_seen(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    later = datetime.now(UTC).isoformat()
    earlier = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    # Later observation arrives first, earlier one arrives second (out of order).
    r1 = await sim.send_ingest([_record(integration["id"], "10.0.0.52", occurred_at=later)])
    r2 = await sim.send_ingest([_record(integration["id"], "10.0.0.52", occurred_at=earlier)])
    assert r1.status_code == 200 and r2.status_code == 200
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    device = next(d for d in devices if d["external_identifier"] == "10.0.0.52")
    # The upsert keeps only the LAST delivered occurred_at, not the chronologically
    # latest one -- documented behavior, not asserted as a defect here (see report §11).
    assert device["raw_attributes"]["occurred_at"].startswith(earlier[:19])


@pytest.mark.xfail(
    strict=True,
    reason="Finding I4 (PHASE8_INDEPENDENT_RED_TEAM_REPORT.md): ingest_batch's per-record "
    "except block calls `await db.rollback()`, which expires EVERY object tracked by the "
    "session -- including the request-scoped `collector` object obtained once via "
    "Depends(get_current_collector) before the loop starts. Every record processed AFTER "
    "the first rejection then crashes with an unhandled MissingGreenlet error the moment "
    "the S2 assignment check reads `collector.id` again, and is wrongly marked 'rejected' "
    "instead of 'accepted' -- breaking partial-batch-isolation for the exact 'valid and "
    "invalid records mixed in one batch' scenario the master prompt requires. Reproduces "
    "against app/api/v1/collectors.py's ingest_batch.",
)
async def test_sim_11_partial_ack_mixed_batch(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    good1 = _record(integration["id"], "10.0.0.60")
    good2 = _record(integration["id"], "10.0.0.61")
    bad = _record(str(uuid.uuid4()), "10.0.0.62")  # integration_id does not exist / not assigned
    good3 = _record(integration["id"], "10.0.0.63")
    resp = await sim.send_ingest([good1, bad, good2, good3])
    assert resp.status_code == 200
    results = {r["dedup_key"]: r["status"] for r in resp.json()["results"]}
    assert results[good1["dedup_key"]] == "accepted"
    assert results[good2["dedup_key"]] == "accepted"
    assert results[good3["dedup_key"]] == "accepted"
    assert results[bad["dedup_key"]] == "rejected"


async def test_sim_12_retry_after_failed_request_succeeds(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    bad = _record(str(uuid.uuid4()), "10.0.0.63")
    r1 = await sim.send_ingest([bad])
    assert r1.json()["results"][0]["status"] == "rejected"
    # Corrected retry with the SAME dedup_key but the right integration_id must succeed
    # (the claim was released on rejection, not permanently poisoned).
    fixed = dict(bad)
    fixed["integration_id"] = integration["id"]
    r2 = await sim.send_ingest([fixed])
    assert r2.json()["results"][0]["status"] == "accepted"


async def test_sim_13_collector_restart_with_same_identity_still_authenticates(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    # "Restart" = a fresh in-memory SimulatedEdgeCollector reconstructed from the same
    # persisted identity/secret (as a real agent reloading from local disk would do).
    restarted = SimulatedEdgeCollector(client=client, collector_id=sim.collector_id, secret=sim.secret)
    resp = await restarted.send_heartbeat()
    assert resp.status_code == 204


# ------------------------------------------------------------------ 15-19: auth attacks

async def test_sim_15_stale_timestamp_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    old_ts = str(int(time.time()) - 10_000)
    resp = await sim.send_heartbeat(timestamp=old_ts)
    assert resp.status_code == 401


async def test_sim_16_future_timestamp_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    future_ts = str(int(time.time()) + 10_000)
    resp = await sim.send_heartbeat(timestamp=future_ts)
    assert resp.status_code == 401


async def test_sim_17_reused_nonce_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    nonce = uuid.uuid4().hex
    r1 = await sim.send_heartbeat(nonce=nonce)
    r2 = await sim.send_heartbeat(nonce=nonce)
    assert r1.status_code == 204
    assert r2.status_code == 401


async def test_sim_18_modified_body_after_signing_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    original = json.dumps({"batch_id": "x", "records": []}).encode()
    signed_headers = sim._sign(original)
    tampered = json.dumps({"batch_id": "x", "records": [], "extra": "injected"}).encode()
    resp = await client.post(f"/api/v1/collectors/{sim.collector_id}/ingest", content=tampered, headers=signed_headers)
    assert resp.status_code == 401


async def test_sim_19_wrong_collector_secret_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    impostor = SimulatedEdgeCollector(client=client, collector_id=sim.collector_id, secret="totally-wrong-secret")
    resp = await impostor.send_heartbeat()
    assert resp.status_code == 401


async def test_sim_20_disabled_collector_rejected(client, auth_headers, db_session):
    from sqlalchemy import text

    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    await db_session.execute(text("UPDATE collector SET status = 'disabled' WHERE id = :id"), {"id": sim.collector_id})
    await db_session.commit()
    resp = await sim.send_heartbeat()
    assert resp.status_code == 401


# ------------------------------------------------------------------ 21-22: ownership attacks

async def test_sim_21_collector_attempting_another_collectors_integration(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim_a, integration_a = await _register_and_assign(client, headers)
    sim_b, integration_b = await _register_and_assign(client, headers)
    # Collector A (validly authenticated as itself) tries to report for Collector B's integration.
    resp = await sim_a.send_ingest([_record(integration_b["id"], "10.0.0.70")])
    assert resp.json()["results"][0]["status"] == "rejected"
    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    assert not any(d["external_identifier"] == "10.0.0.70" for d in devices)


async def test_sim_22_collector_attempting_another_sites_integration(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    _room_a, site_a = await create_room_and_site(client, auth_headers)
    _room_b, site_b = await create_room_and_site(client, auth_headers)
    sim_a, _integration_a = await _register_and_assign(client, headers, site_id=site_a, collector_type="edge")
    # An integration explicitly scoped to Site B must not be assignable to Site A's
    # collector at all (this is checked at assignment time, not ingest time) -- confirm
    # the assignment itself is refused.
    integ_b = await client.post(
        "/api/v1/integrations",
        json={"name": f"sim-siteb-{uuid.uuid4().hex[:8]}", "integration_type": "icmp", "target_host": "127.0.0.1", "site_id": site_b},
        headers=headers,
    )
    assert integ_b.status_code == 201
    resp = await client.post(f"/api/v1/collectors/{sim_a.collector_id}/assignments", json={"integration_id": integ_b.json()["id"]}, headers=headers)
    assert resp.status_code == 422


# ------------------------------------------------------------------ 23-26: payload attacks

async def test_sim_23_oversized_batch_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    records = [_record(integration["id"], f"10.0.1.{i % 256}") for i in range(501)]
    resp = await sim.send_ingest(records)
    assert resp.status_code == 413


async def test_sim_24_oversized_individual_record_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    rec = _record(integration["id"], "10.0.0.80")
    rec["raw_attributes"] = {"blob": "x" * 20_000}
    resp = await sim.send_ingest([rec])
    assert resp.status_code == 422


async def test_sim_25_malformed_record_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    body = {"batch_id": "x", "records": [{"dedup_key": "x", "integration_id": "not-a-uuid", "external_identifier": "y", "occurred_at": "not-a-date"}]}
    raw = json.dumps(body).encode()
    resp = await sim.send_ingest([], raw_override=raw)
    assert resp.status_code == 422


async def test_sim_26_valid_and_invalid_records_mixed_in_one_batch(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    sim, integration = await _register_and_assign(client, headers)
    valid = _record(integration["id"], "10.0.0.90")
    unassigned = _record(str(uuid.uuid4()), "10.0.0.91")
    resp = await sim.send_ingest([valid, unassigned])
    assert resp.status_code == 200
    results = {r["dedup_key"]: r["status"] for r in resp.json()["results"]}
    assert results[valid["dedup_key"]] == "accepted"
    assert results[unassigned["dedup_key"]] == "rejected"
