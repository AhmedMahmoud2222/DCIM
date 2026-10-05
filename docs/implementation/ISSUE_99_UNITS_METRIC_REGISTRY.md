# Issue #99 implementation start — units and canonical telemetry registry

Base: `main@4000a608b652fde989c35efe1dbeaa769296c498`

This draft branch implements issue #99. Keep the first delivery architecture-first: inventory current physical-value storage, define the canonical dimension/unit and metric registry contracts, then add backwards-compatible schema/service changes. Preserve raw source values and units, prohibit silent historical rewrites and prove conversions with round-trip tests.

Initial slices:
1. Architecture decision + current-field inventory.
2. Canonical metric/unit registry domain and migration.
3. Conversion/validation service boundaries.
4. Telemetry/catalog integration.
5. Frontend presentation and migration/regression tests.

Do not merge until exact-head CI and independent architecture/data-integrity review pass.

## Catalog extraction handoff to #109

Extraction remains lossless: its candidates keep the manufacturer's `raw_value`,
`raw_unit`, exact source text and page, and do not convert during parsing. Before #109
applies an accepted candidate, it must call
`convert_extracted_catalog_candidate(candidate)` from
`app.application.catalog_documents.extraction.unit_handoff`. The adapter uses
`value_numeric` only for finite scalar numeric candidates; missing numeric values,
text candidates and populated `value_max` ranges are rejected for explicit selection.
It preserves the parser's exact `raw_value` and `raw_unit` separately from the numeric
source value and normalized source unit. That executable field contract converts power fields to
W, dimensions to mm, and weights to kg while retaining extracted provenance. Missing,
unknown or dimensionally incompatible units and fields are application errors;
they must never be guessed. The executable parser-to-registry contract is covered with
real extracted power, length and mass candidates, rather than only comparing registry
version constants.

Telemetry presentation follows the same boundary: APIs retain canonical `value` and
`unit` and add `presentation_value` and `presentation_unit`. Existing rows migrated
from main deliberately retain a null registry version and null raw provenance because
their original source value and unit cannot be reconstructed honestly. Read APIs return
those historical values unchanged. New mappings are explicitly registry v1 and new
readings store `raw_value`, `raw_unit`, and `source_scale` beside the canonical value.
Canonicalization is exactly `(raw_value * source_scale)` followed by one unit conversion,
and registry version participates in series identity.

Legacy numeric thresholds are never rewritten. Migration 0035 snapshots a unit only
when mappings, readings and retained aggregates provide unambiguous unit evidence
for the integration/metric. Conflicting or missing evidence leaves the unit
unresolved and evaluation rejects with an explicit 422 error requiring a unit
decision. Raw source units cannot establish an old threshold's authored unit.

Alarm comparison converts each stored reading into the fixed rule unit. It never
scales or canonicalizes the reading a second time, and source-unit changes cannot
reinterpret thresholds. Alarm value and details change together in that unit.
Clients render presentation fields; event CSV also retains stored values and units.
