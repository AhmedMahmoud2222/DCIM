"""Conversion/storage/domain ordering used by the real ingestion boundary."""

from decimal import Context, Decimal, localcontext

import pytest

from app.application.telemetry_service import canonical_storage_value
from app.domain.telemetry.metric_semantics import ValueOutOfRange
from app.domain.telemetry.numeric import InvalidTelemetryValue


def test_canonical_overflow_precedes_physical_domain_rejection():
    with pytest.raises(InvalidTelemetryValue):
        canonical_storage_value("humidity_percent", Decimal("9999999999"), "1", Decimal(1), "2")
    with pytest.raises(ValueOutOfRange):
        canonical_storage_value("humidity_percent", Decimal("1.01"), "1", Decimal(1), "2")


def test_scale_applies_once_and_conversion_keeps_snapshot_context():
    with localcontext(Context(prec=6)):
        assert canonical_storage_value("humidity_percent", Decimal(".25"), "1", Decimal(2), "2") == (
            Decimal("50.00000000"), "%",
        )
        assert canonical_storage_value("airflow_m3_s", Decimal("1234567"), "CFM", Decimal(1), "2") == (
            Decimal("582.65073911"), "m3/s",
        )


def test_v1_retains_representable_outside_v2_domain():
    assert canonical_storage_value("humidity_percent", Decimal(150), "%", Decimal(1), "1") == (
        Decimal("150.00000000"), "%",
    )
