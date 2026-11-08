import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.api.v1.telemetry import _history_raw
from app.domain.telemetry.models import TelemetryReading


@pytest.fixture
def _clean_tables():
    """Serialization-only tests do not use a database or mutate tables."""
    yield


@pytest.mark.parametrize("version,revision,evidence,status", [
    (None, None, None, "unverified_legacy"),
    ("1", None, None, "unverified_registry"),
    ("1", uuid.uuid4(), "pinned", "pinned"),
    ("1", uuid.uuid4(), "inferred_single_revision", "inferred"),
    ("1", uuid.uuid4(), "operator_resolved", "operator_resolved"),
    ("1", uuid.uuid4(), None, "unverified_registry"),
    ("1", None, "pinned", "unverified_registry"),
    (None, uuid.uuid4(), "pinned", "pinned"),
    ("1", None, "inferred_single_revision", "unverified_registry"),
    ("1", None, "operator_resolved", "unverified_registry"),
    ("1", uuid.uuid4(), "future_unknown", "unverified_registry"),
    (None, None, "operator_resolved", "unverified_legacy"),
    (None, uuid.uuid4(), "inferred_single_revision", "inferred"),
    (None, uuid.uuid4(), "operator_resolved", "operator_resolved"),
])
def test_history_status_prioritizes_recorded_revision_evidence(version, revision, evidence, status):
    now = datetime.now(UTC)
    row = TelemetryReading(id=uuid.uuid4(), integration_id=uuid.uuid4(),
        external_identifier="sensor", metric="temperature_c", unit="degC", value=Decimal("25.12345678"),
        raw_value=Decimal("25.12345678"), raw_unit="degC", raw_value_text="25.1234567800",
        source_scale=Decimal("1.00000000"), registry_version=version, mapping_revision_id=revision,
        contract_evidence=evidence, series_key="registry:source:temperature_c", occurred_at=now, received_at=now)
    out = _history_raw(row)
    assert out.provenance_status == status
    assert out.series_key == row.series_key
    assert out.mapping_revision_id == revision
    assert out.contract_evidence == evidence
    assert out.raw_value_text == "25.1234567800"
    assert out.value_decimal == "25.12345678"


@pytest.mark.asyncio
async def test_history_filters_are_applied_before_one_shared_bounded_statement():
    from sqlalchemy.dialects import postgresql

    from app.api.v1.telemetry import metric_history

    class EmptyResult:
        def all(self):
            return []

    class CaptureSession:
        statements = []
        async def execute(self, statement):
            self.statements.append(statement)
            return EmptyResult()

    db = CaptureSession()
    await metric_history(metric="temperature_c", start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC), integration_id=uuid.uuid4(),
        series_key="specific-series", registry_version="1", provenance_status=["pinned", "inferred"],
        limit=5, db=db)
    assert len(db.statements) == 1
    sql = str(db.statements[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "CASE WHEN" in sql
    assert "IN ('pinned', 'inferred')" in sql
    assert "telemetry_reading.series_key = 'specific-series'" in sql
    assert "daily_telemetry_aggregate.series_key = 'specific-series'" in sql
    assert "AND false" in sql
    assert sql.count("LIMIT 5") == 1


def test_unknown_provenance_query_is_422_before_database_execution():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1.telemetry import router

    app = FastAPI()
    app.include_router(router)
    route = next(route for route in router.routes if getattr(route, "path", None) == "/telemetry/history")
    for dependency in route.dependant.dependencies:
        app.dependency_overrides[dependency.call] = lambda: None
    with TestClient(app) as client:
        response = client.get("/telemetry/history", params={"metric": "temperature_c",
            "start": "2026-01-01T00:00:00Z", "end": "2026-01-02T00:00:00Z",
            "integration_id": str(uuid.uuid4()), "provenance_status": "certified"})
    assert response.status_code == 422


def test_daily_history_does_not_fabricate_reading_provenance():
    from datetime import date

    from app.api.v1.telemetry import _history_daily
    from app.domain.telemetry.models import DailyTelemetryAggregate

    row = DailyTelemetryAggregate(id=uuid.uuid4(), integration_id=uuid.uuid4(),
        external_identifier="sensor", series_key="registry:series", metric="temperature_c", unit="degC",
        registry_version="1", day=date(2026, 1, 1), average_value=Decimal("25.00000000"),
        minimum_value=Decimal("20.00000000"), maximum_value=Decimal("30.00000000"), sample_count=2)
    out = _history_daily(row)
    assert out.provenance_status is None
    assert out.mapping_revision_id is None
    assert out.contract_evidence is None
    assert out.raw_value_text is None
    assert out.series_key == row.series_key
    assert out.value_decimal == "25.00000000"


@pytest.mark.parametrize("evidence,status", [
    ("pinned", "pinned"), ("inferred_single_revision", "inferred"),
    ("operator_resolved", "operator_resolved"),
])
def test_sql_status_keeps_checked_legacy_revisions_contract_backed(evidence, status):
    from sqlalchemy import create_engine, literal, select
    from sqlalchemy.sql import visitors

    from app.api.v1.telemetry import reading_provenance_expression

    # Evaluate the actual SQL CASE in a scratch in-memory engine without contacting a
    # PostgreSQL service. Substitution preserves SQL NULL/CASE semantics.
    values = {"registry_version": None, "mapping_revision_id": str(uuid.uuid4()), "contract_evidence": evidence}
    expression = visitors.replacement_traverse(reading_provenance_expression(), {},
        lambda node: literal(values[node.name]) if getattr(node, "table", None) is TelemetryReading.__table__
        and getattr(node, "name", None) in values else None)
    engine = create_engine("sqlite://")
    with engine.connect() as connection:
        assert connection.scalar(select(expression)) == status
    engine.dispose()
