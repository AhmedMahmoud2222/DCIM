"""PostgreSQL parity: reading_provenance_status() vs reading_provenance_expression()."""
from datetime import UTC, datetime
import uuid

import pytest
from sqlalchemy import select

from app.api.v1.telemetry import reading_provenance_expression, reading_provenance_status
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading


# Independent hard-coded expected classification for all 20 combinations.
# (registry_version, revision_present, contract_evidence) -> expected status
EXPECTED = {
    (None, False, None): "unverified_legacy",
    (None, False, "pinned"): "unverified_legacy",
    (None, False, "inferred_single_revision"): "unverified_legacy",
    (None, False, "operator_resolved"): "unverified_legacy",
    (None, False, "unrecognized"): "unverified_legacy",
    ("1", False, None): "unverified_registry",
    ("1", False, "pinned"): "unverified_registry",
    ("1", False, "inferred_single_revision"): "unverified_registry",
    ("1", False, "operator_resolved"): "unverified_registry",
    ("1", False, "unrecognized"): "unverified_registry",
    (None, True, None): "unverified_legacy",
    (None, True, "pinned"): "unverified_legacy",
    (None, True, "inferred_single_revision"): "unverified_legacy",
    (None, True, "operator_resolved"): "unverified_legacy",
    (None, True, "unrecognized"): "unverified_legacy",
    ("1", True, "pinned"): "pinned",
    ("1", True, "inferred_single_revision"): "inferred",
    ("1", True, "operator_resolved"): "operator_resolved",
    ("1", True, None): "unverified_registry",
    ("1", True, "unrecognized"): "unverified_registry",
}


@pytest.mark.asyncio
async def test_provenance_python_sql_parity_postgres(db_session):
    now = datetime(2030, 7, 8, 12, tzinfo=UTC)
    collector = Collector(id=uuid.uuid4(), name="G7", collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=now)
    integration = Integration(id=uuid.uuid4(), name="G7", integration_type="snmp", target_host="192.0.2.7",
                              config={}, poll_interval_seconds=300)
    db_session.add_all([collector, integration])
    await db_session.flush()
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="temp",
                                       canonical_metric="temperature_c", unit="degC", scale=1, registry_version="1")
    db_session.add(mapping)
    await db_session.flush()
    revision = mapping.current_revision_id

    assert len(EXPECTED) == 20

    for (registry_version, has_revision, evidence), expected in EXPECTED.items():
        mapping_revision_id = revision if has_revision else None
        contract_evidence = evidence if (has_revision and evidence in ("pinned", "inferred_single_revision", "operator_resolved")) else None
        row = TelemetryReading(
            id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id,
            mapping_id=mapping.id, external_identifier="sensor", series_key="g7", dedup_key=uuid.uuid4().hex,
            metric="temperature_c", unit="degC", value=1, raw_value=1, raw_unit="degC",
            raw_value_text="1", source_scale=1, registry_version=registry_version,
            mapping_revision_id=mapping_revision_id, contract_evidence=contract_evidence,
            occurred_at=now, received_at=now, attributes={},
        )
        db_session.add(row)
        await db_session.flush()
        sql_val = await db_session.scalar(select(reading_provenance_expression()).where(TelemetryReading.id == row.id))
        py_val = reading_provenance_status(row)
        assert sql_val == expected, (row.id, sql_val, expected)
        assert py_val == expected, (row.id, py_val, expected)
        await db_session.delete(row)
        await db_session.flush()
