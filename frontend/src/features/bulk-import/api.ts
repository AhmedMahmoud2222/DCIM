import { apiFetch, authenticatedFetchBlob } from "@/lib/apiClient";
import { BulkImportJob, BulkImportMode, BulkImportRow, Page } from "@/types";

/** Generic bulk-import job pipeline shared by rack/equipment/catalog XLSX imports
 * (backend/app/api/v1/bulk_import.py). Upload/template-download are per-resource
 * (`/racks`, `/equipment`, `/catalog`); everything after upload is generic, addressed
 * only by job id, per the backend's shared-pipeline design. */
export type BulkImportResource = "rack" | "equipment" | "catalog";

const RESOURCE_BASE_PATHS: Record<BulkImportResource, string> = {
  rack: "/racks",
  equipment: "/equipment",
  catalog: "/catalog",
};

export const downloadImportTemplate = (resource: BulkImportResource) =>
  authenticatedFetchBlob(`${RESOURCE_BASE_PATHS[resource]}/import-template`);

export const uploadImportJob = (resource: BulkImportResource, file: File, mode: BulkImportMode) => {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch<BulkImportJob>(`${RESOURCE_BASE_PATHS[resource]}/import-jobs?mode=${mode}`, {
    method: "POST",
    body: formData,
  });
};

export const getImportJob = (jobId: string) => apiFetch<BulkImportJob>(`/import-jobs/${jobId}`);

export const listImportJobRows = (jobId: string, params: { status?: string; limit?: number; offset?: number } = {}) => {
  const query = new URLSearchParams();
  if (params.status) query.set("status", params.status);
  query.set("limit", String(params.limit ?? 50));
  query.set("offset", String(params.offset ?? 0));
  return apiFetch<Page<BulkImportRow>>(`/import-jobs/${jobId}/rows?${query.toString()}`);
};

export const commitImportJob = (jobId: string) =>
  apiFetch<void>(`/import-jobs/${jobId}/commit`, { method: "POST" });

export const cancelImportJob = (jobId: string) =>
  apiFetch<void>(`/import-jobs/${jobId}/cancel`, { method: "POST" });

export const downloadImportReport = (jobId: string) => authenticatedFetchBlob(`/import-jobs/${jobId}/report`);
