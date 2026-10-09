import { SENSOR_STATE_META, cellRects, describeSensor, formatQuantity, heatColour, normalise, scaleBounds, zonePoints } from "@/features/cooling/thermalMath";
import type { CoolingLayout, HeatMap } from "@/types";
import type { SceneModel } from "./layout3dMath";

/** Thermal layer for the 3D twin: the interpolated field lies on the floor plane, sensors and cooling units are
 * flat markers at their calibrated floor coordinates. Same scale as the racks; interpolated cells are tagged as such. */
export function ThermalFloor3D({ scene, map, layout }: { scene: SceneModel; map: HeatMap | null | undefined; layout: CoolingLayout | null | undefined }) {
  const { minX, minY } = scene.floor.boundsMm;
  const px = (x: number) => (x - minX) * scene.scale;
  const py = (y: number) => (y - minY) * scene.scale;
  const bounds = map ? scaleBounds(map) : null;
  const grid = map?.grid ?? null;
  const colour = (value: number) => (map && bounds ? heatColour(map.metric, normalise(value, bounds.min, bounds.max)) : "#94a3b8");
  return (
    <svg className="layout3d-boundary" width={scene.floor.width} height={scene.floor.height} data-testid="thermal-3d" data-metric={map?.metric} data-map-state={map?.state}>
      {grid && (
        <g data-provenance="interpolated" data-testid="thermal-3d-cells">
          {cellRects(grid).map((cell) => (
            <rect key={cell.index} x={px(cell.xMm)} y={py(cell.yMm)} width={cell.sizeMm * scene.scale + 0.5} height={cell.sizeMm * scene.scale + 0.5} fill={colour(cell.value)} fillOpacity={0.5} data-provenance="interpolated" />
          ))}
        </g>
      )}
      {layout?.zones.map((zone) => {
        const pts = zonePoints(zone);
        if (!pts) return null;
        const stroke = zone.zone_kind === "hot_aisle" ? "#fb923c" : zone.zone_kind === "cold_aisle" ? "#38bdf8" : "#94a3b8";
        return (
          <polygon key={zone.id} points={pts.map(([x, y]) => `${px(x)},${py(y)}`).join(" ")} fill="none" stroke={stroke} strokeWidth={zone.containment === "contained" ? 3 : 1.5} strokeDasharray={zone.containment === "contained" ? undefined : "6 3"} data-zone-kind={zone.zone_kind} data-containment={zone.containment}>
            <title>{`${zone.name} (${zone.zone_kind.replace("_", " ")}${zone.containment === "contained" ? ", contained" : ""})`}</title>
          </polygon>
        );
      })}
      {layout?.cooling_units.filter((u) => u.x_mm != null && u.y_mm != null).map((u) => (
        <g key={u.id} data-unit-id={u.id} data-unit-kind={u.unit_kind}>
          <rect x={px(u.x_mm as number) - 14} y={py(u.y_mm as number) - 9} width={28} height={18} rx={2} fill="#0f172a" stroke="#cbd5e1" strokeDasharray="3 2" />
          <text x={px(u.x_mm as number)} y={py(u.y_mm as number) + 3} fontSize={8} fill="#f8fafc" textAnchor="middle" fontWeight={700}>{u.unit_kind.toUpperCase()}</text>
          <title>{`${u.unit_kind.toUpperCase()} ${u.name}, ${u.operating_status}. Footprint not modelled.`}</title>
        </g>
      ))}
      {map?.sensors.filter((s) => s.x_mm != null && s.y_mm != null).map((s) => {
        const meta = SENSOR_STATE_META[s.state];
        return (
          <g key={s.sensor_id} data-sensor-id={s.sensor_id} data-state={s.state} data-provenance="measured">
            <circle cx={px(s.x_mm as number)} cy={py(s.y_mm as number)} r={6} fill={s.state === "measured_fresh" && s.value != null ? colour(s.value) : "none"} stroke={meta.colour} strokeWidth={2} strokeDasharray={s.state === "measured_fresh" ? undefined : "3 2"} />
            <text x={px(s.x_mm as number)} y={py(s.y_mm as number) + 3} fontSize={8} fill={meta.colour} textAnchor="middle" fontWeight={700}>{s.state === "measured_fresh" ? "" : meta.glyph}</text>
            <text x={px(s.x_mm as number) + 9} y={py(s.y_mm as number) + 3} fontSize={9} fill={meta.colour}>{s.presentation_value != null ? formatQuantity(s.presentation_value, s.presentation_unit) : meta.label}</text>
            <title>{describeSensor(s)}</title>
          </g>
        );
      })}
    </svg>
  );
}
