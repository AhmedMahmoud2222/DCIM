import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import { RoomSpatialCanvas } from "@/features/floor-plans/RoomSpatialCanvas";
import type { Point } from "@/features/floor-plans/spatialMath";
import { ApiError } from "@/lib/apiClient";
import type { CapacityZone, CoolingLayout, HeatMap, RoomAirflow, RoomSpatialView, ThermalException, ThermalMetric } from "@/types";
import { getCoolingLayout, getHeatMap, getRoomAirflow, getRoomCapacity, getRoomExceptions } from "./api";
import { ThermalHeatLayer, ThermalMarkerLayers, type ThermalLayerToggles, type ThermalSelection } from "./ThermalLayers";
import { ThermalLegend } from "./ThermalLegend";
import { MAP_STATE_META, SENSOR_STATE_META, cellAt, describeSensor, explainExclusion, explainReason, formatAge, formatQuantity, metricLabel } from "./thermalMath";

const REFRESH_MS = 30_000;
const messageOf = (e: unknown): string => (e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));

const live = { staleTime: 0, refetchOnMount: "always" as const, refetchInterval: REFRESH_MS, retry: false };

function Section({ title, id, children }: { title: string; id: string; children: React.ReactNode }) {
  return (
    <section aria-labelledby={id} className="rounded-sm border border-slate-800 bg-slate-950/40 p-3">
      <h3 id={id} className="mb-2 text-sm font-semibold text-slate-300">{title}</h3>
      {children}
    </section>
  );
}

function QueryNote({ isLoading, error, what }: { isLoading: boolean; error: unknown; what: string }) {
  if (isLoading) return <p role="status" className="text-xs text-slate-500">Loading {what}…</p>;
  if (error) return <p role="alert" className="text-xs text-red-400">Could not load {what}: {messageOf(error)}</p>;
  return null;
}

const LEVEL_STYLE: Record<CapacityZone["level"], string> = {
  ok: "bg-green-900 text-green-100",
  warning: "bg-yellow-900 text-yellow-100",
  critical: "bg-red-900 text-red-100",
  unknown: "bg-slate-700 text-slate-200",
  not_configured: "bg-slate-800 text-slate-300",
};
const LEVEL_GLYPH: Record<CapacityZone["level"], string> = { ok: "✓", warning: "!", critical: "✕", unknown: "?", not_configured: "–" };
const SEVERITY_STYLE: Record<ThermalException["severity"], string> = { critical: "bg-red-900 text-red-100", warning: "bg-yellow-900 text-yellow-100", info: "bg-slate-700 text-slate-200" };
const SEVERITY_GLYPH: Record<ThermalException["severity"], string> = { critical: "✕", warning: "!", info: "i" };

function clipOf(view: RoomSpatialView): Point[] | null {
  const boundary = view.boundary;
  const pts = boundary?.geometry_data?.points;
  if (pts && pts.length >= 3) return pts as Point[];
  if (boundary?.width_mm && boundary.height_mm) {
    const { x_mm: x, y_mm: y, width_mm: w, height_mm: h } = boundary;
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]];
  }
  if (view.room_width_mm && view.room_height_mm) return [[0, 0], [view.room_width_mm, 0], [view.room_width_mm, view.room_height_mm], [0, view.room_height_mm]];
  return null;
}

function MapStatus({ map }: { map: HeatMap }) {
  const meta = MAP_STATE_META[map.state];
  const q = map.quality;
  return (
    <div className="rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs" data-testid="map-quality" data-map-state={map.state}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className={`rounded-sm px-2 py-0.5 font-medium ${meta.classes}`} data-testid="map-state-badge">
          <span aria-hidden="true">{meta.glyph}</span> {metricLabel(map.metric)} map: {meta.label}
        </span>
        <span className="text-slate-400" data-testid="map-snapshot">
          Snapshot as of {new Date(map.as_of).toLocaleString()} · generated {new Date(map.generated_at).toLocaleTimeString()}
        </span>
      </div>
      {map.state_reasons.length > 0 && (
        <ul className="mb-2 list-disc pl-5 text-yellow-300" data-testid="map-reasons">
          {map.state_reasons.map((r) => (
            <li key={r}>{explainReason(r)}</li>
          ))}
        </ul>
      )}
      <dl className="grid grid-cols-2 gap-x-4 gap-y-0.5 sm:grid-cols-4" data-testid="map-counts">
        <div><dt className="text-slate-500">Sensors expected</dt><dd data-testid="count-total">{q.sensor_count}</dd></div>
        <div><dt className="text-slate-500">Fresh</dt><dd data-testid="count-fresh">{q.fresh_count}</dd></div>
        <div><dt className="text-slate-500">Stale</dt><dd data-testid="count-stale">{q.stale_count}</dd></div>
        <div><dt className="text-slate-500">Missing</dt><dd data-testid="count-missing">{q.missing_count}</dd></div>
        <div><dt className="text-slate-500">Invalid / unlocated</dt><dd>{q.invalid_count} / {q.unlocated_count}</dd></div>
        <div><dt className="text-slate-500">Interpolation coverage</dt><dd data-testid="coverage">{q.coverage_percent}% ({q.coverage_class})</dd></div>
        <div><dt className="text-slate-500">Age skew</dt><dd>{q.max_age_skew_seconds}s of {q.max_age_skew_allowed_seconds}s allowed</dd></div>
        <div><dt className="text-slate-500">Freshness rule</dt><dd>{q.freshness_cutoff}</dd></div>
      </dl>
      <p className="mt-2 text-slate-400" data-testid="map-disclaimer">{map.disclaimer}</p>
      <details className="mt-1 text-slate-500">
        <summary className="cursor-pointer">Method and assumptions</summary>
        <p className="mt-1">
          Inverse-distance weighting (power {map.method.power}), influence radius {map.method.radius_mm} mm, cell size {map.method.cell_mm ?? "n/a"} mm, at least{" "}
          {map.method.min_sensors} fresh sensors, stale and missing sensors excluded.
        </p>
        <ul className="mt-1 list-disc pl-5">
          {map.method.assumptions.map((a) => (
            <li key={a}>{a}</li>
          ))}
        </ul>
      </details>
    </div>
  );
}

function Details({ selection, map, layout, airflow, capacity }: { selection: ThermalSelection; map?: HeatMap; layout?: CoolingLayout; airflow?: RoomAirflow; capacity?: CapacityZone[] }) {
  let body: React.ReactNode = <p className="text-slate-500">Select a sensor, a cooling unit, an airflow arrow, or a heat-map cell (click it, or focus the field and use the arrow keys).</p>;
  if (selection?.kind === "sensor") {
    const p = map?.sensors.find((s) => s.sensor_id === selection.id);
    if (p) {
      const meta = SENSOR_STATE_META[p.state];
      body = (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1" data-testid="detail-sensor">
          <dt className="text-slate-500">Sensor</dt><dd>{p.name} ({p.asset_tag})</dd>
          <dt className="text-slate-500">Provenance</dt><dd data-testid="detail-provenance">Measured</dd>
          <dt className="text-slate-500">State</dt><dd data-testid="detail-state"><span aria-hidden="true">{meta.glyph}</span> {meta.label}</dd>
          <dt className="text-slate-500">Value</dt><dd data-testid="detail-value">{p.presentation_value != null ? formatQuantity(p.presentation_value, p.presentation_unit, 2) : "No value"}{p.state === "measured_stale" && " (stale: not current)"}</dd>
          <dt className="text-slate-500">Timestamp</dt><dd>{p.occurred_at ? `${new Date(p.occurred_at).toLocaleString()} (${formatAge(p.age_seconds)})` : "never"}</dd>
          <dt className="text-slate-500">Poll interval</dt><dd>{p.expected_poll_interval_seconds != null ? `${p.expected_poll_interval_seconds} s` : "unknown"}</dd>
          <dt className="text-slate-500">Position</dt><dd>{p.x_mm != null ? `${Math.round(p.x_mm)}, ${Math.round(p.y_mm ?? 0)} mm${p.position_exact ? "" : " · approximate (rack footprint)"}` : "not recorded"}</dd>
          <dt className="text-slate-500">Role / kind</dt><dd>{p.measurement_role} · {p.sensor_kind}</dd>
          <dt className="text-slate-500">In the field</dt><dd data-testid="detail-used">{p.used_in_field ? "Yes, contributes to the interpolation" : `No (${explainExclusion(p.excluded_reason) || "not contributing"})`}</dd>
          {(p.active_alarm_count ?? 0) > 0 && (<><dt className="text-slate-500">Alarms</dt><dd className="text-red-300">{p.active_alarm_count} active</dd></>)}
          {p.source && (<><dt className="text-slate-500">Source</dt><dd>{p.source.integration_name}</dd></>)}
        </dl>
      );
    }
  } else if (selection?.kind === "cell" && map?.grid) {
    const cell = cellAt(map.grid, map.grid.origin_x_mm + selection.column * map.grid.cell_mm + 1, map.grid.origin_y_mm + selection.row * map.grid.cell_mm + 1);
    body = cell ? (
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1" data-testid="detail-cell">
        <dt className="text-slate-500">Cell</dt><dd>Column {cell.column + 1}, row {cell.row + 1}</dd>
        <dt className="text-slate-500">Provenance</dt><dd data-testid="detail-provenance">Interpolated (not measured)</dd>
        <dt className="text-slate-500">Value</dt><dd data-testid="detail-value">≈ {formatQuantity(cell.value, map.presentation_unit, 2)}</dd>
        <dt className="text-slate-500">Centre</dt><dd>{Math.round(cell.xMm + cell.sizeMm / 2)}, {Math.round(cell.yMm + cell.sizeMm / 2)} mm</dd>
        <dt className="text-slate-500">Supporting sensors</dt><dd>{cell.support} fresh sensor(s) within {map.method.radius_mm} mm</dd>
        <dt className="text-slate-500">Method</dt><dd>Inverse-distance weighting; an estimate between sensors, never a reading</dd>
      </dl>
    ) : (
      <p data-testid="detail-cell-empty" className="text-slate-400">This cell has no value: fewer than two fresh sensors are within reach, or it lies outside the room. Nothing is estimated here.</p>
    );
  } else if (selection?.kind === "unit") {
    const unit = layout?.cooling_units.find((u) => u.id === selection.id);
    const capacityUnit = capacity?.flatMap((z) => [...z.units, ...z.plant.units]).find((u) => u.id === selection.id);
    if (unit) {
      body = (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1" data-testid="detail-unit">
          <dt className="text-slate-500">Cooling unit</dt><dd>{unit.name} ({unit.asset_tag})</dd>
          <dt className="text-slate-500">Type</dt><dd>{unit.unit_kind.toUpperCase()}</dd>
          <dt className="text-slate-500">Status</dt><dd>{unit.operating_status} · lifecycle {unit.lifecycle_status}</dd>
          <dt className="text-slate-500">Rated capacity</dt><dd>{unit.rated_cooling_capacity_kw != null ? `${unit.rated_cooling_capacity_kw} kW` : "Unknown"}</dd>
          <dt className="text-slate-500">Effective capacity</dt><dd>{capacityUnit ? (capacityUnit.effective_kw != null ? `${capacityUnit.effective_kw} kW` : "Unknown") : "Not assigned to a served zone"}</dd>
          <dt className="text-slate-500">Supply direction</dt><dd>{unit.supply_direction_deg != null ? `${unit.supply_direction_deg}° (configured)` : "Not configured"}</dd>
          <dt className="text-slate-500">Footprint</dt><dd>Not modelled; the marker is not to scale</dd>
        </dl>
      );
    }
  } else if (selection?.kind === "airflow") {
    const e = airflow?.elements.find((x) => x.id === selection.id);
    if (e) {
      body = (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1" data-testid="detail-airflow">
          <dt className="text-slate-500">Element</dt><dd>{e.name}</dd>
          <dt className="text-slate-500">Magnitude</dt><dd data-testid="detail-provenance">{e.magnitude_provenance === "measured" ? "Measured" : e.magnitude_provenance === "configured_design" ? "Configured design airflow" : "None"}</dd>
          <dt className="text-slate-500">Direction</dt><dd>{e.direction_deg}° · configured orientation, not a measured flow path</dd>
          <dt className="text-slate-500">Modelled</dt><dd>No. This is not a CFD result.</dd>
        </dl>
      );
    }
  }
  return (
    <div className="rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs" aria-live="polite" data-testid="selected-detail">
      <h3 className="mb-2 text-sm font-semibold text-slate-300">Selected item</h3>
      {body}
    </div>
  );
}

/** A load is a number only when it is complete. An incomplete one says how many placed items have no known demand and shows
 * the known part as "at least", never as the room total. */
function loadText(z: CapacityZone): string {
  const load = z.thermal_load;
  const pending = load.pending_equipment_count ?? 0;
  const pendingNote = pending > 0 ? ` · ${pending} planned or reserved not counted` : "";
  if (load.thermal_kw != null) return `${load.thermal_kw} kW (${load.quality ?? "quality unknown"})${pendingNote}`;
  const state = load.state.replace(/_/g, " ");
  const unknown = load.unknown_demand_equipment_count ?? 0;
  const placed = load.placed_equipment_count;
  const parts = [`unknown (${state})`];
  if (unknown > 0) parts.push(`${unknown}${placed != null ? ` of ${placed}` : ""} placed equipment without a known demand`);
  if (load.thermal_kw_lower_bound != null) parts.push(`at least ${load.thermal_kw_lower_bound} kW known, not a total`);
  return parts.join(", ") + pendingNote;
}

function headroomText(z: CapacityZone): string {
  if (z.headroom_kw != null) return `${z.headroom_kw} kW (${z.utilization_pct}% used)`;
  if (z.headroom_upper_bound_kw != null) return `not calculable (unverified), at most ${z.headroom_upper_bound_kw} kW`;
  return "not calculable";
}

function redundancyText(z: CapacityZone): string {
  const base = z.redundancy.state.replace(/_/g, " ");
  const incomplete = z.redundancy.pools.some((p) => p.verification_unavailable_reason === "incomplete_load");
  return incomplete ? `${base} (N+1 not verified: load incomplete)` : base;
}

export function CoolingEnvironmentPanel({ roomId, view }: { roomId: string; view: RoomSpatialView }) {
  const [metric, setMetric] = useState<ThermalMetric>("temperature_c");
  const [toggles, setToggles] = useState<ThermalLayerToggles>({ heat: true, sensors: true, airflow: true, zones: true, units: true });
  const [selection, setSelection] = useState<ThermalSelection>(null);

  const layoutQuery = useQuery({ queryKey: ["cooling", roomId, "layout"], queryFn: () => getCoolingLayout(roomId), ...live });
  const mapQuery = useQuery({ queryKey: ["cooling", roomId, "heat-map", metric], queryFn: () => getHeatMap(roomId, metric), ...live });
  const airflowQuery = useQuery({ queryKey: ["cooling", roomId, "airflow"], queryFn: () => getRoomAirflow(roomId), ...live });
  const capacityQuery = useQuery({ queryKey: ["cooling", roomId, "capacity"], queryFn: () => getRoomCapacity(roomId), ...live });
  const exceptionsQuery = useQuery({ queryKey: ["cooling", roomId, "exceptions"], queryFn: () => getRoomExceptions(roomId), ...live });
  const map = mapQuery.data;
  const clip = useMemo(() => clipOf(view), [view]);

  const toggle = (key: keyof ThermalLayerToggles, label: string) => (
    <label key={key} className="flex items-center gap-1.5">
      <input type="checkbox" checked={toggles[key]} onChange={(e) => setToggles((t) => ({ ...t, [key]: e.target.checked }))} aria-label={`Show ${label}`} />
      {label}
    </label>
  );

  return (
    <div data-testid="cooling-panel" className="space-y-4">
      <div className="flex flex-wrap items-center gap-4 text-xs text-slate-300">
        <div role="group" aria-label="Environmental metric" className="flex overflow-hidden rounded-sm border border-slate-700">
          {(["temperature_c", "humidity_percent"] as ThermalMetric[]).map((m) => (
            <button key={m} type="button" aria-pressed={metric === m} onClick={() => { setMetric(m); setSelection(null); }} className={`px-3 py-1 ${metric === m ? "bg-blue-700 text-white" : "bg-slate-900 text-slate-300 hover:bg-slate-800"}`}>
              {metricLabel(m)}
            </button>
          ))}
        </div>
        <fieldset className="flex flex-wrap items-center gap-3">
          <legend className="sr-only">Layers</legend>
          {toggle("heat", "heat map")}
          {toggle("sensors", "sensors")}
          {toggle("airflow", "airflow")}
          {toggle("zones", "aisles and containment")}
          {toggle("units", "cooling units")}
        </fieldset>
      </div>

      <QueryNote isLoading={mapQuery.isLoading} error={mapQuery.error} what={`${metricLabel(metric).toLowerCase()} map`} />
      {map && <MapStatus map={map} />}

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <div>
          <RoomSpatialCanvas
            view={view}
            showGrid
            renderUnderlay={({ vp }) => <ThermalHeatLayer vp={vp} map={map} toggles={toggles} clip={clip} selection={selection} onSelect={setSelection} />}
            renderLayers={({ vp }) => (
              <ThermalMarkerLayers vp={vp} map={map} layout={layoutQuery.data} airflow={airflowQuery.data} toggles={toggles} selection={selection} onSelect={setSelection} />
            )}
            extraLegend={<ThermalLegend map={map} />}
          />
        </div>
        <div className="space-y-4">
          <Details selection={selection} map={map} layout={layoutQuery.data} airflow={airflowQuery.data} capacity={capacityQuery.data?.zones} />
          <Section title="Environmental exceptions" id="exceptions-title">
            <QueryNote isLoading={exceptionsQuery.isLoading} error={exceptionsQuery.error} what="exceptions" />
            {exceptionsQuery.data && (
              <>
                <p className="mb-1 text-xs text-slate-500" data-testid="exception-counts">
                  {exceptionsQuery.data.items.length === 0 ? "No exceptions." : `${exceptionsQuery.data.items.length} exception(s): ${Object.entries(exceptionsQuery.data.counts).map(([k, v]) => `${v} ${k}`).join(", ")}`}
                </p>
                <ul className="max-h-72 space-y-1 overflow-auto text-xs" data-testid="exception-list">
                  {exceptionsQuery.data.items.map((item, i) => (
                    <li key={`${item.type}-${item.subject_id}-${i}`} data-exception-type={item.type} data-source={item.source}>
                      <span className={`mr-1 rounded-sm px-1.5 py-0.5 ${SEVERITY_STYLE[item.severity]}`}><span aria-hidden="true">{SEVERITY_GLYPH[item.severity]}</span> {item.severity}</span>
                      <span className="mr-1 text-slate-500">[{item.source === "alarm" ? "alarm" : "derived"}]</span>
                      {item.message}
                    </li>
                  ))}
                </ul>
                {exceptionsQuery.data.alarms_omitted && <p className="mt-1 text-xs text-slate-500">{exceptionsQuery.data.alarms_omitted}</p>}
              </>
            )}
          </Section>
        </div>
      </div>

      <Section title="Cooling capacity and headroom" id="capacity-title">
        <QueryNote isLoading={capacityQuery.isLoading} error={capacityQuery.error} what="cooling capacity" />
        {capacityQuery.data && capacityQuery.data.zones.length === 0 && (
          <p className="text-xs text-slate-400" data-testid="capacity-empty">No served zone is defined for this room, so capacity and headroom cannot be calculated.</p>
        )}
        {capacityQuery.data && capacityQuery.data.zones.length > 0 && (
          <>
            <p className="mb-2 text-xs text-slate-500" data-testid="capacity-assumption">{capacityQuery.data.load_assumption}</p>
            <div className="overflow-x-auto">
              <table className="w-full text-left text-xs" data-testid="capacity-table">
                <thead className="text-slate-500">
                  <tr>
                    <th scope="col">Zone</th><th scope="col">Units (available / total)</th><th scope="col">Installed rated</th><th scope="col">Available</th>
                    <th scope="col">Thermal load</th><th scope="col">Headroom</th><th scope="col">Redundancy</th><th scope="col">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {capacityQuery.data.zones.map((z) => (
                    <tr key={z.zone_id} data-zone-level={z.level} data-redundancy={z.redundancy.state}>
                      <th scope="row">{z.name}</th>
                      <td>{z.available_units} / {z.unit_count}</td>
                      <td>{z.installed_rated_kw != null ? `${z.installed_rated_kw} kW${z.installed_rated_complete ? "" : " (some unknown)"}` : "unknown"}</td>
                      <td>{z.available_kw != null ? `${z.available_kw} kW${z.available_complete ? "" : " (some unknown)"}` : "unknown"}</td>
                      <td data-testid="load-cell">{loadText(z)}</td>
                      <td data-testid="headroom-cell">{headroomText(z)}</td>
                      <td data-testid="redundancy-cell">{redundancyText(z)}</td>
                      <td><span className={`rounded-sm px-1.5 py-0.5 ${LEVEL_STYLE[z.level]}`}><span aria-hidden="true">{LEVEL_GLYPH[z.level]}</span> {z.level.replace(/_/g, " ")}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </Section>

      <details className="text-xs text-slate-400" open>
        <summary className="cursor-pointer text-sm font-semibold text-slate-300">Text alternative: sensors</summary>
        {map && map.sensors.length === 0 && <p className="mt-2" data-testid="sensors-empty">No {metricLabel(metric).toLowerCase()} sensor is placed in this room.</p>}
        {map && map.sensors.length > 0 && (
          <table className="mt-2 w-full text-left" data-testid="sensor-table">
            <thead className="text-slate-500">
              <tr><th scope="col">Sensor</th><th scope="col">State</th><th scope="col">Value</th><th scope="col">Age</th><th scope="col">Position (mm)</th><th scope="col">In field</th></tr>
            </thead>
            <tbody>
              {map.sensors.map((p) => (
                <tr key={p.sensor_id} data-sensor-state={p.state}>
                  <th scope="row"><button type="button" className="underline" onClick={() => setSelection({ kind: "sensor", id: p.sensor_id })} aria-label={describeSensor(p)}>{p.name}</button></th>
                  <td>{SENSOR_STATE_META[p.state].glyph} {SENSOR_STATE_META[p.state].label}</td>
                  <td>{p.presentation_value != null ? formatQuantity(p.presentation_value, p.presentation_unit, 2) : "—"}</td>
                  <td>{formatAge(p.age_seconds)}</td>
                  <td>{p.x_mm != null ? `${Math.round(p.x_mm)}, ${Math.round(p.y_mm ?? 0)}${p.position_exact ? "" : " ≈"}` : "not recorded"}</td>
                  <td>{p.used_in_field ? "yes" : explainExclusion(p.excluded_reason) || "no"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </details>
      {airflowQuery.data && (
        <p className="text-xs text-slate-500" data-testid="airflow-note">
          Airflow: {airflowQuery.data.provenance_summary.measured_magnitude} measured, {airflowQuery.data.provenance_summary.configured_design} configured design, {airflowQuery.data.provenance_summary.modelled} modelled; {airflowQuery.data.provenance_summary.not_drawable} not drawn (no direction or magnitude configured). {airflowQuery.data.note}
        </p>
      )}
    </div>
  );
}
