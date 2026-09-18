"""Fast contract checks for defaults and safety boundaries; DB scenarios require Postgres."""

from edge_collector.scheduler import DEFAULT_POLL_INTERVAL_SECONDS, POLL_INTERVAL_PRESETS

from app.application.telemetry_retention import RAW_RETENTION_DAYS
from app.core.config import Settings
from app.domain.telemetry.models import DailyTelemetryAggregate


def test_monitoring_defaults_and_polling_presets():
    assert DEFAULT_POLL_INTERVAL_SECONDS == 300
    assert POLL_INTERVAL_PRESETS == {60, 180, 300, 600, 900, 1800}
    assert RAW_RETENTION_DAYS == 365
    assert Settings.model_fields["telemetry_daily_retention_days"].default is None
    assert Settings.model_fields["alarm_history_retention_days"].default is None


def test_daily_aggregate_preserves_daily_excursion_information():
    names = {column.name for column in DailyTelemetryAggregate.__table__.columns}
    assert {"average_value", "minimum_value", "maximum_value", "sample_count", "day", "metric", "unit"} <= names
    assert any(index.name == "uq_daily_telemetry_sensor_metric_day" for index in DailyTelemetryAggregate.__table__.constraints)
