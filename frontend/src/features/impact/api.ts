import { apiFetch } from "@/lib/apiClient";
import { ImpactSimulationResult } from "@/types";

export interface SimulateImpactInput {
  target_type: "power_node" | "network_port";
  target_id: string;
}

export const simulateImpact = (body: SimulateImpactInput) =>
  apiFetch<ImpactSimulationResult>("/impact/simulate", { method: "POST", body: JSON.stringify(body) });
