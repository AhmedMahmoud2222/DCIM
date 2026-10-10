# Telemetry numeric validation boundary

Applies to `POST /api/v1/collectors/{collector_id}/telemetry` (`ingest_collector_telemetry()` →
`ingest_reading()`). It does not concern the separate `/collectors/{collector_id}/ingest` discovery path
used by the packaged Edge Collector.

> **Issue #128 / G3 supersedes the rounding and wire statements below where they differ.** See
> [Source numeric fidelity](#source-numeric-fidelity-issue-128--g3) at the end of this file.

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


## Source numeric fidelity (Issue #128 / G3)

### Wire
`POST /collectors/{id}/telemetry` parses the (already HMAC-authenticated and byte-bounded) body itself, so a numeral never
passes through a float. `value` (and the optional `source_scale` echo) may be a bare JSON number, a JSON integer or a
decimal string, and keeps every digit: `1234567890.12345678` and the documented maximum `9999999999.99999999` are accepted
exactly. Floats written by existing clients are unchanged: a client that already serialised a float sends the float's
shortest text, which is exactly what is stored.

| Input | Outcome |
|---|---|
| Not JSON, a boolean, `null`, an array/object, a string that is not a decimal numeral, a numeral over 64 characters | `422 Validation Error` for the whole request (nothing is stored) |
| `NaN`, `Infinity`, `-Infinity` (number or string), a value above `9999999999.99999999` in magnitude, an exponent beyond 10^+/-100 | that record only: `INVALID_TELEMETRY_VALUE`; valid peers persist |
| More than 8 decimal places | accepted and rounded (below) |

The same grammar applies to the mapping `scale` (`POST /telemetry/mappings`): a number or decimal string, at most 8 decimal
places, inside the storable range; anything else is a 422. Authorisation is decided before the body is read.

### Rounding
The service, not PostgreSQL, rounds the source value once to 8 decimals with **ROUND_HALF_EVEN** (the registry's rule).
PostgreSQL therefore never rounds a stored source value (previously half away from zero). The original value must itself be
inside the documented range: `9999999999.999999994` is rejected even though rounding would bring it in. Scaled and canonical
values keep their existing range checks (`INVALID_TELEMETRY_VALUE` on overflow), and a pre-registry mapping's scaled value
is rounded half-even once as well.

The canonical value is computed from the **stored** raw value, the pinned G1 scale and unit, so
`canonical == convert(raw_value * source_scale, raw_unit)` holds for every new reading and can be recomputed from persisted
data alone. This changes stored values only for inputs with more than 8 decimals (ties and near-ties); no existing reading
is rewritten.

*Acknowledged limitation (double rounding):* for such inputs the canonical value can differ by one unit in its 8th decimal
from converting the exact original numeral in one step (for example `68.0000000051` degF: `20.00000001` from the stored raw,
`20.00000000` from the exact original). The original numeral is kept, so the one-step value can always be recomputed.

### Evidence and responses
New readings store the exact numeral received in `telemetry_reading.raw_value_text` (`VARCHAR(64)`, e.g. `-0.0`, `1E+3`,
`1234567890.123456789`). `NULL` means "stored before G3": the original text is unknown and none is invented. Responses keep
every existing field and add `value_decimal`, `raw_value_decimal`, `source_scale_decimal` (positional decimal strings of
the stored NUMERIC values, never scientific notation) and `raw_value_text`. Daily aggregates carry only `value_decimal`.
Mapping responses add `scale_decimal`.

### Collector
`edge_collector.telemetry_contract.value_numeral` queues a value as a decimal string and refuses booleans, non-finite
numbers and numerals over 64 characters. `Decimal`, `str` and `int` keep every digit. A `float` can only be sent as its
shortest text: digits it had already lost cannot be recovered, so acquire values as `Decimal` or text.

### Unchanged
Conversion hash and the Decimal context (28 digits), G1 revisions, holds, dedup, alarms, retention, compaction and
history cutoffs. Compaction and late-merge arithmetic (G4) still use floats.
