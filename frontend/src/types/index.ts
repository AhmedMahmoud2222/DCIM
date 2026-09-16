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
