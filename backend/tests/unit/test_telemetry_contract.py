from datetime import UTC, datetime


def test_telemetry_record_preserves_occurrence_and_uses_explicit_dedup_key():
    from app.api.v1.telemetry import TelemetryRecordIn

    occurred_at = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
    record = TelemetryRecordIn(
        dedup_key="edge-1:42",
        integration_id="00000000-0000-0000-0000-000000000001",
        external_identifier="sensor-a",
        source_identifier="1.3.6.1.4.1.999.1",
        occurred_at=occurred_at,
        value=21.5,
    )

    assert record.dedup_key == "edge-1:42"
    assert record.occurred_at == occurred_at


def test_versioned_series_namespace_cannot_collide_with_unrestricted_legacy_unit_text():
    import uuid

    from app.domain.telemetry.models import telemetry_series_key

    integration_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    legacy = telemetry_series_key(integration_id, None, "sensor", "temperature_c", "degC:1")
    canonical = telemetry_series_key(integration_id, None, "sensor", "temperature_c", "degC", "1")
    assert legacy == f"{integration_id}:unmanaged:sensor:temperature_c:degC:1"
    assert canonical != legacy
    assert canonical.startswith(f"registry:1:{integration_id}:")
    assert telemetry_series_key(integration_id, None, "sensor", "temperature_c", "degC", "2") != canonical
    assert telemetry_series_key(integration_id, None, "another-sensor", "temperature_c", "degC", "1") != canonical
    assert telemetry_series_key(integration_id, uuid.uuid4(), "sensor", "temperature_c", "degC", "1") != canonical
