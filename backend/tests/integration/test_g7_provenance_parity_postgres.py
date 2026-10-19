"""PostgreSQL parity: reading_provenance_status() vs reading_provenance_expression()."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.api.v1.telemetry import reading_provenance_expression, reading_provenance_status
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import IntegrationMetricMapping, TelemetryReading


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
    cases = [
        (None, None, None),
        (revision, "pinned", "1"),
        (revision, "inferred_single_revision", "1"),
        (revision, "operator_resolved", "1"),
        (revision, None, "1"),
        (None, "pinned", "1"),
        (revision, "pinned", None),
    ]
    for mapping_revision_id, evidence, registry_version in cases:
        row = TelemetryReading(
            id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id,
            mapping_id=mapping.id, external_identifier="sensor", series_key="g7", dedup_key=uuid.uuid4().hex,
            metric="temperature_c", unit="degC", value=1, raw_value=1, raw_unit="degC",
            raw_value_text="1", source_scale=1, registry_version=registry_version,
            mapping_revision_id=mapping_revision_id, contract_evidence=evidence,
            occurred_at=now, received_at=now, attributes={},
        )
        db_session.add(row)
    await db_session.flush()
    rows = (await db_session.execute(select(TelemetryReading))).scalars().all()
    for row in rows:
        sql_val = await db_session.scalar(select(reading_provenance_expression()).where(TelemetryReading.id == row.id))
        assert sql_val == reading_provenance_status(row), (row.id, sql_val, reading_provenance_status(row))
