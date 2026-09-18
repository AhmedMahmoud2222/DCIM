"""API-level tests for Phase 8 collector identity/capability/assignment/heartbeat/
ingest endpoints."""

import uuid

from sqlalchemy import text

from tests.api._phase8_helpers import create_integration, register_collector, sign_request


async def test_register_central_collector(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers, collector_type="central")
    assert collector["collector_type"] == "central"
    assert collector["site_id"] is None
    assert len(collector["secret"]) > 20  # a real, generated secret was returned


async def test_central_collector_rejects_site_id(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    resp = await client.post(
        "/api/v1/collectors", json={"name": f"c-{uuid.uuid4().hex[:6]}", "collector_type": "central", "site_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 422


async def test_collector_name_must_be_unique(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    name = f"dup-{uuid.uuid4().hex[:8]}"
    await register_collector(client, headers, name=name)
    resp = await client.post("/api/v1/collectors", json={"name": name, "collector_type": "central"}, headers=headers)
    assert resp.status_code == 409


async def test_viewer_cannot_register_collector(client, auth_headers):
    headers = await auth_headers("Viewer")
    resp = await client.post("/api/v1/collectors", json={"name": "x", "collector_type": "central"}, headers=headers)
    assert resp.status_code == 403


async def test_list_and_get_collector_includes_computed_health(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp = await client.get(f"/api/v1/collectors/{collector['id']}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["health"] == "offline"  # no heartbeat ever recorded yet
    assert body["last_heartbeat_at"] is None


async def test_declare_and_list_capabilities(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp", "rest"]}, headers=headers)
    assert resp.status_code == 204
    resp = await client.get(f"/api/v1/collectors/{collector['id']}/capabilities", headers=headers)
    assert {c["protocol_code"] for c in resp.json()} == {"icmp", "rest"}

    # Re-declaring is idempotent -- no duplicate rows, no error.
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    assert resp.status_code == 204
    resp = await client.get(f"/api/v1/collectors/{collector['id']}/capabilities", headers=headers)
    assert len(resp.json()) == 2


async def test_assignment_requires_matching_capability(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)  # no capabilities declared
    integration = await create_integration(client, headers, integration_type="icmp")
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    assert resp.status_code == 422


async def test_assignment_succeeds_with_matching_capability_and_appears_on_integration(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp")

    resp = await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    assert resp.status_code == 201

    resp = await client.get(f"/api/v1/integrations/{integration['id']}", headers=headers)
    assert resp.json()["assigned_collector_id"] == collector["id"]


async def test_reassignment_closes_old_and_opens_new_row(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector_a = await register_collector(client, headers)
    collector_b = await register_collector(client, headers)
    for c in (collector_a, collector_b):
        await client.post(f"/api/v1/collectors/{c['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp")

    r1 = await client.post(f"/api/v1/collectors/{collector_a['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    assert r1.status_code == 201
    r2 = await client.post(f"/api/v1/collectors/{collector_b['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    assert r2.status_code == 201
    assert r2.json()["effective_to"] is None
    assert r1.json()["id"] != r2.json()["id"]

    resp = await client.get(f"/api/v1/integrations/{integration['id']}", headers=headers)
    assert resp.json()["assigned_collector_id"] == collector_b["id"]


async def test_assignment_to_disabled_collector_rejected(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp")

    # Flip status directly via the DB session the `client` fixture shares -- there is no
    # PATCH-status endpoint in this phase's minimal admin surface, and this is the
    # cleanest way to set up the precondition without inventing one just for the test.
    await db_session.execute(
        text("UPDATE collector SET status = 'disabled' WHERE id = :id"), {"id": collector["id"]},
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers,
    )
    assert resp.status_code == 409


async def test_assignment_rejects_collector_and_integration_scoped_to_different_sites(client, auth_headers):
    from tests.api._phase3_helpers import create_room_and_site

    headers = await auth_headers("DCIM Manager")
    _room_a, site_a = await create_room_and_site(client, auth_headers)
    _room_b, site_b = await create_room_and_site(client, auth_headers)

    collector = await register_collector(client, headers, collector_type="edge", site_id=site_a)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp", site_id=site_b)

    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers,
    )
    assert resp.status_code == 422

    # Same site is fine.
    same_site_integration = await create_integration(client, headers, integration_type="icmp", site_id=site_a)
    resp2 = await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": same_site_integration["id"]}, headers=headers,
    )
    assert resp2.status_code == 201


async def test_assignment_to_bogus_collector_id_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    integration = await create_integration(client, headers, integration_type="icmp")
    resp = await client.post(
        f"/api/v1/collectors/{uuid.uuid4()}/assignments", json={"integration_id": integration["id"]}, headers=headers,
    )
    assert resp.status_code == 404


async def test_heartbeat_requires_collector_signature_not_user_jwt(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)

    # A user JWT (even a valid, privileged one) must NOT authenticate a heartbeat --
    # the collector machine-trust boundary is a separate mechanism entirely.
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/heartbeat", json={"status": "ok"}, headers=headers)
    assert resp.status_code in (401, 422)  # missing X-Collector-* headers


async def test_heartbeat_with_valid_signature_updates_health_to_healthy(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    body = {"status": "ok", "queue_depth": 3}
    import json as _json
    raw_body = _json.dumps(body).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)

    resp = await client.post(f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"})
    assert resp.status_code == 204

    resp = await client.get(f"/api/v1/collectors/{collector['id']}", headers=headers)
    assert resp.json()["health"] == "healthy"


async def test_heartbeat_signature_replay_rejected(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    body = {"status": "ok"}
    import json as _json
    raw_body = _json.dumps(body).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)

    resp1 = await client.post(f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"})
    assert resp1.status_code == 204
    resp2 = await client.post(f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"})
    assert resp2.status_code == 401


async def test_heartbeat_signed_identity_must_match_url_path(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector_a = await register_collector(client, headers)
    collector_b = await register_collector(client, headers)
    body = {"status": "ok"}
    import json as _json
    raw_body = _json.dumps(body).encode()
    # Sign as collector_a but target collector_b's URL.
    sig_headers = sign_request(secret=collector_a["secret"], collector_id=uuid.UUID(collector_a["id"]), raw_body=raw_body)
    resp = await client.post(f"/api/v1/collectors/{collector_b['id']}/heartbeat", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"})
    assert resp.status_code == 401


async def test_heartbeat_rejects_oversized_status_as_clean_422_not_500(client, auth_headers):
    """Same class of bug as PHASE1_IMPLEMENTATION_RED_TEAM_REPORT.md Finding M1: an
    API-boundary field must be bounded to match its column's actual width
    (CollectorHeartbeat.status is String(16)), so an oversized value is rejected as a
    clean validation error, never reaching Postgres and surfacing as an unhandled 500."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    body = {"status": "x" * 17}
    import json as _json
    raw_body = _json.dumps(body).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)
    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/heartbeat", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422


async def test_ingest_batch_is_idempotent_on_duplicate_dedup_key(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)

    import json as _json
    dedup_key = uuid.uuid4().hex
    record = {
        "dedup_key": dedup_key, "integration_id": integration["id"], "external_identifier": "10.0.0.9",
        "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {"reachable": True},
    }
    payload = {"batch_id": uuid.uuid4().hex, "records": [record]}
    raw_body = _json.dumps(payload).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)

    resp1 = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"})
    assert resp1.status_code == 200
    assert resp1.json()["results"][0]["status"] == "accepted"

    # Exact replay of the same batch (new nonce/timestamp/signature, since those are
    # per-request, but the SAME dedup_key) must be idempotent, not double-processed.
    sig_headers2 = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)
    resp2 = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers={**sig_headers2, "Content-Type": "application/json"})
    assert resp2.status_code == 200
    assert resp2.json()["results"][0]["status"] == "duplicate"

    # Exactly one DiscoveredDevice must exist, not two.
    resp = await client.get("/api/v1/discovery/devices", headers=headers)
    matching = [d for d in resp.json() if d["external_identifier"] == "10.0.0.9"]
    assert len(matching) == 1


async def test_ingest_batch_rejects_oversized_batch(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    integration = await create_integration(client, headers, integration_type="icmp")

    import json as _json
    records = [
        {"dedup_key": uuid.uuid4().hex, "integration_id": integration["id"], "external_identifier": f"10.0.0.{i}",
         "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {}}
        for i in range(600)
    ]
    payload = {"batch_id": uuid.uuid4().hex, "records": records}
    raw_body = _json.dumps(payload).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"})
    assert resp.status_code == 413


async def test_ingest_batch_rejects_oversized_single_record_raw_attributes(client, auth_headers):
    """PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md finding S1: MAX_BATCH_RECORDS alone
    bounds record COUNT, not the size of any one record's `raw_attributes` blob -- a
    single record with a huge attribute payload must still be rejected, not silently
    accepted and written to the database."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    integration = await create_integration(client, headers, integration_type="icmp")

    import json as _json

    oversized_record = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"], "external_identifier": "10.0.0.99",
        "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {"blob": "x" * 20_000},
    }
    payload = {"batch_id": uuid.uuid4().hex, "records": [oversized_record]}
    raw_body = _json.dumps(payload).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)
    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422


async def test_ingest_batch_one_bad_record_does_not_fail_the_rest(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)

    import json as _json
    good = {"dedup_key": uuid.uuid4().hex, "integration_id": integration["id"], "external_identifier": "10.0.0.50",
            "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {}}
    bad = {"dedup_key": uuid.uuid4().hex, "integration_id": str(uuid.uuid4()), "external_identifier": "10.0.0.51",
           "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {}}  # integration_id does not exist -> FK violation
    payload = {"batch_id": uuid.uuid4().hex, "records": [good, bad]}
    raw_body = _json.dumps(payload).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"})
    assert resp.status_code == 200
    results = {r["dedup_key"]: r["status"] for r in resp.json()["results"]}
    assert results[good["dedup_key"]] == "accepted"
    assert results[bad["dedup_key"]] == "rejected"


async def test_poll_now_succeeds_on_real_icmp_and_creates_a_discovered_device(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp", target_host="127.0.0.1")
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)

    resp = await client.post(f"/api/v1/collectors/{collector['id']}/poll-now", headers=headers)
    assert resp.status_code == 200
    outcomes = {o["integration_id"]: o for o in resp.json()}
    assert outcomes[integration["id"]]["succeeded"] is True

    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    assert any(d["external_identifier"] == "127.0.0.1" for d in devices)


async def test_poll_now_one_failing_integration_does_not_prevent_a_sibling_from_succeeding(client, auth_headers):
    """Failure isolation (master prompt §14/§25 item 18): an SNMP integration run
    through the generic polling path always fails today (no real transport factory can
    be injected from this path -- a disclosed, honest limitation, see
    app/application/drivers/snmp.py), which makes it a convenient real failure to pair
    against a real, succeeding ICMP integration on the SAME collector in the SAME
    polling cycle."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp", "snmp"]}, headers=headers)
    icmp_integration = await create_integration(client, headers, integration_type="icmp", target_host="127.0.0.1")
    snmp_integration = await create_integration(client, headers, integration_type="snmp", target_host="127.0.0.1")
    for integ in (icmp_integration, snmp_integration):
        resp = await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integ["id"]}, headers=headers)
        assert resp.status_code == 201

    resp = await client.post(f"/api/v1/collectors/{collector['id']}/poll-now", headers=headers)
    assert resp.status_code == 200
    outcomes = {o["integration_id"]: o for o in resp.json()}
    assert outcomes[icmp_integration["id"]]["succeeded"] is True
    assert outcomes[snmp_integration["id"]]["succeeded"] is False
    assert outcomes[snmp_integration["id"]]["error"] is not None

    icmp_after = (await client.get(f"/api/v1/integrations/{icmp_integration['id']}", headers=headers)).json()
    snmp_after = (await client.get(f"/api/v1/integrations/{snmp_integration['id']}", headers=headers)).json()
    assert icmp_after["consecutive_failures"] == 0
    assert icmp_after["last_success_at"] is not None
    assert snmp_after["consecutive_failures"] == 1
    assert snmp_after["last_failure_at"] is not None


async def test_ingest_rejects_a_record_for_an_integration_not_assigned_to_this_collector(client, auth_headers):
    """PHASE8_IMPLEMENTATION_RED_TEAM_SCOPE.md finding S2: a valid collector signature
    proves identity, never authorization to report for an arbitrary `integration_id`.
    A registered collector must not be treated as an authoritative source for an
    integration it was never assigned -- not even when the integration_id is real and
    belongs to a DIFFERENT, legitimately-assigned collector."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    integration = await create_integration(client, headers, integration_type="icmp")
    # Deliberately never declare a capability or create an assignment for `collector`.

    import json as _json

    record = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"], "external_identifier": "10.0.0.77",
        "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {},
    }
    payload = {"batch_id": uuid.uuid4().hex, "records": [record]}
    raw_body = _json.dumps(payload).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)
    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/ingest", content=raw_body, headers={**sig_headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    results = {r["dedup_key"]: r["status"] for r in resp.json()["results"]}
    assert results[record["dedup_key"]] == "rejected"

    devices = (await client.get("/api/v1/discovery/devices", headers=headers)).json()
    assert not any(d["external_identifier"] == "10.0.0.77" for d in devices)
