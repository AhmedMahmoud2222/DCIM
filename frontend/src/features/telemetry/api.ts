import { apiFetch } from "@/lib/apiClient";

export interface TelemetryReading {
  id: string;
  integration_id: string;
  managed_asset_id: string | null;
  managed_asset_id: string | null;
  external_identifier: string;
  metric: string;
  unit: string;
  value: number;
  occurred_at: string;
  received_at: string;
  expected_poll_interval_seconds?: number | null;
  resolution?: "raw" | "daily";
  minimum_value?: number | null;
  maximum_value?: number | null;
  sample_count?: number | null;
}

export interface Alarm {
  id: string;
  rule_id: string;
  integration_id: string;
  subject_key: string;
  status: "ACTIVE" | "ACKNOWLEDGED" | "CLEARED";
  opened_at: string;
  acknowledged_at: string | null;
  cleared_at: string | null;
  last_value: number;
}

export interface AlarmHistoryPage { items: Alarm[]; next_cursor: string | null }

export const getLatestTelemetry = (managedAssetId: string) =>
  apiFetch<TelemetryReading[]>(`/telemetry/latest?managed_asset_id=${encodeURIComponent(managedAssetId)}&limit=100`);

export const getTelemetryHistory = (assetId: string, metric: string, start: Date, end: Date) =>
  apiFetch<TelemetryReading[]>(
    `/telemetry/history?managed_asset_id=${encodeURIComponent(assetId)}&metric=${encodeURIComponent(metric)}&start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}&limit=1000`,
  );

export const getAlarmHistory = (assetId: string, start?: Date, end?: Date, cursor?: string) => {
  const params = new URLSearchParams({ managed_asset_id: assetId, limit: "50" });
  if (start) params.set("start", start.toISOString());
  if (end) params.set("end", end.toISOString());
  if (cursor) params.set("cursor", cursor);
  return apiFetch<AlarmHistoryPage>(`/alarms/history?${params.toString()}`);
};

export const acknowledgeAlarm = (id: string) => apiFetch<Alarm>(`/alarms/${id}/acknowledge`, { method: "POST" });
export const getOpenAlarms = (status?: "ACTIVE" | "ACKNOWLEDGED") =>
  apiFetch<Alarm[]>(`/alarms${status ? `?status=${status}` : ""}`);
