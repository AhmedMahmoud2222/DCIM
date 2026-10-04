# Issue #106 implementation start — validated CFD / thermal analysis

Base: `main@4000a608b652fde989c35efe1dbeaa769296c498`

This branch is intentionally dependency-gated. Do not claim validated CFD until calibrated geometry and cooling/environment inputs exist.

Initial slices:
1. Versioned CFD boundary-condition/input schema.
2. Solver-adapter contract and isolated resource-limited job runner.
3. Sensor calibration and error/uncertainty metrics.
4. Scenario comparison and immutable result provenance.
5. Convergence/invalid-result safety rules.
6. Benchmark fixtures and operator report.

Hard dependencies: issues #99, #104 and #105.
