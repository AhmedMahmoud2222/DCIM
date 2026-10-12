"""Real HTTP/PostgreSQL provenance through raw history, filters and G4-R compaction."""
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.telemetry_retention import compact_eligible_raw
from app.domain.integration.models import Collector, Integration
from app.domain.telemetry.models import (
    IntegrationMetricMapping,
    IntegrationMetricMappingRevision,
    TelemetryReading,
)


@pytest.mark.asyncio
async def test_aggregate_history_exposes_lineage_after_compaction(client, auth_headers, db_session):
    now = datetime(2030, 7, 8, 12, tzinfo=UTC)
    collector = Collector(id=uuid.uuid4(), name="G7", collector_type="central", status="active",
                          secret_ciphertext="test", secret_rotated_at=now)
    integration = Integration(id=uuid.uuid4(), name="G7", integration_type="snmp", target_host="192.0.2.7",
                              config={}, poll_interval_seconds=300)
    mapping = IntegrationMetricMapping(id=uuid.uuid4(), integration_id=integration.id, source_identifier="temp",
                                       canonical_metric="temperature_c", unit="degC", scale=1, registry_version="1")
    db_session.add_all([collector, integration, mapping])
    await db_session.flush()
    revision = await db_session.get(IntegrationMetricMappingRevision, mapping.current_revision_id)
    assert revision is not None
    rows = []
    for index, evidence in enumerate((None, "pinned", "inferred_single_revision", "operator_resolved")):
        row = TelemetryReading(id=uuid.uuid4(), collector_id=collector.id, integration_id=integration.id,
            mapping_id=mapping.id, external_identifier="sensor", series_key="g7:same-series", dedup_key=uuid.uuid4().hex,
            metric="temperature_c", unit="degC", value=Decimal("25.12345678"), raw_value=Decimal("25.12345678"),
            raw_unit="degC", raw_value_text="25.1234567800", source_scale=1, registry_version="1",
            mapping_revision_id=revision.id if evidence else None, contract_evidence=evidence,
            occurred_at=now-timedelta(days=500, hours=4-index), received_at=now, attributes={})
        db_session.add(row)
        rows.append(row)
    await db_session.commit()
    headers = await auth_headers()
    params = dict(metric="temperature_c", integration_id=str(integration.id), start=(now-timedelta(days=1000)).isoformat(),
                  end=now.isoformat(), series_key="g7:same-series", registry_version="1")
    response = await client.get("/api/v1/telemetry/history", headers=headers, params=params)
    assert response.status_code == 200, response.text
    points = response.json()
    assert [p["provenance_status"] for p in points] == ["unverified_registry", "pinned", "inferred", "operator_resolved"]
    assert all(p["value_decimal"] == "25.12345678" and p["raw_value_text"] == "25.1234567800" for p in points)
    filtered = await client.get("/api/v1/telemetry/history", headers=headers,
        params={**params, "provenance_status": ["pinned", "operator_resolved"], "limit": 1})
    assert filtered.status_code == 200, filtered.text
    assert [p["provenance_status"] for p in filtered.json()] == ["pinned"]
    unknown = await client.get("/api/v1/telemetry/history", headers=headers, params={**params, "provenance_status": "certified"})
    assert unknown.status_code == 422
    await compact_eligible_raw(db_session, now=now)
    await db_session.commit()
    assert not (await db_session.execute(select(TelemetryReading).where(TelemetryReading.integration_id == integration.id))).scalars().all()
    after = await client.get("/api/v1/telemetry/history", headers=headers, params=params)
    assert after.status_code == 200, after.text
    daily = after.json()
    assert len(daily) == 1 and daily[0]["resolution"] == "daily" and daily[0]["sample_count"] == 4
    assert daily[0]["provenance_status"] is None and daily[0]["mapping_revision_id"] is None
    assert daily[0]["raw_value_text"] is None
    assert daily[0]["aggregate_evidence_class"] == "mixed"
    assert daily[0]["lineage_coverage"] == "complete"
    assert daily[0]["lineage_sample_count"] == 4
    assert daily[0]["raw_evidence_status"] == "expired"
    assert daily[0]["first_compacted_at"] is not None
    assert daily[0]["raw_retention_days_at_compaction"] is not None
    filtered_daily = await client.get("/api/v1/telemetry/history", headers=headers, params={**params, "provenance_status": "pinned"})
    assert filtered_daily.status_code == 200 and filtered_daily.json() == []
