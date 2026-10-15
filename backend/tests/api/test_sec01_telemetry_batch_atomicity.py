"""Issue #38 / SEC-01: regression coverage for the owner-accepted batch-atomicity contract of
`ingest_collector_telemetry()` (app/api/v1/telemetry.py).

The owner was asked to decide whole-batch-rollback vs. partial-commit semantics for *unexpected*
(not domain-specific) exceptions during telemetry batch ingestion and ruled to PRESERVE the existing
whole-batch rollback/retry contract -- no partial-commit semantic change merely to match the old
suggested savepoint remedy. Per-record `db.begin_nested()` savepoints already exist (added incidentally
by #128, not for SEC-01) and correctly isolate per-record *known* domain rejections (see
tests/api/test_telemetry_invalid_numeric.py and test_telemetry_mapping_revisions_api.py for that
coverage) without raising. There is exactly one `await db.commit()`, after the whole `for record in
body.records` loop: a genuinely unexpected exception (not one of the caught domain types) propagates
out of the function, the commit is never reached, and the session's uncommitted work for the *entire
batch* -- including records before the failing one that already passed their own nested-savepoint
checks -- is rolled back.

Per tests/api/test_phase8_final_closure_validation.py's own established precedent for this exact
situation: a genuinely unexpected exception escapes `register_exception_handlers`' catch-all handler
uncaught by any further handler at this layer -- Starlette's `ServerErrorMiddleware` always re-raises
after building its 500 response (see its own `__call__`: "We always continue to raise the exception...
allows test clients to optionally raise the error within the test case"), so `ASGITransport` surfaces
it to this test as a raised exception, not a clean 500 response object, exactly as a real ASGI server
would log an unhandled exception and terminate the response ungracefully. And since this test's
`db_session` dependency override (tests/conftest.py: a plain `yield db_session`) does not itself
replicate production's own `get_db`'s `except Exception: await session.rollback(); raise`, this module
does that rollback explicitly before inspecting durable state, exactly as that precedent does."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.v1 import telemetry as telemetry_module
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading
from tests.api._phase8_helpers import create_integration, register_collector, sign_request


async def _setup(client, auth_headers, db_session):
    headers = await auth_headers("DCIM Manager")
    collector = await register_collector(client, headers)
    await client.post(f"/api/v1/collectors/{collector['id']}/capabilities", json={"protocol_codes": ["icmp"]}, headers=headers)
    integration = await create_integration(client, headers, integration_type="icmp")
    resp = await client.post(
        f"/api/v1/collectors/{collector['id']}/assignments", json={"integration_id": integration["id"]}, headers=headers
    )
    assert resp.status_code == 201, resp.text
    mapping = IntegrationMetricMapping(
        id=uuid.uuid4(), integration_id=uuid.UUID(integration["id"]), source_identifier="sensor",
        canonical_metric="temperature_c", unit="degC", scale=1, registry_version="1",
    )
    db_session.add(mapping)
    await db_session.commit()
    return collector, integration


def _records(integration, keys):
    base = datetime.now(UTC)
    return [
        {
            "dedup_key": key, "integration_id": integration["id"], "external_identifier": "ext",
            "source_identifier": "sensor", "occurred_at": (base + timedelta(seconds=i)).isoformat(), "value": 20.0 + i,
        }
        for i, key in enumerate(keys)
    ]


async def _post(client, collector, records):
    raw = json.dumps({"records": records}).encode()
    headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
    headers["Content-Type"] = "application/json"
    return await client.post(f"/api/v1/collectors/{collector['id']}/telemetry", content=raw, headers=headers)


async def _persisted(db_session):
    rows = (await db_session.execute(select(TelemetryReading))).scalars().all()
    return {row.dedup_key: row for row in rows}


def _raise_for(trigger_key, original):
    async def _wrapped(db, **kwargs):
        if kwargs["dedup_key"] == trigger_key:
            # A genuinely unexpected failure -- not one of ingest_collector_telemetry()'s caught
            # domain exceptions (MetricMappingNotFound, AmbiguousMappingContract, ...).
            raise RuntimeError("synthetic-unexpected-failure-sec01-keep-private")
        return await original(db, **kwargs)

    return _wrapped


async def test_unexpected_exception_mid_batch_aborts_the_whole_batch_with_no_partial_persistence(
    client, auth_headers, db_session, monkeypatch,
):
    collector, integration = await _setup(client, auth_headers, db_session)
    keys = ["r1", "r2", "bad", "r4", "r5"]  # the unexpected failure hits the middle record, not the first or last
    records = _records(integration, keys)

    original = telemetry_module.ingest_reading
    monkeypatch.setattr(telemetry_module, "ingest_reading", _raise_for("bad", original))

    # No silent loss: the client-visible outcome is unambiguous -- a raised exception (never a
    # `TelemetryBatchOut` implying any success for this batch).
    with pytest.raises(RuntimeError, match="synthetic-unexpected-failure-sec01-keep-private"):
        await _post(client, collector, records)
    await db_session.rollback()

    persisted = await _persisted(db_session)
    assert persisted == {}, "no record from the aborted batch -- including peers that individually passed -- may be persisted"


async def test_retry_after_an_aborted_batch_is_safe_and_exactly_once_per_dedup_key(
    client, auth_headers, db_session, monkeypatch,
):
    collector, integration = await _setup(client, auth_headers, db_session)
    keys = ["s1", "s2", "bad", "s4", "s5"]
    records = _records(integration, keys)

    original = telemetry_module.ingest_reading
    monkeypatch.setattr(telemetry_module, "ingest_reading", _raise_for("bad", original))
    with pytest.raises(RuntimeError, match="synthetic-unexpected-failure-sec01-keep-private"):
        await _post(client, collector, records)
    await db_session.rollback()
    assert (await _persisted(db_session)) == {}

    # Retry the identical batch (same dedup_keys) once the unexpected condition is gone -- exactly
    # what a real collector does with an unacknowledged batch.
    monkeypatch.setattr(telemetry_module, "ingest_reading", original)
    retried = await _post(client, collector, records)
    assert retried.status_code == 200, retried.text
    assert [r["status"] for r in retried.json()["results"]] == ["accepted"] * len(keys)

    persisted = await _persisted(db_session)
    assert sorted(persisted) == sorted(keys)
    assert len(persisted) == len(keys)  # exactly one reading per dedup_key -- no double counting from the aborted attempt


async def test_known_rejection_in_the_same_batch_as_valid_records_does_not_abort_the_batch(client, auth_headers, db_session):
    # Distinct from tests/api/test_telemetry_invalid_numeric.py's InvalidTelemetryValue coverage of the
    # same principle: this exercises MetricMappingNotFound -> UNKNOWN_METRIC_MAPPING specifically, which
    # had no direct regression test.
    collector, integration = await _setup(client, auth_headers, db_session)
    now = datetime.now(UTC)
    records = [
        {"dedup_key": "ok-1", "integration_id": integration["id"], "external_identifier": "ext",
         "source_identifier": "sensor", "occurred_at": now.isoformat(), "value": 20.0},
        {"dedup_key": "unmapped", "integration_id": integration["id"], "external_identifier": "ext",
         "source_identifier": "no-such-mapping", "occurred_at": (now + timedelta(seconds=1)).isoformat(), "value": 1.0},
        {"dedup_key": "ok-2", "integration_id": integration["id"], "external_identifier": "ext",
         "source_identifier": "sensor", "occurred_at": (now + timedelta(seconds=2)).isoformat(), "value": 21.0},
    ]
    resp = await _post(client, collector, records)
    assert resp.status_code == 200, resp.text
    assert [(r["dedup_key"], r["status"], r["error"]) for r in resp.json()["results"]] == [
        ("ok-1", "accepted", None), ("unmapped", "rejected", "UNKNOWN_METRIC_MAPPING"), ("ok-2", "accepted", None),
    ]
    persisted = await _persisted(db_session)
    assert sorted(persisted) == ["ok-1", "ok-2"]
