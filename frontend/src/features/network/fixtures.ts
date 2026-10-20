import type { Cable, DeviceProfile, Neighbor, VendorProfile } from "@/features/network/api";
import type { CurrentUser } from "@/lib/authStore";

export const userWith = (...permission_codes: string[]): CurrentUser => ({
  id: "u1",
  email: "u@example.com",
  full_name: "Test User",
  role_names: ["Engineer"],
  permission_codes,
});

export const vendor: VendorProfile = {
  id: "v1", code: "cisco", name: "Cisco", description: null, status: "active",
  sys_object_id_prefixes: ["1.3.6.1.4.1.9"], supported_protocols: ["snmp"], discovery_oids: {},
  neighbor_discovery: { lldp: { enabled: true }, cdp: { enabled: true } }, version: 3, retired_at: null,
};

export const device: DeviceProfile = {
  id: "d1", vendor_profile_id: "v1", code: "cisco-switch", name: "Cisco switch", description: null, status: "active",
  device_class: "switch", match_criteria: [{ field: "model", op: "prefix", value: "C93" }], firmware_min: "16.0",
  firmware_max: null, priority: 2, capabilities: { snmp_versions: ["v2c", "v3"], lldp: true, cdp: true },
  interface_discovery: {}, neighbor_behavior: {}, version: 1, retired_at: null,
};

export const neighbor = (overrides: Partial<Neighbor> = {}): Neighbor => ({
  id: "n1", integration_id: "i1", integration_name: "edge-sw-1", protocol: "lldp", source_collector_id: "c1", scan_id: "s1",
  first_seen_at: "2026-10-01T00:00:00Z", last_seen_at: "2026-10-06T00:00:00Z", status: "active", effective_status: "active",
  local_port_name: "Eth1/1", local_port_ref: "1", remote_chassis_ident: "00:50:56:3a:1b:2c", remote_chassis_subtype: "mac_address",
  remote_port_ident: "Eth1/24", remote_port_subtype: "interface_name", remote_port_description: null,
  remote_system_name: "core-sw-1", remote_system_description: null, remote_platform: null,
  remote_management_address: "10.0.0.1", capabilities: ["bridge"], native_vlan: null, ttl_seconds: null, raw_evidence: {},
  reconciliation_state: "proposed", match_evidence: { proposal: { local_port_id: "lp", remote_port_id: "rp" } },
  local_port_id: null, remote_port_id: null, decided_at: null, decision_reason: null, version: 4, ...overrides,
});

export const cable = (overrides: Partial<Cable> = {}): Cable => ({
  id: "c1", label: "PP1-A01", cable_type: "copper_utp", connector_a: null, connector_b: null, length_m: "2.50",
  route_metadata: {}, status: "planned", installed_at: null, removed_at: null, source: "manual", source_neighbor_id: null,
  port_connection_id: null, notes: null, version: 1, created_at: "2026-10-01T00:00:00Z",
  endpoints: [
    { end: "A", restricted: false, port: { port_id: "pa", port_name: "Eth1/1", media_type: "copper", equipment_id: "ea", equipment_hostname: "edge-sw-1", equipment_asset_tag: "EQ-1" } },
    { end: "B", restricted: false, port: { port_id: "pb", port_name: "Eth1/24", media_type: "copper", equipment_id: "eb", equipment_hostname: "core-sw-1", equipment_asset_tag: "EQ-2" } },
  ],
  ...overrides,
});
