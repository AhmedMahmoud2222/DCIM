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
  hostname: string | null;
  owner: string | null;
  service: string | null;
  environment: string | null;
  notes: string | null;
  version: number;
  created_at: string;
  placement: EquipmentPlacement | null;
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
