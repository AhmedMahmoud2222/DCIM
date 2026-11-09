import { apiFetch } from "@/lib/apiClient";

// Thin client for alarm-rule management endpoints. Authorization is enforced by the backend.
export interface AlarmRule {
  id: string;
  integration_id: string;
  metric: string;
  rule_type: string;
  threshold: number | null;
  unit: string | null;
  registry_version: string | null;
  enabled: boolean;
  name: string;
  updated_at: string;
  unit_status: "unresolved" | "inferred_legacy" | "explicit" | "invalid_contract";
  needs_attention: boolean;
  blocked_notified_at: string | null;
  unit_resolved_by: string | null;
  unit_resolved_at: string | null;
  unit_resolution_reason: string | null;
}

export interface AlarmRuleResolution extends AlarmRule {
  retained_blocked_window_readings: number;
  blocked_window_count_complete: boolean;
  retroactive_evaluation: boolean;
}

export interface AlarmRuleListFilters {
  needsAttention?: boolean;
  integrationId?: string;
  limit?: number;
  offset?: number;
}

export const listAlarmRules = (filters: AlarmRuleListFilters = {}) => {
  const params = new URLSearchParams({ limit: String(filters.limit ?? 100), offset: String(filters.offset ?? 0) });
  if (filters.needsAttention) params.set("needs_attention", "true");
  if (filters.integrationId) params.set("integration_id", filters.integrationId);
  return apiFetch<AlarmRule[]>(`/alarms/rules?${params.toString()}`);
};

export interface ResolveRuleUnitInput {
  unit: string;
  reason: string;
  // Optimistic-concurrency guard: the updated_at the operator saw when the row loaded.
  expected_updated_at: string;
}

export interface DisableRuleInput {
  reason: string;
  expected_updated_at: string;
}

export interface CreateAlarmRuleInput {
  integration_id: string;
  metric: string;
  rule_type: string;
  threshold: number | null;
  name: string;
}

export interface EditAlarmRuleInput {
  name?: string;
  threshold?: number;
  reason: string;
  expected_updated_at: string;
}

export const createAlarmRule = (input: CreateAlarmRuleInput) =>
  apiFetch<{ id: string }>("/alarms/rules", { method: "POST", body: JSON.stringify(input) });

export const editAlarmRule = (id: string, input: EditAlarmRuleInput) =>
  apiFetch<AlarmRule>(`/alarms/rules/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(input) });

export const resolveAlarmRuleUnit = (id: string, input: ResolveRuleUnitInput) =>
  apiFetch<AlarmRuleResolution>(`/alarms/rules/${encodeURIComponent(id)}/resolve-unit`, {
    method: "POST",
    body: JSON.stringify(input),
  });

export const disableAlarmRule = (id: string, input: DisableRuleInput) =>
  apiFetch<AlarmRule>(`/alarms/rules/${encodeURIComponent(id)}/disable`, {
    method: "POST",
    body: JSON.stringify(input),
  });
