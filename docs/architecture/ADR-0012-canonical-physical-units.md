# ADR-0012: Canonical physical units and telemetry metrics

Status: accepted for issue #99

## Decision

The versioned registry in `app.domain.telemetry.registry` is the authority for telemetry metric names, dimensions, canonical storage units and presentation units. New telemetry writes convert at the ingestion boundary with decimal, half-even rounding to eight decimal places. A mapping's `unit` describes the source value; the reading's `unit` describes its canonical value.

Every converted reading retains `raw_value`, `raw_unit` and `registry_version`. Existing readings remain readable with null raw provenance; migration 0035 adds nullable columns but never rewrites historical numeric values. Affine conversions, including Fahrenheit, are performed once before persistence. Mappings with an unknown unit or a unit from the wrong dimension are rejected.

Catalog extraction continues to preserve the manufacturer's text, raw value and raw unit. Applying candidates to catalog revisions must call the registry boundary for any target field that requires canonical conversion; extraction itself remains non-destructive and does not silently normalize evidence.

## Consequences

- Analytics may rely on a metric's canonical unit instead of guessing from each row.
- Source evidence remains auditable and can be reprocessed by a future registry version.
- Adding a metric or unit requires a registry version decision and conversion tests.
- Existing historical rows are not retroactively labeled as raw evidence they never stored.

## Legacy alarm compatibility

Rule thresholds retain their original numeric values. Migration 0035 records a
legacy rule unit only from unambiguous integration/metric unit evidence across
mappings, readings and retained aggregates. Missing/conflicting evidence is never
guessed: those rules require an explicit unit decision before evaluation.
Readings are converted from their stored unit to the fixed rule unit for comparison,
without applying source scale again. Alarm lifecycle values carry that same unit.
The client renders API presentation values and units, while CSV also retains the
stored value and unit.

The catalog scalar handoff accepts real parser `value_numeric`, preserves original
raw lexemes and page evidence, and rejects text, absent numeric values and ranges
until explicitly resolved. Grams are accepted only as mass and converted to kg.
