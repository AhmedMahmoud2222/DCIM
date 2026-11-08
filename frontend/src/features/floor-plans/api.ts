import { apiFetch } from "@/lib/apiClient";
import {
  AcceptCandidateRequest,
  Calibration,
  CalibrationRequest,
  FloorPlan,
  ImportCandidate,
  ImportDiagnostics,
  ImportJob,
  OverlayKind,
  Page,
  RoomOverlays,
  RoomSpatialView,
  SourceGeometry,
  SpatialObject,
} from "@/types";

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
export const getSourceGeometry = (jobId: string) => apiFetch<SourceGeometry>(`/floor-plans/import-jobs/${jobId}/source-geometry`);
export const listImportCandidates = (jobId: string, status?: string) =>
  apiFetch<Page<ImportCandidate>>(`/floor-plans/import-jobs/${jobId}/candidates?limit=200${status ? `&status=${status}` : ""}`);

export interface CandidateCorrection {
  cx?: number;
  cy?: number;
  width?: number;
  height?: number;
  rotation_deg?: number;
  points?: [number, number][];
  label?: string;
  object_type?: string;
  matched_asset_id?: string;
  clear_match?: boolean;
}

export const correctCandidate = (jobId: string, candidate: ImportCandidate, body: CandidateCorrection) =>
  apiFetch<ImportCandidate>(`/floor-plans/import-jobs/${jobId}/candidates/${candidate.id}`, {
    method: "PATCH",
    body: JSON.stringify(body),
    ifMatch: candidate.version,
  });

export const undoCandidateCorrection = (jobId: string, candidate: ImportCandidate) =>
  apiFetch<ImportCandidate>(`/floor-plans/import-jobs/${jobId}/candidates/${candidate.id}/undo`, {
    method: "POST",
    ifMatch: candidate.version,
  });

export const acceptCandidate = (jobId: string, candidate: ImportCandidate, body: AcceptCandidateRequest) =>
  apiFetch<ImportCandidate>(`/floor-plans/import-jobs/${jobId}/candidates/${candidate.id}/accept`, {
    method: "POST",
    body: JSON.stringify(body),
    ifMatch: candidate.version,
  });

export const rejectCandidate = (jobId: string, candidate: ImportCandidate) =>
  apiFetch<ImportCandidate>(`/floor-plans/import-jobs/${jobId}/candidates/${candidate.id}/reject`, {
    method: "POST",
    ifMatch: candidate.version,
  });

export const reconcileJob = (jobId: string) =>
  apiFetch<Record<string, number>>(`/floor-plans/import-jobs/${jobId}/reconcile`, { method: "POST" });

export const setCalibration = (floorPlan: FloorPlan, body: CalibrationRequest) =>
  apiFetch<{ floor_plan: FloorPlan; calibration: Calibration; reconciled: Record<string, number> }>(
    `/floor-plans/${floorPlan.id}/calibration`,
    { method: "POST", body: JSON.stringify(body), ifMatch: floorPlan.version },
  );

export const listCalibrations = (floorPlanId: string) => apiFetch<Calibration[]>(`/floor-plans/${floorPlanId}/calibrations`);

export type BoundaryRequest =
  | { shape: "rect"; x_mm: number; y_mm: number; width_mm: number; height_mm: number; rotation_deg?: number }
  | { shape: "polygon"; points_mm: [number, number][] };

export const setRoomBoundary = (floorPlan: FloorPlan, body: BoundaryRequest) =>
  apiFetch<{ spatial_object_id: string; floor_plan: FloorPlan; racks_outside_boundary: string[] }>(
    `/floor-plans/${floorPlan.id}/room-boundary`,
    { method: "PUT", body: JSON.stringify(body), ifMatch: floorPlan.version },
  );

export const getRoomSpatialView = (roomId: string) => apiFetch<RoomSpatialView>(`/spatial/rooms/${roomId}/view`);
export const getRoomOverlays = (roomId: string, kinds: OverlayKind[]) =>
  apiFetch<RoomOverlays>(`/spatial/rooms/${roomId}/overlays?kinds=${kinds.join(",")}`);
