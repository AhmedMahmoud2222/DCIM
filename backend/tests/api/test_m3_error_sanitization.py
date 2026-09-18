"""Pre-MVP consolidation hardening: M3 -- collector per-record ACK errors must never
leak raw internal exception text (SQL, constraint-internal detail, stack traces,
filesystem paths). Reuses the SAME genuine-database-constraint-violation setup
`tests/api/test_phase8_correction_validation.py`'s hostile self-review test
established (two concurrent records racing on `discovered_device`'s own unique
constraint) as the one reliable way to force a REAL `IntegrityError`, rather than a
synthetic one, through this exact code path."""

import asyncio
import json
import uuid

from tests.api._phase8_helpers import create_integration, register_collector, sign_request


async def _assign(client, headers, integration_type="icmp"):
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": [integration_type]}, headers=headers)
    integration = await create_integration(client, headers, integration_type=integration_type)
    await client.post(f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers)
    return collector, integration


def _record(integration_id, external_id, dedup_key=None):
    return {
        "dedup_key": dedup_key or uuid.uuid4().hex, "integration_id": integration_id,
        "external_identifier": external_id, "occurred_at": "2026-01-01T00:00:00Z", "raw_attributes": {},
    }


async def test_not_assigned_rejection_uses_stable_error_code(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector, _integration = await _assign(client, headers)
    fake_integration_id = str(uuid.uuid4())
    payload = {"batch_id": uuid.uuid4().hex, "records": [_record(fake_integration_id, "30.0.0.1")]}
    raw = json.dumps(payload).encode()
    sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
    resp = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw, headers={**sig_headers, "Content-Type": "application/json"})
    assert resp.status_code == 200
    result = resp.json()["results"][0]
    assert result["status"] == "rejected"
    assert result["error_code"] == "NOT_ASSIGNED"
    assert "assigned" in result["error"].lower()
    assert "traceback" not in result["error"].lower()


async def test_idempotency_conflict_uses_stable_error_code(client, auth_headers):
    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    key = uuid.uuid4().hex
    payload1 = {"batch_id": uuid.uuid4().hex, "records": [_record(integration["id"], "30.0.0.2", key)]}
    raw1 = json.dumps(payload1).encode()
    sig1 = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw1)
    await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw1, headers={**sig1, "Content-Type": "application/json"})

    payload2 = {"batch_id": uuid.uuid4().hex, "records": [_record(integration["id"], "30.0.0.99", key)]}
    raw2 = json.dumps(payload2).encode()
    sig2 = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw2)
    resp2 = await client.post(f"/api/v1/collectors/{collector['id']}/ingest", content=raw2, headers={**sig2, "Content-Type": "application/json"})
    result = resp2.json()["results"][0]
    assert result["error_code"] == "IDEMPOTENCY_CONFLICT"


async def test_genuine_database_constraint_violation_does_not_leak_sql_to_collector(client, auth_headers, db_session):
    """The load-bearing test: force a REAL `asyncpg.exceptions.UniqueViolationError`
    inside the per-record SAVEPOINT (two genuinely concurrent records, same
    external_identifier, different dedup_keys -- the exact race
    `test_i4_concurrent_records_same_external_identifier_hits_real_db_constraint_inside_savepoint`
    already proved happens reliably) and confirm the LOSING record's response contains
    NEITHER the raw exception text NOR any SQL/constraint-name/internal detail -- only
    the stable `INTERNAL_PROCESSING_ERROR` code and a generic message."""
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.api.deps import get_db
    from app.main import app
    from tests.conftest import TEST_DATABASE_URL

    headers = await auth_headers("DCIM Manager")
    collector, integration = await _assign(client, headers)
    external_id = "30.0.0.3"
    key_a, key_b = uuid.uuid4().hex, uuid.uuid4().hex

    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True, pool_size=10)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    async def _override():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _override
    try:
        async def _send(dedup_key):
            payload = {"batch_id": uuid.uuid4().hex, "records": [_record(integration["id"], external_id, dedup_key)]}
            raw = json.dumps(payload).encode()
            sig_headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                return await ac.post(
                    f"/api/v1/collectors/{collector['id']}/ingest", content=raw,
                    headers={**sig_headers, "Content-Type": "application/json"},
                )

        r_a, r_b = await asyncio.gather(_send(key_a), _send(key_b))
    finally:
        app.dependency_overrides.pop(get_db, None)
        await engine.dispose()

    for resp in (r_a, r_b):
        assert resp.status_code == 200
        for result in resp.json()["results"]:
            if result["status"] != "rejected":
                continue
            assert result["error_code"] == "INTERNAL_PROCESSING_ERROR", result
            error_text = result["error"].lower()
            banned_substrings = [
                "integrityerror", "uniqueviolation", "asyncpg", "sqlalchemy", "select ", "insert into",
                "constraint", "duplicate key", "traceback", "/home/", "/app/", ".py", "psycopg",
            ]
            for banned in banned_substrings:
                assert banned not in error_text, f"leaked internal detail {banned!r} in error message: {result['error']!r}"
            assert result["error"] == "An internal error occurred while processing this record."
