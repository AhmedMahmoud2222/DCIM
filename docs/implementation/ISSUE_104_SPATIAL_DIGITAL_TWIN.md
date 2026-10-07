# Issue #104 implementation start — calibrated spatial digital twin

Base: `main@4000a608b652fde989c35efe1dbeaa769296c498`

Preserve FloorPlan/SpatialObject authority and the mandatory human acceptance boundary.

Initial slices:
1. Spatial coordinate/calibration/grid contract.
2. Secure isolated DXF and VSDX/Visio importer adapters.
3. Rack classification improvements and manual correction tooling.
4. Dimensionally accurate 2D/3D geometry using authoritative rack/equipment dimensions.
5. Power/network/environment overlay contracts.
6. Hostile file, calibration-accuracy and browser workflow tests.

The current CSS 3D view remains a schematic fallback until authoritative calibrated geometry is available.
