import { type GridSettings, SNAP_OPTIONS } from "./gridSettings";
import { type BoundsMm, type Viewport, formatGridInterval, formatLength, gridTicks, mmToPx, niceGridInterval } from "./spatialMath";

export function GridControls({ settings, onChange, idPrefix }: { settings: GridSettings; onChange: (s: GridSettings) => void; idPrefix: string }) {
  return (
    <div className="flex flex-wrap items-center gap-4 text-xs text-slate-400" role="group" aria-label="Grid and snapping">
      <label className="flex items-center gap-1.5">
        <input
          type="checkbox"
          checked={settings.showGrid}
          onChange={(e) => onChange({ ...settings, showGrid: e.target.checked })}
        />
        Engineering grid
      </label>
      <label className="flex items-center gap-1.5" htmlFor={`${idPrefix}-snap`}>
        Snap to
        <select
          id={`${idPrefix}-snap`}
          value={settings.snapMm}
          onChange={(e) => onChange({ ...settings, snapMm: Number(e.target.value) })}
          className="rounded-sm border border-slate-700 bg-slate-900 px-1 py-0.5 text-xs text-slate-200"
        >
          {SNAP_OPTIONS.map((mm) => (
            <option key={mm} value={mm}>
              {mm === 0 ? "off" : formatGridInterval(mm)}
            </option>
          ))}
        </select>
      </label>
    </div>
  );
}

const GRID_STROKE = "rgb(51 65 85 / 0.55)";

/** Engineering grid + rulers + origin marker + scale bar over canonical mm, in a pixel viewport. */
export function SceneChrome({
  vp,
  bounds,
  showGrid,
  unitLabel = "mm",
}: {
  vp: Viewport;
  bounds: BoundsMm;
  showGrid: boolean;
  unitLabel?: string;
}) {
  const interval = niceGridInterval(vp.scale, 48);
  const xs = gridTicks(bounds.minX, bounds.maxX, interval);
  const ys = gridTicks(bounds.minY, bounds.maxY, interval);
  const [x0, y0] = mmToPx(vp, bounds.minX, bounds.minY);
  const [x1, y1] = mmToPx(vp, bounds.maxX, bounds.maxY);
  const [ox, oy] = mmToPx(vp, 0, 0);
  const barMm = niceGridInterval(vp.scale, 90);
  const labelOf = (mm: number) => (interval >= 1000 ? `${mm / 1000}` : `${mm}`);
  return (
    <g aria-hidden="true" data-testid="scene-chrome" data-grid-interval-mm={interval}>
      {showGrid && (
        <g stroke={GRID_STROKE} strokeWidth={1}>
          {xs.map((x) => {
            const [px] = mmToPx(vp, x, 0);
            return <line key={`gx${x}`} x1={px} x2={px} y1={y0} y2={y1} />;
          })}
          {ys.map((y) => {
            const [, py] = mmToPx(vp, 0, y);
            return <line key={`gy${y}`} x1={x0} x2={x1} y1={py} y2={py} />;
          })}
        </g>
      )}
      <g fontSize={9} fill="#64748b">
        {xs
          .filter((_, i) => i % Math.max(1, Math.round(56 / (interval * vp.scale))) === 0)
          .map((x) => {
            const [px] = mmToPx(vp, x, 0);
            return (
              <text key={`rx${x}`} x={px} y={y0 - 6} textAnchor="middle">
                {labelOf(x)}
              </text>
            );
          })}
        {ys.map((y) => {
          const [, py] = mmToPx(vp, 0, y);
          return (
            <text key={`ry${y}`} x={x0 - 6} y={py + 3} textAnchor="end">
              {labelOf(y)}
            </text>
          );
        })}
        <text x={x1} y={y0 - 18} textAnchor="end" fill="#94a3b8">
          {interval >= 1000 ? "metres" : "millimetres"} · grid {formatGridInterval(interval)}
        </text>
      </g>
      {ox >= x0 - 1 && ox <= x1 + 1 && oy >= y0 - 1 && oy <= y1 + 1 && (
        <g stroke="#38bdf8" strokeWidth={1.5} data-testid="room-origin">
          <line x1={ox - 9} x2={ox + 9} y1={oy} y2={oy} />
          <line x1={ox} x2={ox} y1={oy - 9} y2={oy + 9} />
          <circle cx={ox} cy={oy} r={3.5} fill="#0c4a6e" />
          <text x={ox + 8} y={oy + 14} fontSize={9} fill="#7dd3fc" stroke="none">
            room origin (0, 0) · X→ Y↓
          </text>
        </g>
      )}
      <g data-testid="scale-bar" transform={`translate(${x0}, ${y1 + 22})`}>
        <line x1={0} x2={barMm * vp.scale} y1={0} y2={0} stroke="#cbd5e1" strokeWidth={2} />
        <line x1={0} x2={0} y1={-4} y2={4} stroke="#cbd5e1" strokeWidth={2} />
        <line x1={barMm * vp.scale} x2={barMm * vp.scale} y1={-4} y2={4} stroke="#cbd5e1" strokeWidth={2} />
        <text x={barMm * vp.scale + 8} y={3} fontSize={10} fill="#cbd5e1">
          {formatLength(barMm)} ({unitLabel})
        </text>
      </g>
    </g>
  );
}
