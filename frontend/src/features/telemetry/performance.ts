import { apiFetch } from "@/lib/apiClient";

export interface PerformanceBucket {
  start: string;
  end: string;
  minimum: number;
  maximum: number;
  average: number;
  sample_count: number;
  resolution: "raw" | "daily" | "mixed";
}

export interface PerformanceSeries {
  series_key: string;
  external_identifier: string;
  integration_id: string;
  metric: string;
  unit: string;
  registry_version: string | null;
  sample_count: number;
  minimum: number | null;
  maximum: number | null;
  average: number | null;
  p50: number | null;
  p95: number | null;
  partial_days: number;
  points: PerformanceBucket[];
}

export function getPerformance(asset: string, site: string, metric: string, start: string, end: string) {
  const params = new URLSearchParams({ managed_asset_id: asset, metric, start, end });
  if (site) params.set("site_id", site);
  return apiFetch<{ series: PerformanceSeries[] }>(`/telemetry/performance?${params}`);
}

function csvCell(value: string | number | null) {
  if (typeof value === "number") return String(value);
  let text = value ?? "";
  if (/^\s*[=+@-]|^[\t\r\n]/.test(text)) text = "'" + text;
  return '"' + text.replace(/"/g, '""') + '"';
}

export function performanceCsv(series: PerformanceSeries[], asset: string, site: string, start: string, end: string) {
  const header = ["equipment_id","site_filter","window_start","window_end","integration_id","series_key","source","registry_version","metric","unit","bucket_start","bucket_end","resolution","sample_count","minimum","maximum","average","excluded_partial_days","quality"];
  const lines = series.flatMap(s => {
    const identity = [asset,site,start,end,s.integration_id,s.series_key,s.external_identifier,s.registry_version,s.metric,s.unit];
    if (!s.points.length) return [[...identity,null,null,null,0,null,null,null,s.partial_days,"no eligible samples"]];
    return s.points.map(p => [...identity,p.start,p.end,p.resolution,p.sample_count,p.minimum,p.maximum,p.average,s.partial_days,s.partial_days ? "partial window" : "observed"]);
  });
  return [header, ...lines].map(row => row.map(csvCell).join(",")).join("\r\n");
}
