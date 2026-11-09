import { apiFetch } from "@/lib/apiClient";

export interface ManagedAssetLifecycle {
  id: string;
  asset_type: string;
  asset_tag: string;
  serial_number: string | null;
  lifecycle_status: string;
  created_at: string;
}

export const transitionManagedAssetLifecycle = (id: string, toStatus: string) =>
  apiFetch<ManagedAssetLifecycle>(`/managed-assets/${id}/lifecycle-transition`, {
    method: "POST",
    body: JSON.stringify({ to_status: toStatus }),
  });
