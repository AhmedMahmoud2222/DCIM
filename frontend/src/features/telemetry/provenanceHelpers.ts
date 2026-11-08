import { TelemetryReading } from "@/features/telemetry/api";

const labels = {
  pinned: "Pinned", inferred: "Inferred", operator_resolved: "Operator resolved",
  unverified_registry: "Unverified registry", unverified_legacy: "Unverified legacy",
};

export function provenanceLabel(point: TelemetryReading): string {
  if (point.resolution === "daily") return "Aggregate provenance pending";
  return point.provenance_status ? labels[point.provenance_status] : "Provenance unavailable";
}

/** Keep distinct stored contracts separate. No conversion or precision claim is made here. */
export function groupHistoryPoints(points: TelemetryReading[]): TelemetryReading[][] {
  const groups = new Map<string, TelemetryReading[]>();
  for (const point of points) {
    const key = JSON.stringify([
      point.series_key ?? point.id, point.provenance_status ?? "unavailable", point.unit,
      point.registry_version ?? null, point.presentation_unit, point.resolution ?? "raw",
      point.resolution === "daily" ? null : point.mapping_revision_id ?? null,
    ]);
    const group = groups.get(key) ?? [];
    group.push(point);
    groups.set(key, group);
  }
  return [...groups.values()];
}
