"""Collector telemetry endpoint: non-finite and out-of-range numbers are rejected per record.

Runs against real PostgreSQL. Persistence is NUMERIC(18, 8) (|x| <= 9999999999.99999999);
the ingestion boundary must reject out-of-range input itself instead of letting a database
overflow surface as an HTTP 500, and one bad reading must not disturb valid batch peers."""

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.application.telemetry_service import ingest_reading
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading
from tests.api._phase8_helpers import create_integration, register_collector, sign_request

NOW = datetime.now(UTC).isoformat()


async def _setup(client, auth_headers, db_session, *, metric="temperature_c", unit="degC", scale=1, legacy=False):
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
        canonical_metric=metric, unit=unit, scale=scale,
    )
    db_session.add(mapping)
    await db_session.commit()
    if legacy:
        await db_session.execute(
            text("UPDATE integration_metric_mapping SET registry_version = NULL WHERE id = :id"), {"id": mapping.id}
        )
        await db_session.commit()
    return collector, integration


def _record(integration, dedup_key, value_literal):
    # Raw JSON text: Python's json module emits NaN/Infinity literals and 1e400 verbatim.
    return (
        f'{{"dedup_key": "{dedup_key}", "integration_id": "{integration["id"]}", "external_identifier": "ext", '
        f'"source_identifier": "sensor", "occurred_at": "{NOW}", "value": {value_literal}}}'
    )


async def _post(client, collector, integration, specs):
    raw = ('{"records": [' + ", ".join(_record(integration, key, literal) for key, literal in specs) + "]}").encode()
    headers = sign_request(secret=collector["secret"], collector_id=uuid.UUID(collector["id"]), raw_body=raw)
    headers["Content-Type"] = "application/json"
    return await client.post(f"/api/v1/collectors/{collector['id']}/telemetry", content=raw, headers=headers)


async def _persisted(db_session):
    rows = (await db_session.execute(select(TelemetryReading).order_by(TelemetryReading.dedup_key))).scalars().all()
    return {row.dedup_key: row for row in rows}


# --- invalid raw source values -----------------------------------------------------------

@pytest.mark.parametrize("literal", [
    "NaN", "Infinity", "-Infinity",
    "1e300", "-1e300", "1e400", "-1e400",  # 1e400 parses to +/-inf
    "10000000000.0", "-10000000000.0",  # first integer above the NUMERIC(18, 8) range
])
async def test_invalid_raw_value_rejected_per_record_and_peers_persist(client, auth_headers, db_session, literal):
    collector, integration = await _setup(client, auth_headers, db_session)
    specs = [("a-good", "20.5"), ("b-bad", literal), ("c-good", "21.5")]
    resp = await _post(client, collector, integration, specs)
    assert resp.status_code == 200, resp.text
    assert [(r["dedup_key"], r["status"], r["error"]) for r in resp.json()["results"]] == [
        ("a-good", "accepted", None), ("b-bad", "rejected", "INVALID_TELEMETRY_VALUE"), ("c-good", "accepted", None),
    ]
    persisted = await _persisted(db_session)
    assert sorted(persisted) == ["a-good", "c-good"]
    assert persisted["a-good"].value == Decimal("20.5")
    assert persisted["c-good"].value == Decimal("21.5")
    # Retrying the invalid record is rejected again; it never degrades into "duplicate".
    retry = await _post(client, collector, integration, specs)
    assert retry.status_code == 200
    assert [r["status"] for r in retry.json()["results"]] == ["duplicate", "rejected", "duplicate"]
    assert sorted(await _persisted(db_session)) == ["a-good", "c-good"]


# --- scale and canonical conversion -------------------------------------------------------

@pytest.mark.parametrize("legacy", [False, True])
async def test_scale_multiplication_overflow_rejected(client, auth_headers, db_session, legacy):
    # 1e8 is itself storable; times scale 1000 it is 1e11, beyond the range.
    collector, integration = await _setup(client, auth_headers, db_session, scale=1000, legacy=legacy)
    resp = await _post(client, collector, integration, [("ok", "5.0"), ("scaled-over", "100000000.0"), ("ok2", "6.0")])
    assert resp.status_code == 200, resp.text
    assert [(r["dedup_key"], r["status"], r["error"]) for r in resp.json()["results"]] == [
        ("ok", "accepted", None), ("scaled-over", "rejected", "INVALID_TELEMETRY_VALUE"), ("ok2", "accepted", None),
    ]
    persisted = await _persisted(db_session)
    assert sorted(persisted) == ["ok", "ok2"]
    assert persisted["ok"].value == Decimal("5000")


async def test_canonical_conversion_overflow_rejected(client, auth_headers, db_session):
    # Raw and scaled value are storable, but Kelvin -> degC subtracts 273.15 and leaves the range.
    collector, integration = await _setup(client, auth_headers, db_session, unit="K")
    resp = await _post(client, collector, integration, [("ok", "300.0"), ("canon-over", "-9999999999.0"), ("ok2", "301.0")])
    assert resp.status_code == 200, resp.text
    assert [(r["dedup_key"], r["status"], r["error"]) for r in resp.json()["results"]] == [
        ("ok", "accepted", None), ("canon-over", "rejected", "INVALID_TELEMETRY_VALUE"), ("ok2", "accepted", None),
    ]
    persisted = await _persisted(db_session)
    assert sorted(persisted) == ["ok", "ok2"]
    assert persisted["ok"].value == Decimal("26.85")


async def test_canonical_ratio_scaling_overflow_rejected(client, auth_headers, db_session):
    # Ratio "1" -> "%" multiplies by 100; 1e9 is storable raw, 1e11 canonical is not.
    collector, integration = await _setup(client, auth_headers, db_session, metric="load_percent", unit="1")
    resp = await _post(client, collector, integration, [("ok", "0.5"), ("canon-over", "1000000000.0")])
    assert [(r["status"], r["error"]) for r in resp.json()["results"]] == [
        ("accepted", None), ("rejected", "INVALID_TELEMETRY_VALUE"),
    ]
    assert sorted(await _persisted(db_session)) == ["ok"]


# --- boundary values that must still be accepted ------------------------------------------

async def test_values_at_storable_boundary_are_accepted_and_round_trip(client, auth_headers, db_session):
    collector, integration = await _setup(client, auth_headers, db_session, legacy=True)
    # 9999999999.999998 is the largest double below the bound that prints exactly.
    resp = await _post(client, collector, integration, [("max", "9999999999.999998"), ("min", "-9999999999.999998")])
    assert [r["status"] for r in resp.json()["results"]] == ["accepted", "accepted"]
    persisted = await _persisted(db_session)
    assert persisted["max"].value == Decimal("9999999999.99999800")
    assert persisted["min"].value == Decimal("-9999999999.99999800")


async def test_exact_maximum_decimal_persists(db_session):
    now = datetime.now(UTC)
    collector = Collector(id=uuid.uuid4(), name=uuid.uuid4().hex, collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=now)
    integration = Integration(id=uuid.uuid4(), name=uuid.uuid4().hex, integration_type="snmp", target_host="192.0.2.1",
                              config={}, poll_interval_seconds=60)
    db_session.add_all([collector, integration])
    await db_session.flush()
    db_session.add(IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="s",
                                            canonical_metric="power_kw", unit="kW", scale=1))
    await db_session.flush()
    for key, value in [("max", Decimal("9999999999.99999999")), ("min", Decimal("-9999999999.99999999"))]:
        result = await ingest_reading(db_session, collector_id=collector.id, integration_id=integration.id, dedup_key=key,
                                      external_identifier="e", source_identifier="s", occurred_at=now, value=value)
        assert not result.duplicate
        reading = await db_session.get(TelemetryReading, result.reading_id)
        await db_session.refresh(reading)
        assert reading.value == value
        assert reading.raw_value == value


# --- request-level metadata -----------------------------------------------------------------

def test_literals_used_by_the_tests_are_what_the_collector_would_send():
    # json.dumps (what a Python edge client uses) emits these exact non-finite literals.
    assert json.dumps([float("nan"), float("inf"), float("-inf")]) == "[NaN, Infinity, -Infinity]"
