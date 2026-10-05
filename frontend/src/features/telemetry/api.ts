import { apiFetch } from "@/lib/apiClient";
import { LatestPortStatus } from "@/types";

export interface TelemetryReading {
  id: string;
  integration_id: string;
  managed_asset_id: string | null;
  external_identifier: string;
  metric: string;
  unit: string;
  value: number;
  presentation_unit: string;
  presentation_value: number;
  raw_value?: number | null;
  raw_unit?: string | null;
  registry_version?: string;
  occurred_at: string;
  received_at: string;
  expected_poll_interval_seconds?: number | null;
  resolution?: "raw" | "daily";
  minimum_value?: number | null;
  maximum_value?: number | null;
  presentation_minimum_value?: number | null;
  presentation_maximum_value?: number | null;
  sample_count?: number | null;
}

export interface Alarm {
  id: string;
  rule_id: string;
  integration_id: string;
  managed_asset_id: string | null;
  subject_key: string;
  status: "ACTIVE" | "ACKNOWLEDGED" | "CLEARED";
  opened_at: string;
  acknowledged_at: string | null;
  cleared_at: string | null;
  last_value: number;
}

export interface AlarmHistoryPage { items: Alarm[]; next_cursor: string | null }

// Events tab (restoration): the backend's GET /alarms/history already accepts every
// filter below — this is a thin, additive client-side wrapper, not a new endpoint.
export interface AlarmHistoryFilters {
  status?: Alarm["status"];
  integrationId?: string;
  managedAssetId?: string;
  ruleId?: string;
  start?: Date;
  end?: Date;
  cursor?: string;
  limit?: number;
}

export const getLatestTelemetry = (managedAssetId: string) =>
  apiFetch<TelemetryReading[]>(`/telemetry/latest?managed_asset_id=${encodeURIComponent(managedAssetId)}&limit=100`);

export const getTelemetryHistory = (assetId: string, metric: string, start: Date, end: Date) =>
  apiFetch<TelemetryReading[]>(
    `/telemetry/history?managed_asset_id=${encodeURIComponent(assetId)}&metric=${encodeURIComponent(metric)}&start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}&limit=1000`,
  );

export const listAlarmHistory = (filters: AlarmHistoryFilters = {}) => {
  const params = new URLSearchParams({ limit: String(filters.limit ?? 50) });
  if (filters.status) params.set("status", filters.status);
  if (filters.integrationId) params.set("integration_id", filters.integrationId);
  if (filters.managedAssetId) params.set("managed_asset_id", filters.managedAssetId);
  if (filters.ruleId) params.set("rule_id", filters.ruleId);
  if (filters.start) params.set("start", filters.start.toISOString());
  if (filters.end) params.set("end", filters.end.toISOString());
  if (filters.cursor) params.set("cursor", filters.cursor);
  return apiFetch<AlarmHistoryPage>(`/alarms/history?${params.toString()}`);
};

export const getAlarmHistory = (assetId: string, start?: Date, end?: Date, cursor?: string) =>
  listAlarmHistory({ managedAssetId: assetId, start, end, cursor });

export const acknowledgeAlarm = (id: string) => apiFetch<Alarm>(`/alarms/${id}/acknowledge`, { method: "POST" });
export const getOpenAlarms = (status?: "ACTIVE" | "ACKNOWLEDGED", limit = 100) =>
  apiFetch<Alarm[]>(`/alarms?${new URLSearchParams({ limit: String(limit), ...(status ? { status } : {}) }).toString()}`);

// -------------------------------------------------------- Phase 10C: port/inlet status

export const getLatestPortStatusForEquipment = (equipmentId: string) =>
  apiFetch<LatestPortStatus[]>(`/telemetry/port-status/latest?equipment_id=${encodeURIComponent(equipmentId)}`);

export const getLatestPortStatusForRack = (rackId: string) =>
  apiFetch<LatestPortStatus[]>(`/telemetry/port-status/latest?rack_id=${encodeURIComponent(rackId)}`);
