"""API-level tests for Phase 8 collector identity/capability/assignment/heartbeat/
ingest endpoints."""

import io
import json
import uuid

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.api.v1 import collectors as collector_api
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


@pytest.mark.parametrize("failure_kind", ["integrity", "runtime"])
async def test_ingest_unexpected_failure_never_logs_or_returns_sensitive_data(
    client, auth_headers, monkeypatch, failure_kind,
):
    """SEC-07: SQL parameters and exception text are untrusted even on the server."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities",
        json={"protocol_codes": ["icmp"]}, headers=headers,
    )
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments",
        json={"integration_id": integration["id"]}, headers=headers,
    )

    credential = "synthetic-credential-SEC07-keep-private"
    payload_secret = "synthetic-telemetry-SEC07-keep-private"
    sql_parameter = "synthetic-sql-param-SEC07-keep-private"
    external_id = "synthetic-device-id-SEC07-keep-private"
    bad = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": external_id, "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"credential": credential, "telemetry": payload_secret},
    }
    good = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.52", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"reachable": True},
    }
    batch_id = uuid.uuid4().hex
    raw_body = json.dumps({"batch_id": batch_id, "records": [bad, good]}).encode()
    signed = sign_request(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body,
    )

    original_ingest = collector_api.ingest_discovery

    async def fail_one_record(*args, **kwargs):
        if kwargs["external_identifier"] == external_id:
            if failure_kind == "integrity":
                raise IntegrityError(
                    "INSERT INTO discovered_device (credential) VALUES (:private_value)",
                    {"private_value": sql_parameter},
                    ValueError(f"database rejected {payload_secret}"),
                )
            raise RuntimeError(f"unexpected {credential} {sql_parameter}")
        return await original_ingest(*args, **kwargs)

    monkeypatch.setattr(collector_api, "ingest_discovery", fail_one_record)
    log_output = io.StringIO()
    # Capture the emitted structured JSON, not only the arguments passed to a mock.
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output),
        processors=[structlog.processors.JSONRenderer()],
    )
    monkeypatch.setattr(collector_api, "logger", logger)

    response = await client.post(
        f"/api/v1/collectors/{collector['id']}/ingest",
        content=raw_body, headers={**signed, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["results"] == [
        {
            "dedup_key": bad["dedup_key"], "status": "rejected",
            "error_code": "INTERNAL_PROCESSING_ERROR",
            "error": "An internal error occurred while processing this record.",
        },
        {
            "dedup_key": good["dedup_key"], "status": "accepted",
            "error_code": None, "error": None,
        },
    ]
    for sensitive in (credential, payload_secret, sql_parameter, external_id):
        assert sensitive not in response.text

    events = [json.loads(line) for line in log_output.getvalue().splitlines()]
    assert events == [{
        "event": "ingest_batch_record_processing_failed",
        "error_code": "INTERNAL_PROCESSING_ERROR",
        "record_index": 0,
    }]
    for sensitive in (
        credential, payload_secret, sql_parameter, external_id,
        bad["dedup_key"], batch_id, integration["id"], collector["id"],
    ):
        assert sensitive not in log_output.getvalue()


@pytest.mark.parametrize("failure_kind", ["integrity", "runtime"])
async def test_ingest_claim_release_failure_never_logs_or_returns_sensitive_data(
    client, auth_headers, monkeypatch, failure_kind,
):
    """Codex's Phase 11 independent review (Issue #38): `idem.release_claim()` runs its
    own DELETE + COMMIT and can itself fail. Left uncaught, that new exception would
    propagate past `ingest_batch()`'s own sanitized logging entirely and reach the
    GLOBAL handlers in app/core/errors.py, which log unsanitized exception text --
    and, via Python's implicit exception chaining, would also print the ORIGINAL
    sensitive exception this record was already handling. Mirrors
    test_ingest_unexpected_failure_never_logs_or_returns_sensitive_data above, but
    forces the failure inside release_claim itself rather than inside
    ingest_discovery, with a distinct synthetic secret so a passing test can only mean
    release_claim's own failure path is what's being contained."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities",
        json={"protocol_codes": ["icmp"]}, headers=headers,
    )
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments",
        json={"integration_id": integration["id"]}, headers=headers,
    )

    original_credential = "synthetic-credential-SEC07B-original-keep-private"
    release_secret = "synthetic-release-SEC07B-keep-private"
    bad = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.60", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"credential": original_credential},
    }
    good = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.61", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"reachable": True},
    }
    batch_id = uuid.uuid4().hex
    raw_body = json.dumps({"batch_id": batch_id, "records": [bad, good]}).encode()
    signed = sign_request(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body,
    )

    original_ingest = collector_api.ingest_discovery

    async def fail_bad_record(*args, **kwargs):
        if kwargs["external_identifier"] == bad["external_identifier"]:
            if failure_kind == "integrity":
                raise IntegrityError(
                    "INSERT INTO discovered_device (credential) VALUES (:private_value)",
                    {"private_value": original_credential},
                    ValueError(f"database rejected {original_credential}"),
                )
            raise RuntimeError(f"unexpected {original_credential}")
        return await original_ingest(*args, **kwargs)

    async def fail_release_claim(*args, **kwargs):
        raise RuntimeError(f"release failed: {release_secret}")

    monkeypatch.setattr(collector_api, "ingest_discovery", fail_bad_record)
    monkeypatch.setattr(collector_api.idem, "release_claim", fail_release_claim)
    log_output = io.StringIO()
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output),
        processors=[structlog.processors.JSONRenderer()],
    )
    monkeypatch.setattr(collector_api, "logger", logger)

    response = await client.post(
        f"/api/v1/collectors/{collector['id']}/ingest",
        content=raw_body, headers={**signed, "Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["results"] == [
        {
            "dedup_key": bad["dedup_key"], "status": "rejected",
            "error_code": "INTERNAL_PROCESSING_ERROR",
            "error": "An internal error occurred while processing this record.",
        },
        {
            "dedup_key": good["dedup_key"], "status": "accepted",
            "error_code": None, "error": None,
        },
    ]
    for sensitive in (original_credential, release_secret):
        assert sensitive not in response.text

    log_text = log_output.getvalue()
    events = [json.loads(line) for line in log_text.splitlines()]
    assert {"event": "ingest_batch_claim_release_failed", "record_index": 0} in events
    for sensitive in (
        original_credential, release_secret,
        bad["dedup_key"], batch_id, integration["id"], collector["id"],
    ):
        assert sensitive not in log_text


@pytest.mark.parametrize("failure_kind", ["integrity", "runtime"])
async def test_ingest_claim_release_and_rollback_failure_aborts_batch_safely(
    client, auth_headers, monkeypatch, failure_kind,
):
    """Codex's second-round Phase 11 review (Issue #38 / PR #49): if `db.rollback()`
    itself fails after `release_claim()` already failed, the new exception must not
    escape `_release_claim_safely()` unsanitized either -- left uncaught it would
    propagate past `ingest_batch()` entirely, and the global catch-all handler's
    `exc_info=True` would chain all three exceptions (the original ingest failure, the
    release failure, and the rollback failure) into one traceback. Forces all three to
    fail with distinct synthetic secrets and confirms none reach logs or the response,
    and that the batch aborts outright (no 200 with an accepted ACK resting on a
    session that failed to even roll back) rather than continuing to the sibling
    record."""
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities",
        json={"protocol_codes": ["icmp"]}, headers=headers,
    )
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments",
        json={"integration_id": integration["id"]}, headers=headers,
    )

    original_credential = "synthetic-credential-SEC07C-original-keep-private"
    release_secret = "synthetic-release-SEC07C-keep-private"
    rollback_secret = "synthetic-rollback-SEC07C-keep-private"
    bad = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.70", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"credential": original_credential},
    }
    good = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.71", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"reachable": True},
    }
    batch_id = uuid.uuid4().hex
    raw_body = json.dumps({"batch_id": batch_id, "records": [bad, good]}).encode()
    signed = sign_request(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body,
    )

    original_ingest = collector_api.ingest_discovery

    async def fail_bad_record(*args, **kwargs):
        if kwargs["external_identifier"] == bad["external_identifier"]:
            if failure_kind == "integrity":
                raise IntegrityError(
                    "INSERT INTO discovered_device (credential) VALUES (:private_value)",
                    {"private_value": original_credential},
                    ValueError(f"database rejected {original_credential}"),
                )
            raise RuntimeError(f"unexpected {original_credential}")
        return await original_ingest(*args, **kwargs)

    async def fail_release_claim(*args, **kwargs):
        raise RuntimeError(f"release failed: {release_secret}")

    async def fail_rollback(self, *args, **kwargs):
        raise RuntimeError(f"rollback failed: {rollback_secret}")

    monkeypatch.setattr(collector_api, "ingest_discovery", fail_bad_record)
    monkeypatch.setattr(collector_api.idem, "release_claim", fail_release_claim)
    monkeypatch.setattr(AsyncSession, "rollback", fail_rollback)
    log_output = io.StringIO()
    logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output),
        processors=[structlog.processors.JSONRenderer()],
    )
    monkeypatch.setattr(collector_api, "logger", logger)

    response = await client.post(
        f"/api/v1/collectors/{collector['id']}/ingest",
        content=raw_body, headers={**signed, "Content-Type": "application/json"},
    )

    assert response.status_code == 503
    assert response.json()["status"] == 503
    for sensitive in (original_credential, release_secret, rollback_secret):
        assert sensitive not in response.text

    log_text = log_output.getvalue()
    events = [json.loads(line) for line in log_text.splitlines()]
    assert {"event": "ingest_batch_claim_release_rollback_failed", "record_index": 0} in events
    for sensitive in (
        original_credential, release_secret, rollback_secret,
        bad["dedup_key"], batch_id, integration["id"], collector["id"],
    ):
        assert sensitive not in log_text


async def test_ingest_release_route_rollback_and_cleanup_rollback_failures_all_contained(
    client, auth_headers, monkeypatch,
):
    """Codex's third-round Phase 11 review (Issue #38 / PR #49): app/db/session.py's
    get_db() dependency has its OWN cleanup rollback (`except Exception: await
    session.rollback(); raise`), which runs on ANY exception escaping the route --
    including the sanitized ApiError(503) _release_claim_safely() now raises when its
    own route-level rollback fails. If THAT cleanup rollback also fails, the raw
    cleanup exception previously replaced the sanitized ApiError and could reach the
    global catch-all handler unsanitized.

    The `client` fixture's own get_db override is a bare passthrough
    (`yield db_session`) that never exercises production cleanup at all -- exactly
    what Codex flagged the prior release/rollback test for. This test restores the
    REAL get_db dependency for the ingest requests, forces all four failure points
    (original ingest, release_claim, the route-level rollback, and get_db's own
    cleanup rollback) with four distinct synthetic secrets, and confirms: the
    response stays a generic 503 (not a 500 from the unguarded cleanup failure), none
    of the four secrets reach the response or any of the three loggers involved
    (route, global handlers, session cleanup) -- and, on an unpatched retry of the
    identical batch, the record that already committed before the failure replays as
    a duplicate, proving no data loss and that the batch remains valid for retry."""
    import app.core.errors as errors_module
    import app.db.session as session_module
    from app.db.session import get_db as real_get_db
    from app.main import app as fastapi_app

    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities",
        json={"protocol_codes": ["icmp"]}, headers=headers,
    )
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments",
        json={"integration_id": integration["id"]}, headers=headers,
    )

    original_credential = "synthetic-credential-SEC07D-original-keep-private"
    release_secret = "synthetic-release-SEC07D-keep-private"
    route_rollback_secret = "synthetic-route-rollback-SEC07D-keep-private"
    cleanup_rollback_secret = "synthetic-cleanup-rollback-SEC07D-keep-private"

    good_before = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.80", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"reachable": True},
    }
    bad = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.81", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"credential": original_credential},
    }
    good_after = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.82", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"reachable": True},
    }
    batch_id = uuid.uuid4().hex
    raw_body = json.dumps({"batch_id": batch_id, "records": [good_before, bad, good_after]}).encode()
    signed = sign_request(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body,
    )

    original_ingest = collector_api.ingest_discovery
    original_release_claim = collector_api.idem.release_claim
    original_rollback = AsyncSession.rollback

    async def fail_bad_record(*args, **kwargs):
        if kwargs["external_identifier"] == bad["external_identifier"]:
            raise RuntimeError(f"unexpected {original_credential}")
        return await original_ingest(*args, **kwargs)

    async def fail_release_claim(*args, **kwargs):
        raise RuntimeError(f"release failed: {release_secret}")

    rollback_calls = {"count": 0}

    async def fail_rollback(self, *args, **kwargs):
        rollback_calls["count"] += 1
        if rollback_calls["count"] == 1:
            raise RuntimeError(f"route rollback failed: {route_rollback_secret}")
        raise RuntimeError(f"cleanup rollback failed: {cleanup_rollback_secret}")

    log_output = io.StringIO()
    shared_logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output),
        processors=[structlog.processors.JSONRenderer()],
    )

    monkeypatch.setattr(collector_api, "ingest_discovery", fail_bad_record)
    monkeypatch.setattr(collector_api.idem, "release_claim", fail_release_claim)
    monkeypatch.setattr(AsyncSession, "rollback", fail_rollback)
    monkeypatch.setattr(collector_api, "logger", shared_logger)
    monkeypatch.setattr(errors_module, "logger", shared_logger)
    monkeypatch.setattr(session_module, "logger", shared_logger, raising=False)

    original_override = fastapi_app.dependency_overrides.get(real_get_db)
    fastapi_app.dependency_overrides.pop(real_get_db, None)
    try:
        response = await client.post(
            f"/api/v1/collectors/{collector['id']}/ingest",
            content=raw_body, headers={**signed, "Content-Type": "application/json"},
        )
    finally:
        if original_override is not None:
            fastapi_app.dependency_overrides[real_get_db] = original_override
        # The real get_db() above used app.db.session's own module-level engine/pool --
        # a process-wide singleton, unlike the per-test db_engine fixture. The injected
        # rollback failures leave its connection in a bad state bound to THIS test's
        # event loop; left pooled, the next test (a fresh event loop) would check it
        # out and crash with "attached to a different loop" / "Event loop is closed".
        # Disposing here forces a fresh connection for every later test.
        await session_module.engine.dispose()

    assert response.status_code == 503, response.text
    assert response.json()["status"] == 503
    for sensitive in (original_credential, release_secret, route_rollback_secret, cleanup_rollback_secret):
        assert sensitive not in response.text

    log_text = log_output.getvalue()
    events = [json.loads(line) for line in log_text.splitlines()]
    assert {"event": "ingest_batch_claim_release_rollback_failed", "record_index": 1} in events
    assert any(e.get("event") == "db_session_cleanup_rollback_failed" for e in events)
    for sensitive in (
        original_credential, release_secret, route_rollback_secret, cleanup_rollback_secret,
        good_before["dedup_key"], bad["dedup_key"], good_after["dedup_key"],
        batch_id, integration["id"], collector["id"],
    ):
        assert sensitive not in log_text

    monkeypatch.setattr(collector_api, "ingest_discovery", original_ingest)
    monkeypatch.setattr(collector_api.idem, "release_claim", original_release_claim)
    monkeypatch.setattr(AsyncSession, "rollback", original_rollback)

    # A real collector retry re-signs the same batch content with a fresh nonce --
    # replaying the identical signed request is itself correctly rejected by the
    # HMAC replay-protection layer (a single-use nonce), independent of anything
    # this test is verifying.
    retry_signed = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)
    retry_response = await client.post(
        f"/api/v1/collectors/{collector['id']}/ingest",
        content=raw_body, headers={**retry_signed, "Content-Type": "application/json"},
    )
    assert retry_response.status_code == 200, retry_response.text
    retry_results = {r["dedup_key"]: r for r in retry_response.json()["results"]}
    assert retry_results[good_before["dedup_key"]]["status"] == "duplicate"
    assert retry_results[good_after["dedup_key"]]["status"] == "accepted"
    # `bad`'s claim from the aborted attempt above was never released (the real
    # release_claim() was mocked out before it could run) -- it is genuinely still
    # "processing" in the database, so an immediate retry correctly finds it still
    # claimed rather than silently duplicating or losing it. It becomes reclaimable
    # again only after idempotency.STALE_CLAIM_TIMEOUT.
    assert retry_results[bad["dedup_key"]]["status"] == "rejected"
    assert retry_results[bad["dedup_key"]]["error_code"] == "PROCESSING"


async def test_ingest_release_rollback_and_session_close_failures_all_contained(
    client, auth_headers, monkeypatch,
):
    """Codex's fourth-round Phase 11 review (Issue #38 / PR #49): the prior fix wrapped
    get_db()'s cleanup rollback, but `async with AsyncSessionLocal() as session:` still
    ran `AsyncSession.__aexit__`'s own `close()` OUTSIDE any guard -- a failing close()
    would replace the just-recovered, sanitized `ApiError(503)` with its own raw
    exception one layer further out, exactly like the rollback failure this same PR
    already fixed twice over. get_db() now manages the session manually (see its
    docstring) so `close()`, and a best-effort `invalidate()` when rollback itself
    failed, are individually guarded too.

    Forces a FIFTH distinct failure point -- `AsyncSession.close()` -- on top of the
    four the prior test in this file already covers, using the same real-dependency
    technique, and confirms the response and every logger involved stay exactly as
    safe as before, and that a record after the failing one is never processed."""
    import app.core.errors as errors_module
    import app.db.session as session_module
    from app.db.session import get_db as real_get_db
    from app.main import app as fastapi_app

    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities",
        json={"protocol_codes": ["icmp"]}, headers=headers,
    )
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments",
        json={"integration_id": integration["id"]}, headers=headers,
    )

    original_credential = "synthetic-credential-SEC07E-original-keep-private"
    release_secret = "synthetic-release-SEC07E-keep-private"
    route_rollback_secret = "synthetic-route-rollback-SEC07E-keep-private"
    cleanup_rollback_secret = "synthetic-cleanup-rollback-SEC07E-keep-private"
    close_secret = "synthetic-session-close-SEC07E-keep-private"

    bad = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.90", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"credential": original_credential},
    }
    good_after = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.91", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"reachable": True},
    }
    batch_id = uuid.uuid4().hex
    raw_body = json.dumps({"batch_id": batch_id, "records": [bad, good_after]}).encode()
    signed = sign_request(
        secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body,
    )

    original_ingest = collector_api.ingest_discovery

    async def fail_bad_record(*args, **kwargs):
        if kwargs["external_identifier"] == bad["external_identifier"]:
            raise RuntimeError(f"unexpected {original_credential}")
        return await original_ingest(*args, **kwargs)

    async def fail_release_claim(*args, **kwargs):
        raise RuntimeError(f"release failed: {release_secret}")

    rollback_calls = {"count": 0}

    async def fail_rollback(self, *args, **kwargs):
        rollback_calls["count"] += 1
        if rollback_calls["count"] == 1:
            raise RuntimeError(f"route rollback failed: {route_rollback_secret}")
        raise RuntimeError(f"cleanup rollback failed: {cleanup_rollback_secret}")

    async def fail_close(self, *args, **kwargs):
        raise RuntimeError(f"close failed: {close_secret}")

    invalidate_calls = {"count": 0}
    original_invalidate = AsyncConnection.invalidate

    async def spy_invalidate(self, *args, **kwargs):
        invalidate_calls["count"] += 1
        return await original_invalidate(self, *args, **kwargs)

    log_output = io.StringIO()
    shared_logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output),
        processors=[structlog.processors.JSONRenderer()],
    )

    monkeypatch.setattr(collector_api, "ingest_discovery", fail_bad_record)
    monkeypatch.setattr(collector_api.idem, "release_claim", fail_release_claim)
    monkeypatch.setattr(AsyncSession, "rollback", fail_rollback)
    monkeypatch.setattr(AsyncSession, "close", fail_close)
    # SEC (Codex PR #49 review, ROUND 8): get_db()'s own invalidate fallback now calls
    # AsyncConnection.invalidate() directly on a connection snapshotted before rollback/
    # close (see app/db/session.py's docstring for why session.invalidate() itself is
    # unreliable here) -- spy at that level, not AsyncSession.invalidate, which this path
    # no longer calls at all.
    monkeypatch.setattr(AsyncConnection, "invalidate", spy_invalidate)
    monkeypatch.setattr(collector_api, "logger", shared_logger)
    monkeypatch.setattr(errors_module, "logger", shared_logger)
    monkeypatch.setattr(session_module, "logger", shared_logger, raising=False)

    original_override = fastapi_app.dependency_overrides.get(real_get_db)
    fastapi_app.dependency_overrides.pop(real_get_db, None)
    try:
        response = await client.post(
            f"/api/v1/collectors/{collector['id']}/ingest",
            content=raw_body, headers={**signed, "Content-Type": "application/json"},
        )
    finally:
        if original_override is not None:
            fastapi_app.dependency_overrides[real_get_db] = original_override
        # Same reasoning as the four-failure test above: app.db.session's engine is a
        # process-wide singleton, and the injected failures leave its connection bound
        # to this test's event loop -- dispose it so no later test inherits it.
        await session_module.engine.dispose()

    assert response.status_code == 503, response.text
    body = response.json()
    assert body["status"] == 503
    assert "results" not in body  # good_after was never reached, let alone processed
    for sensitive in (original_credential, release_secret, route_rollback_secret, cleanup_rollback_secret, close_secret):
        assert sensitive not in response.text

    # The cleanup rollback failed, so get_db() must have discarded the connection via
    # invalidate() rather than silently handing a known-broken one to a later request.
    assert invalidate_calls["count"] >= 1

    log_text = log_output.getvalue()
    events = [json.loads(line) for line in log_text.splitlines()]
    assert any(e.get("event") == "db_session_cleanup_rollback_failed" for e in events)
    assert any(e.get("event") == "db_session_cleanup_close_failed" for e in events)
    for sensitive in (
        original_credential, release_secret, route_rollback_secret, cleanup_rollback_secret, close_secret,
        bad["dedup_key"], good_after["dedup_key"], batch_id, integration["id"], collector["id"],
    ):
        assert sensitive not in log_text


async def test_ingest_close_failure_after_successful_rollback_stays_safe(client, auth_headers, monkeypatch):
    """Codex's fourth-round review asked specifically for a close failure DISTINCT from
    a rollback failure -- proving close() is guarded independently, not only as a
    side effect of the rollback-failure path. Here `_release_claim_safely()`'s own
    `db.rollback()` succeeds normally (the existing, already-safe NOT_ASSIGNED-style
    outcome), so the request completes as an ordinary 200 with a rejected ACK -- but
    `get_db()`'s cleanup then hits `session.close()` failing on its own, on what is
    otherwise get_db()'s HAPPY exit path (no exception was propagating out of the
    route at all). Confirms the close failure is contained there too: the normal 200
    response is unaffected, and the close secret never reaches it or any logger."""
    import app.core.errors as errors_module
    import app.db.session as session_module
    from app.db.session import get_db as real_get_db
    from app.main import app as fastapi_app

    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities",
        json={"protocol_codes": ["icmp"]}, headers=headers,
    )
    integration = await create_integration(client, headers, integration_type="icmp")
    # Deliberately no assignment -- current_assignment() returns None, so the record
    # takes the existing, already-safe ApiError("Not Assigned") path, whose rollback
    # (the savepoint's automatic one, then _release_claim_safely()'s real rollback)
    # succeeds normally; only close() is broken here.

    close_secret = "synthetic-session-close-SEC07F-keep-private"
    record = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.95", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {},
    }
    batch_id = uuid.uuid4().hex
    raw_body = json.dumps({"batch_id": batch_id, "records": [record]}).encode()
    signed = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)

    async def fail_close(self, *args, **kwargs):
        raise RuntimeError(f"close failed: {close_secret}")

    log_output = io.StringIO()
    shared_logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output),
        processors=[structlog.processors.JSONRenderer()],
    )
    monkeypatch.setattr(AsyncSession, "close", fail_close)
    monkeypatch.setattr(collector_api, "logger", shared_logger)
    monkeypatch.setattr(errors_module, "logger", shared_logger)
    monkeypatch.setattr(session_module, "logger", shared_logger, raising=False)

    original_override = fastapi_app.dependency_overrides.get(real_get_db)
    fastapi_app.dependency_overrides.pop(real_get_db, None)
    try:
        response = await client.post(
            f"/api/v1/collectors/{collector['id']}/ingest",
            content=raw_body, headers={**signed, "Content-Type": "application/json"},
        )
    finally:
        if original_override is not None:
            fastapi_app.dependency_overrides[real_get_db] = original_override
        await session_module.engine.dispose()

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "rejected"
    assert result["error_code"] == "NOT_ASSIGNED"
    assert close_secret not in response.text

    log_text = log_output.getvalue()
    assert {"event": "db_session_cleanup_close_failed"} in [json.loads(line) for line in log_text.splitlines()]
    assert close_secret not in log_text


async def test_ingest_close_failure_alone_on_a_fully_successful_request_stays_safe(client, auth_headers, monkeypatch):
    """The purest form of Codex's "independent close failure" ask: NOTHING else fails --
    no ingest error, no release_claim call, no rollback at all -- yet get_db()'s
    close() still fails on its own during a fully successful request's cleanup.
    Confirms this never surfaces to the client (still 200, still the real accepted
    result) and is only ever logged as the fixed, safe event."""
    import app.core.errors as errors_module
    import app.db.session as session_module
    from app.db.session import get_db as real_get_db
    from app.main import app as fastapi_app

    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities",
        json={"protocol_codes": ["icmp"]}, headers=headers,
    )
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments",
        json={"integration_id": integration["id"]}, headers=headers,
    )

    close_secret = "synthetic-session-close-SEC07G-keep-private"
    record = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.96", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"reachable": True},
    }
    batch_id = uuid.uuid4().hex
    raw_body = json.dumps({"batch_id": batch_id, "records": [record]}).encode()
    signed = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)

    async def fail_close(self, *args, **kwargs):
        raise RuntimeError(f"close failed: {close_secret}")

    log_output = io.StringIO()
    shared_logger = structlog.wrap_logger(
        structlog.PrintLogger(file=log_output),
        processors=[structlog.processors.JSONRenderer()],
    )
    monkeypatch.setattr(AsyncSession, "close", fail_close)
    monkeypatch.setattr(collector_api, "logger", shared_logger)
    monkeypatch.setattr(errors_module, "logger", shared_logger)
    monkeypatch.setattr(session_module, "logger", shared_logger, raising=False)

    original_override = fastapi_app.dependency_overrides.get(real_get_db)
    fastapi_app.dependency_overrides.pop(real_get_db, None)
    try:
        response = await client.post(
            f"/api/v1/collectors/{collector['id']}/ingest",
            content=raw_body, headers={**signed, "Content-Type": "application/json"},
        )
    finally:
        if original_override is not None:
            fastapi_app.dependency_overrides[real_get_db] = original_override
        await session_module.engine.dispose()

    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert result["status"] == "accepted"
    assert close_secret not in response.text

    log_text = log_output.getvalue()
    assert {"event": "db_session_cleanup_close_failed"} in [json.loads(line) for line in log_text.splitlines()]
    assert close_secret not in log_text


async def test_ingest_close_failure_invalidates_connection_so_pool_recovers_without_engine_dispose(
    client, auth_headers, monkeypatch,
):
    """Codex's fourth-round review: a close()-only failure (no preceding rollback
    failure) was previously logged and swallowed with no attempt to discard the
    connection at all -- exactly the condition this session's own reproduction of the
    pre-guard code proved can leave a connection "idle in transaction" holding real
    Postgres locks indefinitely, silently corrupting the pool for later, unrelated
    requests. `get_db()` now attempts the same best-effort `invalidate()` when
    `close()` itself fails, independent of whether rollback ran or failed.

    This test verifies the OPERATIONAL guarantee, deliberately kept separate from the
    sanitization tests above (response/log secrecy): a SECOND, wholly ordinary request
    through the same real `get_db` dependency succeeds immediately afterward, with no
    `engine.dispose()` call in between rescuing it. The `dispose()` calls in the other
    real-`get_db` tests in this file are a test-hygiene-only safety net against
    inter-test pollution (see their own comments); this test proves the second request
    works on its own, without leaning on that safety net at all.

    SEC (Codex PR #49 review, ROUND 8): this scenario's request has already
    `await db.commit()`-ed its accepted record before `get_db()`'s cleanup ever runs,
    so `session.in_transaction()` is already `False` by the time the injected `close()`
    failure hits -- there is no live connection here for the fix to discard (confirmed
    empirically: `get_db`'s connection-snapshot helper correctly captures nothing in
    this exact scenario). This test therefore no longer asserts that any connection was
    invalidated -- see
    `tests/integration/test_db_session_cleanup.py` for that proof, using a genuinely
    live, uncommitted transaction and direct pool-level assertions, exactly matching
    Codex's own named example (a read endpoint that returns without committing)."""
    import app.db.session as session_module
    from app.db.session import get_db as real_get_db
    from app.main import app as fastapi_app

    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(
        f"/api/v1/collectors/{collector['id']}/capabilities",
        json={"protocol_codes": ["icmp"]}, headers=headers,
    )
    integration = await create_integration(client, headers, integration_type="icmp")
    await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments",
        json={"integration_id": integration["id"]}, headers=headers,
    )

    record = {
        "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
        "external_identifier": "10.0.0.97", "occurred_at": "2026-01-01T00:00:00Z",
        "raw_attributes": {"reachable": True},
    }
    raw_body = json.dumps({"batch_id": uuid.uuid4().hex, "records": [record]}).encode()
    signed = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body)

    original_close = AsyncSession.close

    async def fail_close_once(self, *args, **kwargs):
        # Only THIS request's own close() call fails -- restored immediately after, so
        # the second request's cleanup (and this test's own eventual teardown) behaves
        # normally, modeling a transient close failure rather than a permanently dead
        # driver.
        monkeypatch.setattr(AsyncSession, "close", original_close)
        raise RuntimeError("close failed: synthetic-session-close-SEC07H-keep-private")

    monkeypatch.setattr(AsyncSession, "close", fail_close_once)

    original_override = fastapi_app.dependency_overrides.get(real_get_db)
    fastapi_app.dependency_overrides.pop(real_get_db, None)
    try:
        response = await client.post(
            f"/api/v1/collectors/{collector['id']}/ingest",
            content=raw_body, headers={**signed, "Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["results"][0]["status"] == "accepted"

        # The critical operational assertion: a second, wholly ordinary request
        # through the SAME real get_db dependency (same engine, same pool) succeeds
        # immediately -- no engine.dispose() has happened yet. If the first request's
        # broken connection had been silently returned to the pool instead of
        # invalidated, this would hang or fail exactly like this session's own
        # pre-fix reproduction (a connection stuck "idle in transaction" holding
        # locks the next checkout needs).
        record2 = {
            "dedup_key": uuid.uuid4().hex, "integration_id": integration["id"],
            "external_identifier": "10.0.0.98", "occurred_at": "2026-01-01T00:00:00Z",
            "raw_attributes": {"reachable": True},
        }
        raw_body2 = json.dumps({"batch_id": uuid.uuid4().hex, "records": [record2]}).encode()
        signed2 = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw_body2)
        response2 = await client.post(
            f"/api/v1/collectors/{collector['id']}/ingest",
            content=raw_body2, headers={**signed2, "Content-Type": "application/json"},
        )
        assert response2.status_code == 200, response2.text
        assert response2.json()["results"][0]["status"] == "accepted"
    finally:
        if original_override is not None:
            fastapi_app.dependency_overrides[real_get_db] = original_override
        # Test-hygiene-only, run AFTER the operational assertion above already proved
        # the fix works without it.
        await session_module.engine.dispose()


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
