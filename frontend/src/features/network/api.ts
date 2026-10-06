import { apiFetch } from "@/lib/apiClient";
import type { Page } from "@/types";

// ------------------------------------------------------------------ profiles
export interface VendorProfile {
  id: string;
  code: string;
  name: string;
  description: string | null;
  status: "active" | "retired";
  sys_object_id_prefixes: string[];
  supported_protocols: string[];
  discovery_oids: Record<string, string>;
  neighbor_discovery: { lldp?: { enabled: boolean; table_oid?: string }; cdp?: { enabled: boolean; table_oid?: string } };
  version: number;
  retired_at: string | null;
}

export interface DeviceProfile {
  id: string;
  vendor_profile_id: string;
  code: string;
  name: string;
  description: string | null;
  status: "active" | "retired";
  device_class: string;
  match_criteria: Array<{ field: string; op: string; value: string | string[] }>;
  firmware_min: string | null;
  firmware_max: string | null;
  priority: number;
  capabilities: { metrics?: boolean; interfaces?: boolean; lldp?: boolean; cdp?: boolean; snmp_versions?: string[] };
  interface_discovery: Record<string, unknown>;
  neighbor_behavior: { lldp?: { enabled: boolean }; cdp?: { enabled: boolean } };
  version: number;
  retired_at: string | null;
}

export interface MetricMapping {
  id: string;
  oid: string;
  canonical_metric: string;
  unit: string;
  scale: number;
  value_type: string;
  description: string | null;
}

export interface ProfileTemplate {
  key: string;
  title: string;
  vendor: Record<string, unknown> & { code: string; name: string };
  devices: Array<Record<string, unknown> & { code: string }>;
}

export interface MatchFacts {
  sys_object_id?: string;
  sys_descr?: string;
  model?: string;
  hardware_revision?: string;
  firmware?: string;
}

export interface MatchResult {
  state: "matched" | "vendor_only" | "ambiguous" | "no_match";
  vendor_profile_id: string | null;
  device_profile_id: string | null;
  candidate_vendor_profile_ids: string[];
  candidate_device_profile_ids: string[];
  reasons: string[];
}

export const listVendorProfiles = (includeRetired = false) =>
  apiFetch<VendorProfile[]>(`/network-profiles/vendors?include_retired=${includeRetired}`);
export const listDeviceProfiles = (includeRetired = false) =>
  apiFetch<DeviceProfile[]>(`/network-profiles/devices?include_retired=${includeRetired}`);
export const listDeviceMetricMappings = (deviceId: string) =>
  apiFetch<MetricMapping[]>(`/network-profiles/devices/${deviceId}/metric-mappings?effective=true`);
export const listProfileTemplates = () => apiFetch<ProfileTemplate[]>("/network-profiles/templates");
export const createVendorProfile = (body: Record<string, unknown>) =>
  apiFetch<VendorProfile>("/network-profiles/vendors", { method: "POST", body: JSON.stringify(body) });
export const createDeviceProfile = (vendorId: string, body: Record<string, unknown>) =>
  apiFetch<DeviceProfile>(`/network-profiles/vendors/${vendorId}/devices`, { method: "POST", body: JSON.stringify(body) });
export const retireVendorProfile = (id: string, version: number) =>
  apiFetch<VendorProfile>(`/network-profiles/vendors/${id}/retire`, { method: "POST", ifMatch: version });
export const retireDeviceProfile = (id: string, version: number) =>
  apiFetch<DeviceProfile>(`/network-profiles/devices/${id}/retire`, { method: "POST", ifMatch: version });
export const matchProfile = (facts: MatchFacts) =>
  apiFetch<MatchResult>("/network-profiles/match", { method: "POST", body: JSON.stringify(facts) });

// ------------------------------------------------------------------ neighbors
export type ReconciliationState = "unmatched" | "ambiguous" | "proposed" | "conflict" | "confirmed" | "rejected";

export interface Neighbor {
  id: string;
  integration_id: string;
  integration_name: string | null;
  protocol: "lldp" | "cdp";
  source_collector_id: string | null;
  scan_id: string | null;
  first_seen_at: string;
  last_seen_at: string;
  status: "active" | "stale";
  effective_status: "active" | "stale";
  local_port_name: string | null;
  local_port_ref: string | null;
  remote_chassis_ident: string;
  remote_chassis_subtype: string | null;
  remote_port_ident: string;
  remote_port_subtype: string | null;
  remote_port_description: string | null;
  remote_system_name: string | null;
  remote_system_description: string | null;
  remote_platform: string | null;
  remote_management_address: string | null;
  capabilities: string[];
  native_vlan: number | null;
  ttl_seconds: number | null;
  raw_evidence: Record<string, unknown>;
  reconciliation_state: ReconciliationState;
  match_evidence: {
    proposal?: { local_port_id: string; remote_port_id: string };
    reasons?: string[];
    local?: { state: string; reason?: string; candidates?: string[] };
    remote_device?: { state: string; reason?: string; suggested_equipment_id?: string; candidates?: string[] };
    remote_port?: { state: string; reason?: string; candidates?: string[] };
    conflicts?: Array<{ kind: string; id: string }>;
    protocol_disagreement?: Array<{ neighbor_id: string; protocol: string; remote: string }>;
    decision?: { override: boolean };
  };
  local_port_id: string | null;
  remote_port_id: string | null;
  decided_at: string | null;
  decision_reason: string | null;
  version: number;
}

export interface NeighborFilters {
  reconciliation_state?: ReconciliationState | "";
  protocol?: "lldp" | "cdp" | "";
}

export const listNeighbors = (filters: NeighborFilters = {}) => {
  const query = new URLSearchParams({ limit: "200" });
  if (filters.reconciliation_state) query.set("reconciliation_state", filters.reconciliation_state);
  if (filters.protocol) query.set("protocol", filters.protocol);
  return apiFetch<Page<Neighbor>>(`/discovery/neighbors?${query.toString()}`);
};

export interface NeighborDecision {
  local_port_id?: string | null;
  remote_port_id?: string | null;
  reason?: string | null;
}

const decide = (neighbor: Neighbor, action: string, body: NeighborDecision = {}) =>
  apiFetch<Neighbor>(`/discovery/neighbors/${neighbor.id}/${action}`, {
    method: "POST",
    body: JSON.stringify(body),
    ifMatch: neighbor.version,
  });
export const confirmNeighbor = (neighbor: Neighbor, body?: NeighborDecision) => decide(neighbor, "confirm", body);
export const rejectNeighbor = (neighbor: Neighbor, body?: NeighborDecision) => decide(neighbor, "reject", body);
export const revokeNeighbor = (neighbor: Neighbor, body?: NeighborDecision) => decide(neighbor, "revoke", body);
export const rematchNeighbor = (neighbor: Neighbor) =>
  apiFetch<Neighbor>(`/discovery/neighbors/${neighbor.id}/rematch`, { method: "POST" });

// ------------------------------------------------------------------ cables
export const CABLE_TYPES = ["copper_utp", "copper_stp", "coax", "fiber_sm", "fiber_mm", "dac", "aoc", "console", "other"] as const;
export type CableType = (typeof CABLE_TYPES)[number];
export type CableStatus = "planned" | "installed" | "removed";

export interface CablePortRef {
  port_id: string;
  port_name: string;
  media_type: string;
  equipment_id: string;
  equipment_hostname: string | null;
  equipment_asset_tag: string;
}

export interface CableEndpoint {
  end: "A" | "B";
  restricted: boolean;
  port: CablePortRef | null;
}

export interface Cable {
  id: string;
  label: string;
  cable_type: CableType;
  connector_a: string | null;
  connector_b: string | null;
  length_m: string | null;
  route_metadata: Record<string, unknown>;
  status: CableStatus;
  installed_at: string | null;
  removed_at: string | null;
  source: "manual" | "discovery_confirmed" | "import";
  source_neighbor_id: string | null;
  port_connection_id: string | null;
  notes: string | null;
  version: number;
  created_at: string;
  endpoints: CableEndpoint[];
}

export interface CableInput {
  label: string;
  cable_type: CableType;
  connector_a?: string | null;
  connector_b?: string | null;
  length_m?: string | null;
  notes?: string | null;
  status?: "planned" | "installed";
}

export const listCables = (status?: CableStatus | "") => {
  const query = new URLSearchParams({ limit: "200" });
  if (status) query.set("status", status);
  return apiFetch<Page<Cable>>(`/cables?${query.toString()}`);
};
export const createCable = (body: CableInput & { endpoint_a_port_id: string; endpoint_b_port_id: string }) =>
  apiFetch<Cable>("/cables", { method: "POST", body: JSON.stringify(body) });
export const createCableFromNeighbor = (neighborId: string, body: CableInput) =>
  apiFetch<Cable>(`/cables/from-neighbor/${neighborId}`, { method: "POST", body: JSON.stringify(body) });
export const installCable = (cable: Cable) =>
  apiFetch<Cable>(`/cables/${cable.id}/install`, { method: "POST", body: JSON.stringify({}), ifMatch: cable.version });
export const removeCable = (cable: Cable, reason?: string) =>
  apiFetch<Cable>(`/cables/${cable.id}/remove`, {
    method: "POST",
    body: JSON.stringify({ reason: reason ?? null }),
    ifMatch: cable.version,
  });
export const deleteCable = (cable: Cable) => apiFetch<void>(`/cables/${cable.id}`, { method: "DELETE", ifMatch: cable.version });

// ------------------------------------------------------------------ trace
export interface TracePortView {
  port_id: string;
  port_name: string;
  stable_key: string;
  media_type: string;
  equipment_id: string;
  equipment_hostname: string | null;
  equipment_asset_tag: string;
}

export interface TraceCable {
  id: string;
  label: string;
  cable_type: string;
  status: string;
  length_m: number | null;
  source: string;
  installed_at: string | null;
  removed_at: string | null;
}

export interface TraceEvidence {
  neighbor_id: string;
  protocol: string;
  reconciliation_state: string;
  status: string;
  authoritative: false;
  last_seen_at: string;
  local_port_id: string | null;
  remote_port_id: string | null;
  remote_system_name: string | null;
  remote_chassis_ident: string;
  remote_port_ident: string;
}

export interface TraceResult {
  start: TracePortView;
  path: Array<{
    link: { kind: "cable" | "port_connection" | "none"; cable?: TraceCable; cable_label?: string | null; note?: string; status?: string };
    hop: { restricted: boolean; remote: TracePortView | null } | null;
  }>;
  terminated: "end_of_path" | "no_link" | "restricted";
  evidence: { authoritative: false; neighbors: TraceEvidence[]; agreement: "agrees" | "disagrees" | "no_evidence" | "undocumented_adjacency" };
  previous_cables: TraceCable[];
}

export const traceFromPort = (portId: string) => apiFetch<TraceResult>(`/topology/ports/${portId}/trace`);
