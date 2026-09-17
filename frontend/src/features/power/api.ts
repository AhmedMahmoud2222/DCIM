import { apiFetch } from "@/lib/apiClient";
import {
  CapacityException,
  CapacityFigures,
  DashboardExceptionItem,
  DashboardSummary,
  EquipmentPowerSummary,
  Page,
  PowerConnection,
  PowerNode,
  TopologyNode,
} from "@/types";

export const listPowerNodes = (nodeType?: string) =>
  apiFetch<Page<PowerNode>>(`/power/nodes?limit=200${nodeType ? `&node_type=${nodeType}` : ""}`);
export const getPowerNode = (id: string) => apiFetch<PowerNode>(`/power/nodes/${id}`);
export const retirePowerNode = (id: string) => apiFetch<PowerNode>(`/power/nodes/${id}/retire`, { method: "POST" });

export const getUpstream = (nodeId: string) => apiFetch<TopologyNode[]>(`/power/nodes/${nodeId}/upstream`);
export const getDownstream = (nodeId: string) => apiFetch<TopologyNode[]>(`/power/nodes/${nodeId}/downstream`);

export const getNodeCapacity = (nodeId: string) => apiFetch<CapacityFigures>(`/power/nodes/${nodeId}/capacity`);

export interface SetCapacityInput {
  rated_capacity_kw?: number;
  configured_capacity_kw?: number;
  warning_threshold_pct?: number;
  critical_threshold_pct?: number;
  redundancy_factor?: string;
}

export const setNodeCapacity = (nodeId: string, body: SetCapacityInput, ifMatch?: number) =>
  apiFetch<CapacityFigures>(`/power/nodes/${nodeId}/capacity`, { method: "PUT", body: JSON.stringify(body), ifMatch });

export const listCapacityExceptions = () => apiFetch<CapacityException[]>("/power/capacity-exceptions");

export const getEquipmentPowerSummary = (equipmentAssetId: string) =>
  apiFetch<EquipmentPowerSummary>(`/power/equipment/${equipmentAssetId}/power-summary`);

export interface CreatePowerAssetInput {
  asset_tag: string;
  name: string;
  [key: string]: unknown;
}

export const createUtilityIntake = (label: string) =>
  apiFetch<PowerNode>("/power/utility-intakes", { method: "POST", body: JSON.stringify({ label }) });
export const createPdu = (body: CreatePowerAssetInput) =>
  apiFetch<PowerNode>("/power/pdus", { method: "POST", body: JSON.stringify(body) });
export const createUps = (body: CreatePowerAssetInput) =>
  apiFetch<PowerNode>("/power/upses", { method: "POST", body: JSON.stringify(body) });
export const createGenerator = (body: CreatePowerAssetInput) =>
  apiFetch<PowerNode>("/power/generators", { method: "POST", body: JSON.stringify(body) });
export const createPowerPanel = (body: CreatePowerAssetInput) =>
  apiFetch<PowerNode>("/power/power-panels", { method: "POST", body: JSON.stringify(body) });
export const createEquipmentFeed = (equipmentAssetId: string, label: string) =>
  apiFetch<PowerNode>("/power/equipment-feeds", {
    method: "POST",
    body: JSON.stringify({ equipment_asset_id: equipmentAssetId, label }),
  });

export interface CreateConnectionInput {
  source_node_id: string;
  target_node_id: string;
  connection_type?: string;
  feed_label?: string;
  phase?: string;
  voltage?: number;
  rated_current_a?: number;
  status?: string;
}

export const listConnections = (nodeId?: string) =>
  apiFetch<Page<PowerConnection>>(`/power/connections?limit=200${nodeId ? `&node_id=${nodeId}` : ""}`);
export const createConnection = (body: CreateConnectionInput) =>
  apiFetch<PowerConnection>("/power/connections", { method: "POST", body: JSON.stringify(body) });
export const disconnectConnection = (id: string) =>
  apiFetch<PowerConnection>(`/power/connections/${id}/disconnect`, { method: "POST" });

export const getDashboardSummary = (params?: { site_id?: string; building_id?: string; floor_id?: string; room_id?: string }) => {
  const q = new URLSearchParams(params as Record<string, string>).toString();
  return apiFetch<DashboardSummary>(`/dashboard/summary${q ? `?${q}` : ""}`);
};
export const getDashboardExceptions = () => apiFetch<DashboardExceptionItem[]>("/dashboard/exceptions");
