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
`validate_catalog_candidate_unit(candidate.unit, expected_dimension)` and then
`convert_value(candidate.value, candidate.unit, target_unit)` from the canonical
registry. Missing, unknown or dimensionally incompatible units are application errors;
they must never be guessed. The executable parser-to-registry contract is covered with
real extracted power, length and mass candidates, rather than only comparing registry
version constants.

Telemetry presentation follows the same boundary: APIs retain canonical `value` and
`unit` and add `presentation_value` and `presentation_unit`. Existing rows migrated
from main deliberately receive null raw provenance because their original source value
and unit cannot be reconstructed honestly; newly ingested rows store raw and canonical
values separately.
