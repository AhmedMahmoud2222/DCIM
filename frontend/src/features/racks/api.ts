import { apiFetch } from "@/lib/apiClient";
import { Page, Rack, RackElevation, RackModel, RackModelRevision, Room } from "@/types";

export const listRacks = () => apiFetch<Page<Rack>>("/racks?limit=200");
export const getRack = (id: string) => apiFetch<Rack>(`/racks/${id}`);
export const getRackElevation = (id: string) => apiFetch<RackElevation>(`/racks/${id}/elevation`);
export const listRooms = () => apiFetch<Page<Room>>("/rooms?limit=200");
export const listRackModels = () => apiFetch<Page<RackModel>>("/rack-models?limit=200");
export const listRackModelRevisions = (rackModelId: string) =>
  apiFetch<Page<RackModelRevision>>(`/rack-models/${rackModelId}/revisions?limit=200`);

export interface CreateRackInput {
  asset_tag: string;
  model_revision_id: string;
  name: string;
  owner?: string;
  room_id?: string;
  x_mm?: number;
  y_mm?: number;
  rotation_deg?: number;
}

export const createRack = (body: CreateRackInput, idempotencyKey: string) =>
  apiFetch<Rack>("/racks", { method: "POST", body: JSON.stringify(body), idempotencyKey });

export const createRackModel = (manufacturer: string, model_name: string) =>
  apiFetch<RackModel>("/rack-models", { method: "POST", body: JSON.stringify({ manufacturer, model_name }) });

export const createRackModelRevision = (
  rackModelId: string,
  body: { height_u: number; width_mm: number; depth_mm: number },
) =>
  apiFetch<RackModelRevision>(`/rack-models/${rackModelId}/revisions`, { method: "POST", body: JSON.stringify(body) });

export interface MoveRackInput {
  room_id: string;
  x_mm?: number;
  y_mm?: number;
  rotation_deg?: number;
}

export const moveRack = (id: string, body: MoveRackInput, ifMatch?: number) =>
  apiFetch<Rack>(`/racks/${id}/move`, { method: "POST", body: JSON.stringify(body), ifMatch });

export const retireRack = (id: string) => apiFetch<Rack>(`/racks/${id}/retire`, { method: "POST" });
