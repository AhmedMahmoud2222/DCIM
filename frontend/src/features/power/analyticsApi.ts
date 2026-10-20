import { apiFetch, authenticatedFetchBlob } from "@/lib/apiClient";
import { Page } from "@/types";

export type Quality = "measured" | "stale" | "estimated" | "mixed" | "missing";
export type ProtectionState = "closed" | "open" | "tripped" | "unknown";

export interface NodeRollup {
  id: string;
  node_type: string;
  label: string;
  rated_kw: number | null;
  capacity_kw: number | null;
  load_kw: number;
  allocated_kw: number;
  headroom_kw: number | null;
  allocated_headroom_kw: number | null;
  utilization_pct: number | null;
  quality: Quality;
  level: "ok" | "warning" | "critical" | "overload" | "unknown";
  protection_state: ProtectionState | null;
  interrupted: boolean;
  overloaded: boolean;
  warnings: string[];
}

export interface EquipmentRollup {
  id: string;
  label: string;
  scenario: string;
  quality: Quality;
  demand_kw: number | null;
  served: boolean;
  live_inlets: number;
  inlet_count: number;
}

export interface ScopeRollup {
  scope: string;
  id: string | null;
  load_kw: number;
  allocated_kw: number;
  unserved_kw: number;
  capacity_kw: number | null;
  capacity_basis: string | null;
  headroom_kw: number | null;
  utilization_pct: number | null;
  quality: Quality;
  equipment_count: number;
}

export interface SiteRollup {
  site_id: string;
  generated_at: string;
  metric: string;
  unit: string;
  site: ScopeRollup;
  rooms: ScopeRollup[];
  racks: ScopeRollup[];
  nodes: NodeRollup[];
  equipment: EquipmentRollup[];
  warnings: string[];
}

export interface ProtectionDevice {
  id: string;
  housing_asset_id: string | null;
  site_id: string;
  label: string;
  device_type: string;
  rating_a: number;
  voltage_v: number;
  poles: number;
  phase_config: string;
  rated_kw: number;
  state: ProtectionState;
  status: string;
  state_changed_at: string | null;
  retired_at: string | null;
  upstream_node_ids: string[];
  downstream_node_ids: string[];
  version: number;
}

export interface Snapshot {
  bucket_start: string;
  bucket_end: string;
  scope_type: string;
  scope_id: string;
  unit: string;
  load_kw: number | null;
  load_basis: string;
  effective_capacity_kw: number | null;
  headroom_kw: number | null;
  utilization_pct: number | null;
  sample_count: number;
  expected_samples: number;
  coverage_ratio: number;
  quality: Quality;
}

export type ForecastStatus = "good" | "flat" | "already_over_capacity" | "stale" | "sparse" | "missing" | "estimated_mixed";

export interface Forecast {
  metric: string;
  unit: string;
  method: string;
  status: ForecastStatus;
  no_forecast_reason: string | null;
  current_load_kw: number | null;
  capacity_kw: number | null;
  headroom_kw: number | null;
  utilization_pct: number | null;
  window_start: string;
  window_end: string;
  bucket_count: number;
  measured_bucket_count: number;
  day_count: number;
  sample_count: number;
  coverage_ratio: number | null;
  slope_kw_per_day: number | null;
  r_squared: number | null;
  horizon_days: number;
  exhaustion_date: string | null;
  days_to_exhaustion: number | null;
  confidence: string;
}

export interface ReportJob {
  id: string;
  format: "json" | "csv";
  site_id: string | null;
  status: "queued" | "running" | "completed" | "failed";
  attempts: number;
  row_count: number | null;
  failure_code: string | null;
  created_at: string;
  finished_at: string | null;
}

const A = "/power/analytics";
export const getRollup = (siteId: string) => apiFetch<SiteRollup>(`${A}/sites/${siteId}/rollup`);
export const listProtectionDevices = (siteId: string) =>
  apiFetch<Page<ProtectionDevice>>(`/power/protection-devices?limit=200&site_id=${siteId}`);
export const setProtectionState = (id: string, state: ProtectionState, version: number) =>
  apiFetch<ProtectionDevice>(`/power/protection-devices/${id}/state`, {
    method: "POST",
    body: JSON.stringify({ state }),
    ifMatch: version,
  });
export const getHistory = (scopeType: string, scopeId: string, days = 7) => {
  const end = new Date();
  const start = new Date(end.getTime() - days * 86400000);
  const q = new URLSearchParams({ scope_type: scopeType, scope_id: scopeId, start: start.toISOString(), end: end.toISOString(), limit: "200" });
  return apiFetch<Page<Snapshot>>(`${A}/history?${q}`);
};
export const getForecast = (scopeType: string, scopeId: string) =>
  apiFetch<Forecast>(`${A}/forecast?scope_type=${scopeType}&scope_id=${scopeId}`);
export const createReport = (siteId: string | null, format: "json" | "csv") =>
  apiFetch<ReportJob>(`${A}/reports`, { method: "POST", body: JSON.stringify({ site_id: siteId, format }) });
export const listReports = () => apiFetch<Page<ReportJob>>(`${A}/reports?limit=50`);
export const getReport = (id: string) => apiFetch<ReportJob>(`${A}/reports/${id}`);
export const downloadReport = (id: string, format: "json" | "csv") =>
  authenticatedFetchBlob(`${A}/reports/${id}/download?format=${format}`);
