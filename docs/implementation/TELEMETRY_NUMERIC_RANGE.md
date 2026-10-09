# Telemetry numeric validation boundary

Applies to `POST /api/v1/collectors/{collector_id}/telemetry` (`ingest_collector_telemetry()` →
`ingest_reading()`). It does not concern the separate `/collectors/{collector_id}/ingest` discovery path
used by the packaged Edge Collector.

## Permitted range

`telemetry_reading.value`, `raw_value` and `source_scale` are PostgreSQL `NUMERIC(18, 8)`: 18 significant
digits, 8 fractional, so 10 integer digits.

| | |
|---|---|
| Maximum | `9999999999.99999999` |
| Minimum | `-9999999999.99999999` |
| Rounding | to 8 decimals, half away from zero (PostgreSQL behaviour). A value that rounds up to `±10000000000` is out of range. |
| Never storable | `NaN`, `+Infinity`, `-Infinity` |

Constants and the check live in `app/domain/telemetry/numeric.py` (`ensure_storable`).

## What is validated, in order

1. Raw source value (`value` from the record).
2. Source value × mapping scale.
3. Canonical value after unit conversion (registry-versioned mappings). Legacy mappings
   (`registry_version IS NULL`) store the scaled value, which is check 2.

A failure raises `InvalidTelemetryValue`. PostgreSQL overflow is not the validation mechanism.

## Result

The record is rejected inside its own savepoint with `status: "rejected"`, `error: "INVALID_TELEMETRY_VALUE"`.
Nothing is persisted for it, valid peers in the batch persist, and the HTTP status stays 200. A retry of the
same record is rejected again; it never becomes `duplicate` because no row was written.

The error text never echoes the submitted number.

## Unchanged

Duplicate, `UNKNOWN_METRIC_MAPPING`, `INCOMPATIBLE_TELEMETRY_UNITS`, `NOT_ASSIGNED`, alarm evaluation,
registry handling, late-reading merge and retention behave as before.
