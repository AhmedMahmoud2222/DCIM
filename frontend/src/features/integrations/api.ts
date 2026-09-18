import { apiFetch } from "@/lib/apiClient";
import {
  Collector,
  CollectorAssignment,
  CollectorCapability,
  CollectorWithSecret,
  DiscoveredDevice,
  Integration,
  ReconciliationDiff,
} from "@/types";

export const listCollectors = () => apiFetch<Collector[]>("/collectors");
export const getCollector = (id: string) => apiFetch<Collector>(`/collectors/${id}`);

export interface RegisterCollectorInput {
  name: string;
  collector_type: "central" | "edge";
  site_id?: string | null;
  version_string?: string | null;
}
export const registerCollector = (body: RegisterCollectorInput) =>
  apiFetch<CollectorWithSecret>("/collectors", { method: "POST", body: JSON.stringify(body) });

export const listCollectorCapabilities = (collectorId: string) =>
  apiFetch<CollectorCapability[]>(`/collectors/${collectorId}/capabilities`);
export const declareCollectorCapabilities = (collectorId: string, protocolCodes: string[]) =>
  apiFetch<void>(`/collectors/${collectorId}/capabilities`, {
    method: "POST",
    body: JSON.stringify({ protocol_codes: protocolCodes }),
  });

export const assignIntegration = (collectorId: string, integrationId: string) =>
  apiFetch<CollectorAssignment>(`/collectors/${collectorId}/assignments`, {
    method: "POST",
    body: JSON.stringify({ integration_id: integrationId }),
  });

export const triggerPollNow = (collectorId: string) =>
  apiFetch<Array<{ integration_id: string; succeeded: boolean; error: string | null; external_identifier: string | null }>>(
    `/collectors/${collectorId}/poll-now`,
    { method: "POST" },
  );

export const listIntegrations = () => apiFetch<Integration[]>("/integrations");
export const getIntegration = (id: string) => apiFetch<Integration>(`/integrations/${id}`);

export interface CreateIntegrationInput {
  name: string;
  integration_type: string;
  site_id?: string | null;
  target_host: string;
  target_port?: number | null;
  config?: Record<string, unknown>;
  credential?: string | null;
  poll_interval_seconds?: number;
  enabled?: boolean;
}
export const createIntegration = (body: CreateIntegrationInput) =>
  apiFetch<Integration>("/integrations", { method: "POST", body: JSON.stringify(body) });

export const updateIntegration = (
  id: string,
  body: Partial<Pick<Integration, "enabled" | "poll_interval_seconds" | "target_host" | "target_port" | "config">> & {
    credential?: string;
  },
  ifMatchVersion: number,
) => apiFetch<Integration>(`/integrations/${id}`, { method: "PATCH", body: JSON.stringify(body), ifMatch: ifMatchVersion });

export const listDiscoveredDevices = () => apiFetch<DiscoveredDevice[]>("/discovery/devices");
export const listReconciliationDiffs = (statusFilter?: string) =>
  apiFetch<ReconciliationDiff[]>(`/discovery/reconciliation${statusFilter ? `?status_filter=${statusFilter}` : ""}`);
export const acceptReconciliation = (diffId: string, matchedManagedAssetId?: string, reason?: string) =>
  apiFetch<ReconciliationDiff>(`/discovery/reconciliation/${diffId}/accept`, {
    method: "POST",
    body: JSON.stringify({ matched_managed_asset_id: matchedManagedAssetId ?? null, reason: reason ?? null }),
  });
export const rejectReconciliation = (diffId: string, reason?: string) =>
  apiFetch<ReconciliationDiff>(`/discovery/reconciliation/${diffId}/reject`, {
    method: "POST",
    body: JSON.stringify({ reason: reason ?? null }),
  });
