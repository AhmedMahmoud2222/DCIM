import { type FormEvent, useEffect, useId, useState } from "react";

import type { Calibration, CalibrationRequest, FloorPlan, ImportDiagnostics } from "@/types";
import type { PickMode } from "./ImportReviewCanvas";
import { type Point, describeCalibration } from "./spatialMath";

const DECLARABLE = new Set(["mm", "cm", "m", "in", "ft"]);
type Method = Calibration["method"];

interface Props {
  floorPlan: FloorPlan;
  jobId: string;
  diagnostics: ImportDiagnostics | undefined;
  picks: Point[];
  pickMode: PickMode;
  onPickMode: (mode: PickMode) => void;
  onClearPicks: () => void;
  origin: Point | null;
  onClearOrigin: () => void;
  pending: boolean;
  error: string | null;
  locked: boolean;
  onSubmit: (request: CalibrationRequest) => void;
}

const num = (v: string): number | undefined => (v.trim() === "" || Number.isNaN(Number(v)) ? undefined : Number(v));

export function CalibrationPanel({ floorPlan, jobId, diagnostics, picks, pickMode, onPickMode, onClearPicks, origin, onClearOrigin, pending, error, locked, onSubmit }: Props) {
  const id = useId();
  const canDeclare = !!diagnostics?.source_units && DECLARABLE.has(diagnostics.source_units);
  const [method, setMethod] = useState<Method>("two_point");
  const [rotation, setRotation] = useState<0 | 90 | 180 | 270>(0);
  const [distance, setDistance] = useState("");
  const [tolerance, setTolerance] = useState("1");
  const [pickTolerance, setPickTolerance] = useState("0");
  const [srcWidth, setSrcWidth] = useState("");
  const [realWidth, setRealWidth] = useState("");
  const [srcHeight, setSrcHeight] = useState("");
  const [realHeight, setRealHeight] = useState("");
  const [scale, setScale] = useState("");

  useEffect(() => {
    if (canDeclare) setMethod("declared_units");
  }, [canDeclare, jobId]);

  const calibration = floorPlan.current_calibration;
  const baseline = picks.length === 2 ? Math.hypot(picks[1][0] - picks[0][0], picks[1][1] - picks[0][1]) : null;
  const previewScale = method === "two_point" && baseline && num(distance) ? num(distance)! / baseline : null;

  function submit(e: FormEvent) {
    e.preventDefault();
    const base: CalibrationRequest = { method, job_id: jobId, rotation_degrees: rotation, ...(origin ? { origin } : {}) };
    if (method === "two_point" && picks.length === 2 && num(distance)) {
      onSubmit({ ...base, p1: picks[0], p2: picks[1], distance_mm: num(distance), tolerance_mm: num(tolerance) ?? 1, pick_tolerance_src: num(pickTolerance) ?? 0 });
    } else if (method === "room_dimension" && num(srcWidth) && num(realWidth)) {
      onSubmit({ ...base, src_width: num(srcWidth), real_width_mm: num(realWidth), src_height: num(srcHeight), real_height_mm: num(realHeight), tolerance_mm: num(tolerance) ?? 1 });
    } else if (method === "manual_scale" && num(scale)) {
      onSubmit({ ...base, mm_per_unit: num(scale) });
    } else if (method === "declared_units") {
      onSubmit(base);
    }
  }

  const ready =
    (method === "declared_units" && canDeclare) ||
    (method === "two_point" && picks.length === 2 && !!num(distance)) ||
    (method === "room_dimension" && !!num(srcWidth) && !!num(realWidth) && (!!num(srcHeight) === !!num(realHeight))) ||
    (method === "manual_scale" && !!num(scale));

  return (
    <section aria-labelledby={`${id}-h`} className="rounded-sm border border-slate-800 bg-slate-900 p-4" data-testid="calibration-panel">
      <h3 id={`${id}-h`} className="mb-2 text-sm font-semibold text-slate-300">
        Calibrate scale and origin
      </h3>
      {calibration && (
        <p className="mb-3 text-xs text-slate-400" data-testid="current-calibration">
          Current calibration #{calibration.sequence} ({calibration.method.replace("_", " ")}): {describeCalibration(calibration)}
          {calibration.warnings.length > 0 && <span className="block text-yellow-400">{calibration.warnings.join(" ")}</span>}
        </p>
      )}
      {locked ? (
        <p className="text-xs text-yellow-400" role="status">
          A shape has already been accepted under this calibration, so it can no longer be changed on this revision. Create a new draft revision to recalibrate.
        </p>
      ) : (
        <form onSubmit={submit} className="space-y-3 text-sm">
          <fieldset>
            <legend className="mb-1 text-xs text-slate-500">Method</legend>
            {(
              [
                ["declared_units", `Use the file's declared units${diagnostics?.source_units ? ` (${diagnostics.source_units}${diagnostics.units_trusted ? "" : ", not authoritative"})` : ""}`],
                ["two_point", "Two points and a measured real distance"],
                ["room_dimension", "Known room width (and height)"],
                ["manual_scale", "Enter the scale directly (no verifiable error bound)"],
              ] as [Method, string][]
            ).map(([value, label]) => (
              <label key={value} className={`mb-1 flex items-center gap-2 ${value === "declared_units" && !canDeclare ? "opacity-40" : ""}`}>
                <input type="radio" name={`${id}-method`} value={value} checked={method === value} disabled={value === "declared_units" && !canDeclare} onChange={() => setMethod(value)} />
                {label}
              </label>
            ))}
          </fieldset>

          {method === "two_point" && (
            <div className="space-y-2 rounded-sm bg-slate-800/40 p-2">
              <div className="flex flex-wrap items-center gap-2">
                <button type="button" className="rounded-sm border border-slate-600 px-2 py-1 text-xs" onClick={() => (onClearPicks(), onPickMode("two-point"))} aria-pressed={pickMode === "two-point"}>
                  {picks.length === 2 ? "Re-pick points" : "Pick two points on the drawing"}
                </button>
                <span className="text-xs text-slate-500" data-testid="pick-status">
                  {picks.length}/2 points{baseline ? ` · ${baseline.toFixed(2)} source units apart` : ""}
                </span>
              </div>
              <div className="grid grid-cols-3 gap-2">
                <label className="text-xs text-slate-400">
                  Real distance (mm)
                  <input aria-label="Real distance (mm)" inputMode="decimal" value={distance} onChange={(e) => setDistance(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                </label>
                <label className="text-xs text-slate-400">
                  Measurement tolerance (mm)
                  <input aria-label="Measurement tolerance (mm)" inputMode="decimal" value={tolerance} onChange={(e) => setTolerance(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                </label>
                <label className="text-xs text-slate-400">
                  Pick tolerance (source units)
                  <input aria-label="Pick tolerance (source units)" inputMode="decimal" value={pickTolerance} onChange={(e) => setPickTolerance(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                </label>
              </div>
              {previewScale && (
                <p className="text-xs text-slate-400" data-testid="scale-preview">
                  Preview: 1 source unit = {previewScale.toPrecision(5)} mm
                </p>
              )}
            </div>
          )}

          {method === "room_dimension" && (
            <div className="grid grid-cols-2 gap-2 rounded-sm bg-slate-800/40 p-2">
              {[
                ["Drawn width (source units)", srcWidth, setSrcWidth],
                ["Real width (mm)", realWidth, setRealWidth],
                ["Drawn height (optional)", srcHeight, setSrcHeight],
                ["Real height (optional, mm)", realHeight, setRealHeight],
              ].map(([label, value, setter]) => (
                <label key={label as string} className="text-xs text-slate-400">
                  {label as string}
                  <input aria-label={label as string} inputMode="decimal" value={value as string} onChange={(e) => (setter as (v: string) => void)(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                </label>
              ))}
              <p className="col-span-2 text-xs text-slate-500">Giving both dimensions checks the drawing is not distorted; scales that differ by more than 2% are rejected.</p>
            </div>
          )}

          {method === "manual_scale" && (
            <label className="block rounded-sm bg-slate-800/40 p-2 text-xs text-slate-400">
              Millimetres per source unit
              <input aria-label="Millimetres per source unit" inputMode="decimal" value={scale} onChange={(e) => setScale(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
            </label>
          )}

          <div className="flex flex-wrap items-center gap-4 text-xs text-slate-400">
            <span className="flex items-center gap-2">
              <button type="button" className="rounded-sm border border-slate-600 px-2 py-1" aria-pressed={pickMode === "origin"} onClick={() => onPickMode("origin")}>
                Pick room origin
              </button>
              <span data-testid="origin-status">{origin ? `origin at (${origin[0].toFixed(1)}, ${origin[1].toFixed(1)})` : "origin: top-left of the drawing"}</span>
              {origin && (
                <button type="button" className="underline" onClick={onClearOrigin}>
                  reset
                </button>
              )}
            </span>
            <label className="flex items-center gap-1">
              Rotate plan
              <select aria-label="Rotate plan" value={rotation} onChange={(e) => setRotation(Number(e.target.value) as 0 | 90 | 180 | 270)} className="rounded-sm border border-slate-700 bg-slate-950 px-1 py-0.5">
                {[0, 90, 180, 270].map((d) => (
                  <option key={d} value={d}>
                    {d}° clockwise
                  </option>
                ))}
              </select>
            </label>
          </div>

          {error && (
            <p role="alert" className="text-xs text-red-400">
              {error}
            </p>
          )}
          <button type="submit" disabled={!ready || pending} className="rounded-sm bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50">
            {pending ? "Calibrating…" : calibration ? "Recalibrate" : "Set calibration"}
          </button>
        </form>
      )}
    </section>
  );
}
