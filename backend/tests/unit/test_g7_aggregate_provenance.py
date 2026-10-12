"""The aggregate display never promotes incomplete lineage to complete evidence."""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.api.v1.telemetry import _history_daily
from app.domain.telemetry.models import DailyTelemetryAggregate


@pytest.fixture
def _clean_tables():
    yield


@pytest.mark.parametrize("count,coverage,classification,compacted,expiry", [
    (None, "none", None, None, "unknown"),
    (2, "partial", "mixed", datetime(2026, 10, 11, tzinfo=UTC), "expired"),
    (4, "complete", "pinned", datetime(2026, 10, 11, tzinfo=UTC), "expired"),
    (4, "complete", "unverified", datetime(2026, 10, 11, tzinfo=UTC), "expired"),
])
def test_daily_summary_preserves_recorded_evidence(count, coverage, classification, compacted, expiry):
    row = DailyTelemetryAggregate(id=uuid.uuid4(), integration_id=uuid.uuid4(), external_identifier="sensor",
        series_key="same-series", metric="temperature_c", unit="degC", registry_version="1", day=date(2026, 1, 1),
        average_value=Decimal("25.12345678"), minimum_value=Decimal("20"), maximum_value=Decimal("30"), sample_count=4,
        lineage_sample_count=count, evidence_class=classification, first_compacted_at=compacted,
        raw_retention_days_at_compaction=365 if compacted else None)
    out = _history_daily(row)
    assert out.provenance_status is None and out.mapping_revision_id is None
    assert out.aggregate_evidence_class == classification and out.lineage_coverage == coverage
    assert out.lineage_sample_count == count and out.raw_evidence_status == expiry
    assert out.first_compacted_at == compacted
    assert out.raw_value is None and out.raw_value_text is None
    assert out.value_decimal == "25.12345678"


def test_inconsistent_lineage_counts_fail_instead_of_claiming_provenance():
    row = DailyTelemetryAggregate(id=uuid.uuid4(), integration_id=uuid.uuid4(), external_identifier="sensor",
        series_key="series", metric="temperature_c", unit="degC", registry_version="1", day=date(2026, 1, 1),
        average_value=Decimal("25"), minimum_value=Decimal("20"), maximum_value=Decimal("30"), sample_count=4,
        lineage_sample_count=5)
    with pytest.raises(ValueError, match="contradict"):
        _history_daily(row)
