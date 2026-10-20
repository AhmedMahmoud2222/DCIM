import { type KeyboardEvent, type MouseEvent, useId } from "react";

import { type Point, type Viewport, mmToPx, pxToMm } from "@/features/floor-plans/spatialMath";
import type { AirflowElement, CoolingLayout, CoolingLayoutUnit, CoolingLayoutZone, HeatMap, RoomAirflow, ThermalSensorPoint } from "@/types";
import {
  SENSOR_STATE_META,
  bearingToVector,
  cellAt,
  cellRects,
  describeSensor,
  formatQuantity,
  heatColour,
  normalise,
  scaleBounds,
  thresholdBreach,
  zonePoints,
} from "./thermalMath";

export interface ThermalLayerToggles {
  heat: boolean;
  sensors: boolean;
  airflow: boolean;
  zones: boolean;
  units: boolean;
}

export type ThermalSelection =
  | { kind: "sensor"; id: string }
  | { kind: "cell"; column: number; row: number }
  | { kind: "unit"; id: string }
  | { kind: "airflow"; id: string }
  | null;

const ZONE_STYLE: Record<CoolingLayoutZone["zone_kind"], { stroke: string; fill: string; dash?: string; label: string }> = {
  cold_aisle: { stroke: "#38bdf8", fill: "rgb(56 189 248 / .10)", dash: "6 3", label: "Cold aisle" },
  hot_aisle: { stroke: "#fb923c", fill: "rgb(251 146 60 / .10)", dash: "6 3", label: "Hot aisle" },
  served_zone: { stroke: "#94a3b8", fill: "none", dash: "2 4", label: "Served zone" },
  supply_region: { stroke: "#67e8f9", fill: "rgb(103 232 249 / .06)", dash: "8 2 2 2", label: "Supply region" },
  return_region: { stroke: "#fca5a5", fill: "rgb(252 165 165 / .06)", dash: "8 2 2 2", label: "Return region" },
};

const UNIT_STATUS: Record<string, { colour: string; glyph: string }> = {
  online: { colour: "#16a34a", glyph: "▶" },
  standby: { colour: "#2563eb", glyph: "❚❚" },
  offline: { colour: "#64748b", glyph: "■" },
  fault: { colour: "#dc2626", glyph: "✕" },
  unknown: { colour: "#475569", glyph: "?" },
};

const keyActivate = (action: () => void) => (event: KeyboardEvent<SVGElement>) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    action();
  }
};

/** Heat-map cells, drawn UNDER racks and equipment so rack links stay clickable. Interpolated cells carry
 * `data-provenance="interpolated"`; they are never styled or labelled as measurements. */
export function ThermalHeatLayer({
  vp, map, toggles, clip, selection, onSelect,
}: {
  vp: Viewport;
  map: HeatMap | null | undefined;
  toggles: ThermalLayerToggles;
  clip: Point[] | null;
  selection: ThermalSelection;
  onSelect: (selection: ThermalSelection) => void;
}) {
  const uid = useId().replace(/:/g, "");
  const clipId = `thermal-clip-${uid}`;
  const bounds = map ? scaleBounds(map) : null;
  const colour = (value: number) => (map && bounds ? heatColour(map.metric, normalise(value, bounds.min, bounds.max)) : "#94a3b8");
  const grid = map?.grid ?? null;

  const onGridClick = (event: MouseEvent<SVGRectElement>) => {
    if (!grid) return;
    const svg = event.currentTarget.ownerSVGElement;
    if (!svg) return;
    const box = svg.getBoundingClientRect();
    const [xMm, yMm] = pxToMm(vp, ((event.clientX - box.left) / box.width) * vp.widthPx, ((event.clientY - box.top) / box.height) * vp.heightPx);
    const cell = cellAt(grid, xMm, yMm);
    onSelect(cell ? { kind: "cell", column: cell.column, row: cell.row } : null);
  };

  const onGridKey = (event: KeyboardEvent<SVGGElement>) => {
    if (!grid) return;
    const current = selection?.kind === "cell" ? selection : { column: Math.floor(grid.columns / 2), row: Math.floor(grid.rows / 2) };
    const delta: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    const step = delta[event.key];
    if (!step) return;
    event.preventDefault();
    const column = Math.min(grid.columns - 1, Math.max(0, current.column + step[0]));
    const row = Math.min(grid.rows - 1, Math.max(0, current.row + step[1]));
    onSelect({ kind: "cell", column, row });
  };

  return (
    <g data-testid="thermal-heat-layer">
      <defs>
        {clip && (
          <clipPath id={clipId}>
            <polygon points={clip.map(([x, y]) => mmToPx(vp, x, y).join(",")).join(" ")} />
          </clipPath>
        )}
      </defs>
      {toggles.heat && grid && map && (
        <g data-testid="heat-cells" data-provenance="interpolated" clipPath={clip ? `url(#${clipId})` : undefined} role="group" aria-label={`${map.metric === "humidity_percent" ? "Humidity" : "Temperature"} field, interpolated from sensors, not measured. Press an arrow key to inspect cells.`} tabIndex={0} onKeyDown={onGridKey}>
          {cellRects(grid).map((cell) => {
            const [px, py] = mmToPx(vp, cell.xMm, cell.yMm);
            const size = cell.sizeMm * vp.scale;
            const breach = thresholdBreach(map, cell.value);
            return (
              <rect key={cell.index} x={px} y={py} width={size + 0.5} height={size + 0.5} fill={colour(cell.value)} fillOpacity={0.55} data-cell-index={cell.index} data-value={cell.value} data-provenance="interpolated" data-breach={breach ?? undefined} />
            );
          })}
          <rect x={0} y={0} width={vp.widthPx} height={vp.heightPx} fill="transparent" onClick={onGridClick} data-testid="heat-hit-area" />
          {selection?.kind === "cell" && (() => {
            const [px, py] = mmToPx(vp, grid.origin_x_mm + selection.column * grid.cell_mm, grid.origin_y_mm + selection.row * grid.cell_mm);
            const size = grid.cell_mm * vp.scale;
            return <rect x={px} y={py} width={size} height={size} fill="none" stroke="#f8fafc" strokeWidth={2} strokeDasharray="3 2" data-testid="cell-cursor" pointerEvents="none" />;
          })()}
        </g>
      )}

    </g>
  );
}

/** Zones, containment, cooling units, airflow arrows and sensor markers, drawn above racks. */
export function ThermalMarkerLayers({
  vp, layout, map, airflow, toggles, selection, onSelect,
}: {
  vp: Viewport;
  map: HeatMap | null | undefined;
  layout: CoolingLayout | null | undefined;
  airflow: RoomAirflow | null | undefined;
  toggles: ThermalLayerToggles;
  selection: ThermalSelection;
  onSelect: (selection: ThermalSelection) => void;
}) {
  const uid = useId().replace(/:/g, "");
  const arrowId = `thermal-arrow-${uid}`;
  const bounds = map ? scaleBounds(map) : null;
  const colour = (value: number) => (map && bounds ? heatColour(map.metric, normalise(value, bounds.min, bounds.max)) : "#94a3b8");
  const sensors = (map?.sensors ?? []).filter((s) => s.x_mm != null && s.y_mm != null);
  const units = (layout?.cooling_units ?? []).filter((u) => u.x_mm != null && u.y_mm != null);
  const arrows = (airflow?.elements ?? []).filter((e) => e.drawable && e.x_mm != null && e.y_mm != null && e.direction_deg != null);

  return (
    <g data-testid="thermal-marker-layers">
      <defs>
        <marker id={arrowId} viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
          <path d="M 0 0 L 10 5 L 0 10 z" fill="context-stroke" />
        </marker>
      </defs>
      {toggles.zones && layout && (
        <g data-testid="zone-layer" pointerEvents="none">
          {layout.zones.map((zone) => {
            const pts = zonePoints(zone);
            if (!pts) return null;
            const style = ZONE_STYLE[zone.zone_kind];
            const contained = zone.containment === "contained";
            const px = pts.map(([x, y]) => mmToPx(vp, x, y).join(",")).join(" ");
            const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
            const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
            const [lx, ly] = mmToPx(vp, cx, cy);
            return (
              <g key={zone.id} data-zone-id={zone.id} data-zone-kind={zone.zone_kind} data-containment={zone.containment}>
                <polygon points={px} fill={style.fill} stroke={style.stroke} strokeWidth={contained ? 3 : 1.5} strokeDasharray={contained ? undefined : style.dash} />
                <text x={lx} y={ly} fontSize={10} fill={style.stroke} textAnchor="middle" pointerEvents="none" paintOrder="stroke" stroke="#020617" strokeWidth={3}>
                  {style.label}
                  {contained ? " · contained" : ""}
                </text>
                <title>{`${zone.name} · ${style.label}${contained ? ", contained" : ""} · operator-configured geometry`}</title>
                {zone.elements.map((el) => {
                  const [x1, y1] = mmToPx(vp, el.x1_mm, el.y1_mm);
                  const [x2, y2] = mmToPx(vp, el.x2_mm, el.y2_mm);
                  return (
                    <line key={el.id} x1={x1} y1={y1} x2={x2} y2={y2} stroke={el.element_kind === "boundary" ? "#e2e8f0" : "#fde047"} strokeWidth={el.element_kind === "boundary" ? 4 : 3} strokeDasharray={el.element_kind === "opening" ? "3 5" : undefined} data-containment-element={el.element_kind}>
                      <title>{`Containment ${el.element_kind}${el.label ? ` · ${el.label}` : ""}`}</title>
                    </line>
                  );
                })}
              </g>
            );
          })}
        </g>
      )}

      {toggles.units && (
        <g data-testid="unit-layer">
          {units.map((unit: CoolingLayoutUnit) => {
            const [px, py] = mmToPx(vp, unit.x_mm as number, unit.y_mm as number);
            const status = UNIT_STATUS[unit.operating_status] ?? UNIT_STATUS.unknown;
            const selected = selection?.kind === "unit" && selection.id === unit.id;
            const label = `${unit.unit_kind.toUpperCase()} ${unit.name}, ${unit.operating_status}, ${unit.rated_cooling_capacity_kw != null ? `${unit.rated_cooling_capacity_kw} kW rated` : "rated capacity unknown"}. Footprint is not modelled, so this marker is not to scale.`;
            return (
              <g key={unit.id} role="button" tabIndex={0} aria-label={label} aria-pressed={selected} data-unit-id={unit.id} data-unit-kind={unit.unit_kind} data-status={unit.operating_status} onClick={() => onSelect({ kind: "unit", id: unit.id })} onKeyDown={keyActivate(() => onSelect({ kind: "unit", id: unit.id }))} style={{ cursor: "pointer" }}>
                <rect x={px - 17} y={py - 11} width={34} height={22} rx={3} fill={status.colour} fillOpacity={0.9} stroke={selected ? "#f8fafc" : "#cbd5e1"} strokeWidth={selected ? 3 : 1} strokeDasharray="3 2" />
                <text x={px} y={py + 3} fontSize={9} fill="#f8fafc" textAnchor="middle" pointerEvents="none" fontWeight={700}>
                  {unit.unit_kind.toUpperCase()} {status.glyph}
                </text>
                <title>{label}</title>
              </g>
            );
          })}
        </g>
      )}

      {toggles.airflow && airflow && (
        <g data-testid="airflow-layer">
          {arrows.map((e: AirflowElement) => {
            const [px, py] = mmToPx(vp, e.x_mm as number, e.y_mm as number);
            const [dx, dy] = bearingToVector(e.direction_deg as number, 44);
            const measured = e.magnitude_provenance === "measured";
            const stale = e.state === "measured_stale";
            const stroke = measured ? (stale ? "#f59e0b" : "#0ea5e9") : "#a3e635";
            const selected = selection?.kind === "airflow" && selection.id === e.id;
            const magnitude = measured
              ? formatQuantity(e.volume_flow?.presentation_value ?? e.velocity?.presentation_value ?? null, e.volume_flow ? e.volume_flow.presentation_unit : e.velocity?.presentation_unit)
              : formatQuantity(e.magnitude_m3_s ?? null, "m³/s", 2);
            const text = `${measured ? (stale ? "measured, stale" : "measured") : "configured design"} ${magnitude}`;
            return (
              <g key={e.id} role="button" tabIndex={0} aria-label={`Airflow at ${e.name}: ${text}, direction ${e.direction_deg} degrees (configured)`} data-airflow-id={e.id} data-provenance={measured ? "measured" : "configured"} onClick={() => onSelect({ kind: "airflow", id: e.id })} onKeyDown={keyActivate(() => onSelect({ kind: "airflow", id: e.id }))} style={{ cursor: "pointer" }}>
                <line x1={px} y1={py} x2={px + dx} y2={py + dy} stroke="transparent" strokeWidth={14} data-hit-area="airflow" />
                <line x1={px} y1={py} x2={px + dx} y2={py + dy} stroke={stroke} strokeWidth={selected ? 4 : 2.5} strokeDasharray={measured && !stale ? undefined : "6 3"} markerEnd={`url(#${arrowId})`} />
                <text x={px + dx / 2 + 4} y={py + dy / 2 - 4} fontSize={9} fill={stroke} paintOrder="stroke" stroke="#020617" strokeWidth={3} pointerEvents="none">
                  {text}
                </text>
                <title>{`${e.name}: ${text}; direction is the configured orientation, not a measured flow path`}</title>
              </g>
            );
          })}
        </g>
      )}

      {toggles.sensors && (
        <g data-testid="sensor-layer" data-provenance="measured">
          {sensors.map((p: ThermalSensorPoint) => {
            const [px, py] = mmToPx(vp, p.x_mm as number, p.y_mm as number);
            const meta = SENSOR_STATE_META[p.state];
            const selected = selection?.kind === "sensor" && selection.id === p.sensor_id;
            const fill = p.state === "measured_fresh" && p.value != null ? colour(p.value) : "none";
            const label = describeSensor(p);
            return (
              <g key={p.sensor_id} role="button" tabIndex={0} aria-label={label} aria-pressed={selected} data-sensor-id={p.sensor_id} data-state={p.state} data-provenance="measured" data-used-in-field={p.used_in_field ? "true" : "false"} onClick={() => onSelect({ kind: "sensor", id: p.sensor_id })} onKeyDown={keyActivate(() => onSelect({ kind: "sensor", id: p.sensor_id }))} style={{ cursor: "pointer" }}>
                {/* generous transparent hit area: hollow (stale / missing) markers would otherwise let clicks fall through to the field */}
                <rect x={px - 12} y={py - 12} width={72} height={24} fill="transparent" data-hit-area="sensor" />
                {p.state === "invalid" ? (
                  <path d={`M ${px} ${py - 9} L ${px + 9} ${py + 7} L ${px - 9} ${py + 7} Z`} fill="#4c1d95" stroke={meta.colour} strokeWidth={2} />
                ) : (
                  <circle cx={px} cy={py} r={7} fill={fill} fillOpacity={0.95} stroke={meta.colour} strokeWidth={p.state === "measured_fresh" ? 2.5 : 2} strokeDasharray={p.state === "measured_stale" ? "3 2" : p.state === "missing" ? "1 2" : undefined} />
                )}
                <text x={px} y={py + 3} fontSize={9} fill={p.state === "measured_fresh" ? "#0f172a" : meta.colour} textAnchor="middle" pointerEvents="none" fontWeight={700}>
                  {p.state === "measured_fresh" ? "" : meta.glyph}
                </text>
                {!p.position_exact && <circle cx={px} cy={py} r={11} fill="none" stroke="#94a3b8" strokeWidth={1} strokeDasharray="1 3" />}
                {(p.active_alarm_count ?? 0) > 0 && (
                  <g pointerEvents="none">
                    <circle cx={px + 8} cy={py - 8} r={5} fill="#7f1d1d" stroke="#ef4444" />
                    <text x={px + 8} y={py - 5} fontSize={8} fill="#fecaca" textAnchor="middle">!</text>
                  </g>
                )}
                <text x={px + 11} y={py + 3} fontSize={10} fill={meta.colour} paintOrder="stroke" stroke="#020617" strokeWidth={3} pointerEvents="none">
                  {p.presentation_value != null ? formatQuantity(p.presentation_value, p.presentation_unit) : meta.label}
                </text>
                {selected && <circle cx={px} cy={py} r={13} fill="none" stroke="#f8fafc" strokeWidth={2} />}
                <title>{label}</title>
              </g>
            );
          })}
        </g>
      )}
    </g>
  );
}
