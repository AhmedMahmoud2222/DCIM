"""PostgreSQL parity: reading_provenance_status() vs reading_provenance_expression()."""
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.api.v1.telemetry import reading_provenance_expression, reading_provenance_status
from app.domain.telemetry.models import TelemetryReading


@pytest.mark.asyncio
async def test_provenance_python_sql_parity_postgres(db_session):
    now = datetime(2030, 7, 8, 12, tzinfo=UTC)
    cases = [
        (None, None, None),
        (uuid.uuid4(), "pinned", "1"),
        (uuid.uuid4(), "inferred_single_revision", "1"),
        (uuid.uuid4(), "operator_resolved", "1"),
        (uuid.uuid4(), "legacy_unmapped", "1"),
        (uuid.uuid4(), None, "1"),
        (None, "pinned", "1"),
        (uuid.uuid4(), "pinned", None),
    ]
    for mapping_revision_id, evidence, registry_version in cases:
        row = TelemetryReading(
            id=uuid.uuid4(), collector_id=None, integration_id=None, mapping_id=None,
            external_identifier="sensor", series_key="g7", dedup_key=uuid.uuid4().hex,
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
        assert sql_val == reading_provenance_status(row), (row, sql_val)
