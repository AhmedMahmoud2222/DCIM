import { apiFetch } from "@/lib/apiClient";
import { FloorPlan, ImportCandidate, ImportDiagnostics, ImportJob, Page, RoomSpatialView, SpatialObject } from "@/types";

export const listFloorPlans = (roomId?: string) =>
  apiFetch<Page<FloorPlan>>(`/floor-plans?limit=200${roomId ? `&room_id=${roomId}` : ""}`);
export const getFloorPlan = (id: string) => apiFetch<FloorPlan>(`/floor-plans/${id}`);
export const createFloorPlan = (roomId: string) =>
  apiFetch<FloorPlan>("/floor-plans", { method: "POST", body: JSON.stringify({ room_id: roomId }) });
export const activateFloorPlan = (id: string, ifMatch: number) =>
  apiFetch<FloorPlan>(`/floor-plans/${id}/activate`, { method: "POST", ifMatch });
export const listFloorPlanObjects = (id: string) => apiFetch<SpatialObject[]>(`/floor-plans/${id}/objects`);

export const uploadFloorPlanFile = (id: string, file: File) => {
  const formData = new FormData();
  formData.append("file", file);
  return apiFetch<ImportJob>(`/floor-plans/${id}/upload`, { method: "POST", body: formData });
};

export const listImportJobs = (floorPlanId: string) => apiFetch<Page<ImportJob>>(`/floor-plans/${floorPlanId}/import-jobs`);
export const getImportJob = (jobId: string) => apiFetch<ImportJob>(`/floor-plans/import-jobs/${jobId}`);
export const getImportDiagnostics = (jobId: string) =>
  apiFetch<ImportDiagnostics>(`/floor-plans/import-jobs/${jobId}/diagnostics`);
export const listImportCandidates = (jobId: string, status?: string) =>
  apiFetch<Page<ImportCandidate>>(`/floor-plans/import-jobs/${jobId}/candidates?limit=200${status ? `&status=${status}` : ""}`);

export const acceptCandidate = (jobId: string, candidateId: string, objectType: string, label?: string) =>
  apiFetch<ImportCandidate>(`/floor-plans/import-jobs/${jobId}/candidates/${candidateId}/accept`, {
    method: "POST",
    body: JSON.stringify({ object_type: objectType, label }),
  });

export const rejectCandidate = (jobId: string, candidateId: string) =>
  apiFetch<ImportCandidate>(`/floor-plans/import-jobs/${jobId}/candidates/${candidateId}/reject`, { method: "POST" });

export const getRoomSpatialView = (roomId: string) => apiFetch<RoomSpatialView>(`/spatial/rooms/${roomId}/view`);
