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
