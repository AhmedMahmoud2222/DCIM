import { apiFetch } from "@/lib/apiClient";
import {
  DEFAULT_LIFECYCLE_SETTINGS,
  DEFAULT_LIFECYCLE_STATES,
} from "@/features/managed-assets/lifecycleDefaults";
import {
  LifecycleHistoryResponse,
  LifecycleSettingsResponse,
  LifecycleStateConfig,
  ManagedAsset,
} from "@/types";

export { DEFAULT_LIFECYCLE_SETTINGS, DEFAULT_LIFECYCLE_STATES };

export function transitionManagedAssetLifecycle(
  assetId: string,
  toStatus: string,
  reason?: string,
): Promise<ManagedAsset> {
  const payload =
    reason !== undefined ? { to_status: toStatus, reason } : { to_status: toStatus };
  return apiFetch<ManagedAsset>(`/managed-assets/${assetId}/lifecycle-transition`, {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function getManagedAssetLifecycleHistory(assetId: string): Promise<LifecycleHistoryResponse> {
  return apiFetch<LifecycleHistoryResponse>(`/managed-assets/${assetId}/lifecycle-history`);
}

export function getLifecycleSettings(): Promise<LifecycleSettingsResponse> {
  return apiFetch<LifecycleSettingsResponse>("/settings/lifecycle");
}

export function updateLifecycleSettings(
  states: LifecycleStateConfig[],
  version: number,
): Promise<LifecycleSettingsResponse> {
  return apiFetch<LifecycleSettingsResponse>("/settings/lifecycle", {
    method: "PUT",
    ifMatch: version,
    body: JSON.stringify({ states, version }),
  });
}

export function restoreDefaultLifecycleSettings(
  version: number,
): Promise<LifecycleSettingsResponse> {
  return apiFetch<LifecycleSettingsResponse>("/settings/lifecycle/restore-defaults", {
    method: "POST",
    ifMatch: version,
    body: JSON.stringify({ version }),
  });
}
