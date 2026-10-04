# Issue #102 implementation start — protection, forecasting and reporting

Base: `main@4000a608b652fde989c35efe1dbeaa769296c498`

Extend the current PowerNode/PowerConnection/capacity/A-B redundancy model; do not create a parallel power graph.

Initial slices:
1. Breaker/circuit/protection-device domain design.
2. Deterministic power rollups including A/B double-count prevention.
3. Historical capacity/utilization snapshots.
4. Forecast service with data-quality/confidence metadata.
5. Background report jobs and exports.
6. Failure-impact consistency and regression tests.

Final forecasting calculations depend on issue #99 canonical units.
