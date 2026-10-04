# Issue #105 implementation start — cooling, heat maps and airflow

Base: `main@4000a608b652fde989c35efe1dbeaa769296c498`

Use the existing ManagedAsset extensibility and telemetry identity; do not create a second asset or sensor identity system.

Initial slices:
1. Cooling asset/zone/containment domain design.
2. Environmental sensor spatial binding.
3. Temperature/humidity heat-map service with freshness/data-quality.
4. Airflow direction/volume visualization.
5. Cooling-capacity/environmental exceptions.
6. Permission, stale-data and interpolation-validation tests.

Final visualization depends on issue #104 calibrated spatial geometry; calculations depend on issue #99 units.
