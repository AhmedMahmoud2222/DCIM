import { apiFetch } from "@/lib/apiClient";
import { Equipment, EquipmentModel, EquipmentModelRevision, Page, PlacementType, Side } from "@/types";

export const listEquipment = () => apiFetch<Page<Equipment>>("/equipment?limit=200");
export const getEquipment = (id: string) => apiFetch<Equipment>(`/equipment/${id}`);
export const listEquipmentModels = () => apiFetch<Page<EquipmentModel>>("/equipment-models?limit=200");
export const listEquipmentModelRevisions = (equipmentModelId: string) =>
  apiFetch<Page<EquipmentModelRevision>>(`/equipment-models/${equipmentModelId}/revisions?limit=200`);

export interface CreateEquipmentInput {
  asset_tag: string;
  model_revision_id: string;
  hostname?: string;
}

export const createEquipment = (body: CreateEquipmentInput, idempotencyKey: string) =>
  apiFetch<Equipment>("/equipment", { method: "POST", body: JSON.stringify(body), idempotencyKey });

export const createEquipmentModel = (manufacturer: string, model_name: string) =>
  apiFetch<EquipmentModel>("/equipment-models", { method: "POST", body: JSON.stringify({ manufacturer, model_name }) });

export const createEquipmentModelRevision = (equipmentModelId: string) =>
  apiFetch<EquipmentModelRevision>(`/equipment-models/${equipmentModelId}/revisions`, {
    method: "POST",
    body: JSON.stringify({}),
  });

export interface MoveEquipmentInput {
  placement_type: PlacementType;
  room_id: string;
  rack_id?: string;
  u_start?: number;
  u_end?: number;
  side?: Side;
}

export const moveEquipment = (id: string, body: MoveEquipmentInput, ifMatch?: number) =>
  apiFetch<Equipment>(`/equipment/${id}/move`, { method: "POST", body: JSON.stringify(body), ifMatch });

export const retireEquipment = (id: string) => apiFetch<Equipment>(`/equipment/${id}/retire`, { method: "POST" });
