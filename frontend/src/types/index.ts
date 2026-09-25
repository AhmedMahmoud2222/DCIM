export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Organization {
  id: string;
  name: string;
  created_at: string;
}

export interface Site {
  id: string;
  city_id: string;
  code: string;
  name: string;
  timezone: string;
}

export interface ManagedAsset {
  id: string;
  asset_type: string;
  asset_tag: string;
  serial_number: string | null;
  lifecycle_status: string;
  created_at: string;
}

export interface Room {
  id: string;
  floor_id: string;
  code: string;
  name: string;
  room_type: string;
  version: number;
}

// ------------------------------------------------------------------- Catalog

export interface RackModel {
  id: string;
  manufacturer: string;
  model_name: string;
  created_at: string;
}

export interface RackModelRevision {
  id: string;
  rack_model_id: string;
  height_u: number;
  width_mm: number;
  depth_mm: number;
  weight_capacity_kg: number | null;
  created_at: string;
}

export interface EquipmentModel {
  id: string;
  manufacturer: string;
  model_name: string;
  created_at: string;
}

export interface EquipmentModelRevision {
  id: string;
  equipment_model_id: string;
  height_u: number | null;
  width_mm: number | null;
  depth_mm: number | null;
  weight_kg: number | null;
  created_at: string;
}

// ------------------------------------------------------------------- Racks

export interface RackPlacement {
  room_id: string;
  x_mm: number | null;
  y_mm: number | null;
  rotation_deg: number | null;
  effective_from: string;
}

export interface Rack {
  id: string;
  asset_tag: string;
  lifecycle_status: string;
  model_revision_id: string;
  name: string;
  owner: string | null;
  notes: string | null;
  version: number;
  created_at: string;
  placement: RackPlacement | null;
}

export interface ElevationSlot {
  equipment_id: string;
  asset_tag: string;
  hostname: string | null;
  u_start: number;
  u_end: number;
  side: string;
  mounting_method: string | null;
  // Phase 10B: null unless this equipment was instantiated from a published catalog
  // revision — the rack view falls back to the plain colored box whenever this is null.
  catalog_model_revision_id: string | null;
}

export interface RackElevation {
  rack_id: string;
  rack_name: string;
  height_u: number;
  slots: ElevationSlot[];
}

// ------------------------------------------------------------------- Equipment

export const PLACEMENT_TYPES = ["rack_mounted", "floor_standing", "wall_mounted", "ceiling_mounted", "other"] as const;
export type PlacementType = (typeof PLACEMENT_TYPES)[number];

export const SIDES = ["front", "rear", "both"] as const;
export type Side = (typeof SIDES)[number];

export interface EquipmentPlacement {
  placement_type: PlacementType;
  room_id: string;
  rack_id: string | null;
  u_start: number | null;
  u_end: number | null;
  side: Side | null;
  effective_from: string;
}

export interface Equipment {
  id: string;
  asset_tag: string;
  lifecycle_status: string;
  model_revision_id: string;
  // Phase 10B: null unless this equipment was instantiated from a published catalog
  // revision (POST /equipment/instantiate) rather than created via the legacy path.
  catalog_model_revision_id: string | null;
  hostname: string | null;
  owner: string | null;
  service: string | null;
  environment: string | null;
  notes: string | null;
  version: number;
  created_at: string;
  placement: EquipmentPlacement | null;
}

// -------------------------------------------------------- Phase 10B: ports & cabling

export const PORT_CONNECTION_STATUSES = ["active", "planned", "faulted"] as const;
export type PortConnectionStatus = (typeof PORT_CONNECTION_STATUSES)[number];

export interface PortConnection {
  id: string;
  source_port_id: string;
  target_port_id: string | null;
  target_power_node_id: string | null;
  cable_id: string | null;
  status: PortConnectionStatus;
}

export interface EquipmentPort {
  id: string;
  equipment_id: string;
  network_port_template_id: string | null;
  stable_key: string;
  display_name: string;
  media_type: string;
  supported_speeds_mbps: number[];
  connector_type: string;
  role: string;
  side: "front" | "rear";
  module_group: string | null;
  sort_order: number;
  connection: PortConnection | null;
}

export interface EquipmentPowerInlet {
  id: string;
  equipment_id: string;
  power_supply_template_id: string | null;
  power_node_id: string;
  stable_key: string;
  label: string;
  connector_type: string;
  sort_order: number;
}

export interface EquipmentPortsList {
  ports: EquipmentPort[];
  power_inlets: EquipmentPowerInlet[];
}

export interface EquipmentInstantiateResult extends Equipment {
  ports: EquipmentPort[];
  power_inlets: EquipmentPowerInlet[];
}

// ------------------------------------------------------------------- Floor plans / spatial

export const FLOOR_PLAN_STATUSES = ["draft", "active", "superseded"] as const;

export interface FloorPlan {
  id: string;
  room_id: string;
  revision_number: number;
  status: string;
  source_file_name: string | null;
  source_format: string | null;
  calibration_scale_mm_per_px: number | null;
  room_width_mm: number | null;
  room_height_mm: number | null;
  version: number;
  created_at: string;
}

export interface ImportJob {
  id: string;
  floor_plan_id: string;
  status: string;
  original_filename: string;
  file_size_bytes: number;
  rejection_reason: string | null;
  created_at: string;
}

export interface ImportDiagnostics {
  job_id: string;
  source_format: string | null;
  parser_name: string | null;
  objects_discovered: number;
  objects_classified: number;
  racks_detected: number;
  unsupported_object_count: number;
  warnings: string[];
  errors: string[];
  ambiguous_count: number;
  confirmed_count: number;
  duration_ms: number | null;
}

export interface ImportCandidate {
  id: string;
  job_id: string;
  raw_geometry: { shape_type: string; x: number; y: number; width?: number; height?: number; radius?: number; text?: string };
  suggested_object_type: string | null;
  suggested_label: string | null;
  confidence: number | null;
  status: string;
  resulting_spatial_object_id: string | null;
}

export interface SpatialObject {
  id: string;
  object_type: string;
  geometry_type: string;
  x_mm: number;
  y_mm: number;
  width_mm: number | null;
  height_mm: number | null;
  rotation_deg: number;
  label: string | null;
  source: string;
}

export interface RoomRack {
  id: string;
  asset_tag: string;
  name: string;
  x_mm: number | null;
  y_mm: number | null;
  rotation_deg: number | null;
  spatial_object_id: string | null;
}

export interface RoomEquipment {
  id: string;
  asset_tag: string;
  hostname: string | null;
  placement_type: string;
  spatial_object_id: string | null;
}

export interface RoomSpatialView {
  room_id: string;
  room_name: string;
  active_floor_plan_id: string | null;
  active_floor_plan_revision: number | null;
  room_width_mm: number | null;
  room_height_mm: number | null;
  generated_at: string;
  racks: RoomRack[];
  equipment: RoomEquipment[];
  objects: SpatialObject[];
}

// --------------------------------------------------------------------- Power

export interface PowerNode {
  id: string;
  node_type: string;
  managed_asset_id: string | null;
  owning_asset_id: string | null;
  label: string;
  retired_at: string | null;
  created_at: string;
}

export interface PowerConnection {
  id: string;
  source_node_id: string;
  target_node_id: string;
  connection_type: string;
  feed_label: string;
  phase: string | null;
  voltage: number | null;
  rated_current_a: number | null;
  status: string;
  version: number;
  effective_from: string;
  effective_to: string | null;
}

export interface TopologyNode {
  node_id: string;
  label: string;
  node_type: string;
}

export interface CapacityFigures {
  power_node_id: string;
  rated_capacity_kw: number | null;
  configured_capacity_kw: number | null;
  effective_capacity_kw: number | null;
  allocated_kw: number | null;
  available_kw: number | null;
  utilization_pct: number | null;
  measured_load_kw: number | null;
  data_quality: "known" | "unknown" | "not_applicable";
  redundancy_factor: string | null;
  warning_threshold_pct: number | null;
  critical_threshold_pct: number | null;
  version: number | null;
}

export interface CapacityException {
  code: string;
  severity: "critical" | "warning" | "info";
  power_node_id: string;
  label: string;
  message: string;
  observed_value: number | null;
  threshold: number | null;
}

export interface EquipmentPowerFeed {
  power_node_id: string;
  feed_label: string | null;
  has_upstream_path: boolean;
  effective_capacity_kw: number | null;
}

export interface EquipmentPowerSummary {
  equipment_asset_id: string;
  feed_nodes: EquipmentPowerFeed[];
  redundancy_classification: "dual_feed_healthy" | "single_feed" | "degraded" | "no_power_modeled";
  effective_demand_kw: number | null;
  data_quality: string;
}

// ------------------------------------------------ Phase 10C: telemetry bindings + impact

export const LINK_STATES = ["UP", "DOWN", "DEGRADED"] as const;
export type LinkState = (typeof LINK_STATES)[number];
export const THRESHOLD_STATUS_LEVELS = ["NORMAL", "WARNING", "CRITICAL"] as const;
export type ThresholdStatusLevel = (typeof THRESHOLD_STATUS_LEVELS)[number];
export type PortStatusLevel = LinkState | ThresholdStatusLevel;

export interface NetworkPortStatusPayload {
  link_state: LinkState;
  bandwidth_util_pct: number;
  error_rate_pct: number;
}

export interface PowerInletStatusPayload {
  current_amps: number;
  active_power_watts: number;
  voltage: number;
}

export interface EnvironmentalStatusPayload {
  temperature_celsius: number;
  humidity_pct: number;
}

export interface LatestPortStatus {
  binding_id: string;
  equipment_id: string;
  target_type: "network_port" | "power_inlet" | "environmental";
  equipment_port_id: string | null;
  equipment_power_inlet_id: string | null;
  label: string | null;
  status_level: PortStatusLevel | null;
  payload: NetworkPortStatusPayload | PowerInletStatusPayload | EnvironmentalStatusPayload | null;
  sampled_at: string | null;
  received_at: string | null;
}

export interface ImpactedEquipmentItem {
  equipment_id: string;
  asset_tag: string;
  hostname: string | null;
  service: string | null;
  hop: number;
  impact_type: "power_loss" | "degraded_redundancy" | "network_isolated" | "network_degraded";
  message: string;
}

export interface ImpactSimulationResult {
  target_type: "power_node" | "network_port";
  target_id: string;
  directly_impacted: ImpactedEquipmentItem[];
  indirectly_impacted: ImpactedEquipmentItem[];
  lost_redundancy_paths: string[];
  affected_services: string[];
}

// --------------------------------------------------------- Integrations / Collectors (Phase 8)

export interface Collector {
  id: string;
  name: string;
  collector_type: "central" | "edge";
  site_id: string | null;
  status: "registered" | "active" | "disabled";
  version_string: string | null;
  health: "healthy" | "stale" | "offline";
  last_heartbeat_at: string | null;
  seconds_since_heartbeat: number | null;
}

export interface CollectorWithSecret extends Collector {
  secret: string;
}

export interface CollectorCapability {
  protocol_code: string;
}

export interface CollectorAssignment {
  id: string;
  collector_id: string;
  integration_id: string;
  effective_from: string;
  effective_to: string | null;
}

export interface Integration {
  id: string;
  name: string;
  integration_type: string;
  site_id: string | null;
  target_host: string;
  target_port: number | null;
  config: Record<string, unknown>;
  enabled: boolean;
  poll_interval_seconds: number;
  last_poll_at: string | null;
  last_success_at: string | null;
  last_failure_at: string | null;
  consecutive_failures: number;
  has_credential: boolean;
  assigned_collector_id: string | null;
  version: number;
}

export interface DiscoveredDevice {
  id: string;
  integration_id: string;
  external_identifier: string;
  status: "new" | "reconciled" | "ignored";
  matched_managed_asset_id: string | null;
  raw_attributes: Record<string, unknown>;
}

export interface ReconciliationDiff {
  id: string;
  discovered_device_id: string;
  diff_type: string;
  status: "pending" | "accepted" | "rejected";
  field_name: string | null;
  discovered_value: string | null;
  authoritative_value: string | null;
}

export interface DashboardSummary {
  site_summary: {
    sites: number;
    buildings: number;
    floors: number;
    rooms: number;
    racks: number;
    equipment: number;
    power_nodes: number;
  };
  capacity_summary: {
    total_configured_kw: number | null;
    total_allocated_kw: number | null;
    total_available_kw: number | null;
    utilization_pct: number | null;
    nodes_with_known_capacity: number;
    nodes_with_unknown_capacity: number;
  };
  rack_summary: {
    total_racks: number;
    occupied_racks: number;
    available_racks: number;
  };
  power_summary: {
    total_power_nodes: number;
    overloaded_nodes: number;
    near_capacity_nodes: number;
    missing_power_path_equipment: number;
    redundancy_degraded_equipment: number;
  };
}

export interface DashboardExceptionItem {
  code: string;
  severity: "critical" | "warning" | "info";
  object_type: string;
  object_id: string;
  message: string;
}

// ------------------------------------------------------ Catalog Designer (Phase 10A)

export const CATALOG_CATEGORIES = ["rack", "equipment", "network_device", "pdu", "ups", "power_panel", "sensor"] as const;
export type CatalogCategory = (typeof CATALOG_CATEGORIES)[number];

export interface Manufacturer {
  id: string;
  name: string;
  status: string;
  created_at: string;
}

export interface CatalogModelRevisionSummary {
  id: string;
  revision_number: number;
  lifecycle_status: string;
  published_at: string | null;
  retired_at: string | null;
}

export interface CatalogModel {
  id: string;
  manufacturer_id: string;
  category: string;
  subtype: string | null;
  model_name: string;
  model_number: string | null;
  description: string | null;
  tags: string[];
  status: string;
  created_at: string;
}

export interface CatalogModelDetail extends CatalogModel {
  revisions: CatalogModelRevisionSummary[];
}

export interface CatalogModelRevision {
  id: string;
  catalog_model_id: string;
  revision_number: number;
  lifecycle_status: string;
  dimension_unit: string | null;
  width_value: number | null;
  height_value: number | null;
  depth_value: number | null;
  rack_unit_height: number | null;
  weight_unit: string | null;
  weight_value: number | null;
  mounting_orientation: string | null;
  supported_placement_types: string[] | null;
  airflow_direction: string | null;
  rated_power_w: number | null;
  typical_power_w: number | null;
  max_power_w: number | null;
  heat_dissipation_btu_hr: number | null;
  power_redundancy_mode: string | null;
  cloned_from_revision_id: string | null;
  created_by_user_id: string;
  published_at: string | null;
  published_by_user_id: string | null;
  retired_at: string | null;
  retired_by_user_id: string | null;
  retirement_reason: string | null;
  allow_installation_when_retired: boolean;
  version: number;
  created_at: string;
}

export interface NetworkPortTemplate {
  id: string;
  catalog_model_revision_id: string;
  revision_version: number;
  stable_key: string;
  display_name: string;
  numbering_pattern: string | null;
  media_type: string;
  supported_speeds_mbps: number[];
  connector_type: string;
  role: string;
  side: string;
  module_group: string | null;
  sort_order: number;
}

export interface PowerSupplyTemplate {
  id: string;
  catalog_model_revision_id: string;
  revision_version: number;
  stable_key: string;
  label: string;
  quantity: number;
  redundancy_mode: string;
  connector_type: string;
  rated_voltage_min: number | null;
  rated_voltage_max: number | null;
  rated_frequency_hz: number | null;
  rated_current_a: number | null;
  hot_swappable: boolean | null;
  sort_order: number;
}

export interface MonitoringMetricTemplate {
  id: string;
  catalog_model_revision_id: string;
  revision_version: number;
  stable_key: string;
  protocol: string;
  protocol_other_label: string | null;
  metric_name: string;
  oid: string | null;
  value_type: string;
  unit: string | null;
  scale: number;
  transform: string;
  offset: number | null;
  default_collection_interval_seconds: number | null;
  default_warning_threshold: number | null;
  default_critical_threshold: number | null;
  sort_order: number;
}

export interface CatalogGraphicMarker {
  id: string;
  catalog_graphic_id: string;
  revision_version: number;
  marker_type: "network_port" | "power_supply" | "module" | "other";
  network_port_template_id: string | null;
  power_supply_template_id: string | null;
  label: string | null;
  marker_x: number;
  marker_y: number;
  sort_order: number;
}

export interface CatalogGraphic {
  id: string;
  catalog_model_revision_id: string;
  revision_version: number;
  side: "front" | "rear";
  original_filename: string;
  mime_type: string;
  file_size_bytes: number;
  width_px: number;
  height_px: number;
  uploaded_at: string;
  markers: CatalogGraphicMarker[];
}

export interface CatalogModelRevisionDetail extends CatalogModelRevision {
  network_ports: NetworkPortTemplate[];
  power_supplies: PowerSupplyTemplate[];
  monitoring_metrics: MonitoringMetricTemplate[];
  graphics: CatalogGraphic[];
}

export interface ValidationIssue {
  field: string;
  code: string;
  message: string;
}

export interface ValidationSummary {
  valid: boolean;
  errors: ValidationIssue[];
  warnings: ValidationIssue[];
}

export interface CatalogFieldDiff {
  field: string;
  left: unknown;
  right: unknown;
}

export interface CatalogChildDiff {
  collection: string;
  stable_key: string;
  change: "added" | "removed" | "changed";
  left: Record<string, unknown> | null;
  right: Record<string, unknown> | null;
}

export interface CatalogCompare {
  left_revision_id: string;
  right_revision_id: string;
  field_diffs: CatalogFieldDiff[];
  child_diffs: CatalogChildDiff[];
}
