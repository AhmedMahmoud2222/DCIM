# Issue #101 implementation start — vendor/device profiles, SNMPv3, LLDP/CDP and cabling

Base: `main@4000a608b652fde989c35efe1dbeaa769296c498`

Preserve the existing Protocol/Driver/Collector architecture and authoritative reconciliation boundary.

Initial slices:
1. Persistent VendorProfile / DeviceProfile / profile metric mapping schema.
2. SNMPv3 USM authPriv transport and real wire tests; retain explicit v2c compatibility.
3. LLDP/CDP normalized discovery with human reconciliation.
4. First-class cable identity, endpoints, lifecycle and path tracing integrated with EquipmentPort/PortConnection.
5. Supported-device/profile matrix plus malformed-protocol/adversarial tests.

Security requirement SEC-04 remains linked to issue #38 until SNMPv3 is independently verified.
