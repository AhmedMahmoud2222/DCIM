# Network traceability and 3D spatial increment

Network inventory is normalized into managed-asset-backed devices, interfaces, and
physical connections. Connections retain provenance and an authority flag; collector
observations are not silently promoted to operator authority. Path results describe a
**modeled physical path**, never packet reachability.

The 3D workspace deliberately uses the browser's native CSS perspective transform
instead of introducing a rendering framework. This is the smallest maintainable option
for the MVP's bounded rack-room geometry, works with the existing React/Vite stack, and
requires no parallel inventory. It reads the same `/spatial/rooms/{id}/view` projection
as the 2D floor plan. Missing physical values receive view-only rendering defaults and
are never persisted. The layer panel is the extension boundary for equipment, power,
network, cooling, sensor, alarm, and analytical overlays.

Future work explicitly excludes LLDP/CDP and SNMP discovery, LACP, VLAN/VRF catalogs,
routing, live utilization and reachability, cable/patch panels, redundant-path policy,
environmental overlays, CFD, and synthetic heat maps.
