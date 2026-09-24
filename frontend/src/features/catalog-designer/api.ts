import { apiFetch } from "@/lib/apiClient";
import {
  CatalogCompare,
  CatalogModel,
  CatalogModelDetail,
  CatalogModelRevision,
  CatalogModelRevisionDetail,
  Manufacturer,
  MonitoringMetricTemplate,
  NetworkPortTemplate,
  Page,
  PowerSupplyTemplate,
  ValidationSummary,
} from "@/types";

// --------------------------------------------------------------------- Manufacturers

export const listManufacturers = (q?: string) =>
  apiFetch<Page<Manufacturer>>(`/catalog/manufacturers?limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`);

export const getManufacturer = (id: string) => apiFetch<Manufacturer>(`/catalog/manufacturers/${id}`);

export const createManufacturer = (name: string) =>
  apiFetch<Manufacturer>("/catalog/manufacturers", { method: "POST", body: JSON.stringify({ name }) });

// --------------------------------------------------------------------------- Models

export interface CatalogModelListFilters {
  category?: string;
  status?: string;
  manufacturer_id?: string;
  q?: string;
}

export const listCatalogModels = (filters: CatalogModelListFilters = {}) => {
  const params = new URLSearchParams({ limit: "200" });
  if (filters.category) params.set("category", filters.category);
  if (filters.status) params.set("status", filters.status);
  if (filters.manufacturer_id) params.set("manufacturer_id", filters.manufacturer_id);
  if (filters.q) params.set("q", filters.q);
  return apiFetch<Page<CatalogModel>>(`/catalog/models?${params.toString()}`);
};

export const getCatalogModel = (id: string) => apiFetch<CatalogModelDetail>(`/catalog/models/${id}`);

export interface CreateCatalogModelInput {
  manufacturer_id: string;
  category: string;
  subtype?: string;
  model_name: string;
  model_number?: string;
  description?: string;
  tags?: string[];
}

export const createCatalogModel = (body: CreateCatalogModelInput) =>
  apiFetch<CatalogModel>("/catalog/models", { method: "POST", body: JSON.stringify(body) });

export interface UpdateCatalogModelMetadataInput {
  description?: string | null;
  tags?: string[];
  status?: string;
}

export const updateCatalogModelMetadata = (id: string, body: UpdateCatalogModelMetadataInput) =>
  apiFetch<CatalogModel>(`/catalog/models/${id}`, { method: "PATCH", body: JSON.stringify(body) });

// ------------------------------------------------------------------------ Revisions

export const getRevision = (id: string) => apiFetch<CatalogModelRevisionDetail>(`/catalog/revisions/${id}`);

export const createDraftRevision = (modelId: string) =>
  apiFetch<CatalogModelRevision>(`/catalog/models/${modelId}/revisions`, { method: "POST" });

export const cloneRevision = (modelId: string, fromRevisionId: string) =>
  apiFetch<CatalogModelRevision>(`/catalog/models/${modelId}/revisions/clone?from_revision_id=${fromRevisionId}`, {
    method: "POST",
  });

export interface UpdateRevisionInput {
  dimension_unit?: string;
  width_value?: number;
  height_value?: number;
  depth_value?: number;
  rack_unit_height?: number;
  weight_unit?: string;
  weight_value?: number;
  mounting_orientation?: string;
  supported_placement_types?: string[];
  airflow_direction?: string;
  rated_power_w?: number;
  typical_power_w?: number;
  max_power_w?: number;
  heat_dissipation_btu_hr?: number;
  power_redundancy_mode?: string;
}

export const updateRevision = (id: string, body: UpdateRevisionInput, ifMatch: number) =>
  apiFetch<CatalogModelRevision>(`/catalog/revisions/${id}`, { method: "PATCH", body: JSON.stringify(body), ifMatch });

export const deleteDraftRevision = (id: string, ifMatch: number) =>
  apiFetch<void>(`/catalog/revisions/${id}`, { method: "DELETE", ifMatch });

export const validateRevision = (id: string) =>
  apiFetch<ValidationSummary>(`/catalog/revisions/${id}/validate`, { method: "POST" });

export const publishRevision = (id: string, reason?: string) =>
  apiFetch<CatalogModelRevision | ValidationSummary>(
    `/catalog/revisions/${id}/publish${reason ? `?reason=${encodeURIComponent(reason)}` : ""}`,
    { method: "POST" },
  );

export interface RetireRevisionInput {
  reason: string;
  allow_installation_when_retired?: boolean;
}

export const retireRevision = (id: string, body: RetireRevisionInput) =>
  apiFetch<CatalogModelRevision>(`/catalog/revisions/${id}/retire`, { method: "POST", body: JSON.stringify(body) });

export interface RetireOverrideInput {
  allow_installation_when_retired: boolean;
  reason: string;
}

export const updateRetireOverride = (id: string, body: RetireOverrideInput) =>
  apiFetch<CatalogModelRevision>(`/catalog/revisions/${id}/retire-override`, { method: "PATCH", body: JSON.stringify(body) });

export const compareRevisions = (leftId: string, rightId: string) =>
  apiFetch<CatalogCompare>(`/catalog/revisions/compare?left=${leftId}&right=${rightId}`);

// -------------------------------------------------------------------- Network ports

export interface NetworkPortTemplateInput {
  stable_key: string;
  display_name: string;
  numbering_pattern?: string;
  media_type: string;
  supported_speeds_mbps: number[];
  connector_type: string;
  role?: string;
  side: string;
  module_group?: string;
  sort_order?: number;
}

export const createNetworkPort = (revisionId: string, body: NetworkPortTemplateInput, ifMatch: number) =>
  apiFetch<NetworkPortTemplate>(`/catalog/revisions/${revisionId}/network-ports`, {
    method: "POST",
    body: JSON.stringify(body),
    ifMatch,
  });

export const deleteNetworkPort = (revisionId: string, portId: string, ifMatch: number) =>
  apiFetch<void>(`/catalog/revisions/${revisionId}/network-ports/${portId}`, { method: "DELETE", ifMatch });

// ----------------------------------------------------------------- Power supplies

export interface PowerSupplyTemplateInput {
  stable_key: string;
  label: string;
  quantity?: number;
  redundancy_mode?: string;
  connector_type: string;
  rated_voltage_min?: number;
  rated_voltage_max?: number;
  rated_frequency_hz?: number;
  rated_current_a?: number;
  hot_swappable?: boolean;
  sort_order?: number;
}

export const createPowerSupply = (revisionId: string, body: PowerSupplyTemplateInput, ifMatch: number) =>
  apiFetch<PowerSupplyTemplate>(`/catalog/revisions/${revisionId}/power-supplies`, {
    method: "POST",
    body: JSON.stringify(body),
    ifMatch,
  });

export const deletePowerSupply = (revisionId: string, psuId: string, ifMatch: number) =>
  apiFetch<void>(`/catalog/revisions/${revisionId}/power-supplies/${psuId}`, { method: "DELETE", ifMatch });

// ------------------------------------------------------------- Monitoring metrics

export interface MonitoringMetricTemplateInput {
  stable_key: string;
  protocol: string;
  protocol_other_label?: string;
  metric_name: string;
  oid?: string;
  value_type: string;
  unit?: string;
  scale?: number;
  transform?: string;
  offset?: number;
  default_collection_interval_seconds?: number;
  default_warning_threshold?: number;
  default_critical_threshold?: number;
  sort_order?: number;
}

export const createMonitoringMetric = (revisionId: string, body: MonitoringMetricTemplateInput, ifMatch: number) =>
  apiFetch<MonitoringMetricTemplate>(`/catalog/revisions/${revisionId}/monitoring-metrics`, {
    method: "POST",
    body: JSON.stringify(body),
    ifMatch,
  });

export const deleteMonitoringMetric = (revisionId: string, metricId: string, ifMatch: number) =>
  apiFetch<void>(`/catalog/revisions/${revisionId}/monitoring-metrics/${metricId}`, { method: "DELETE", ifMatch });
