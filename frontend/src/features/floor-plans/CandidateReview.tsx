import { useEffect, useId, useMemo, useState } from "react";

import type { AcceptCandidateRequest, Calibration, ImportCandidate, MatchStatus, RoomSpatialView } from "@/types";
import type { CandidateCorrection } from "./api";
import { isRect, rectMmToSource, rectSourceToMm } from "./spatialMath";

const OBJECT_TYPES = ["rack", "equipment", "room_outline", "wall", "column", "obstacle", "aisle", "annotation", "imported_shape"];

const MATCH_BADGE: Record<MatchStatus, { label: string; cls: string }> = {
  not_applicable: { label: "n/a", cls: "bg-slate-800 text-slate-400" },
  unmatched: { label: "no matching rack", cls: "bg-slate-700 text-slate-200" },
  matched: { label: "matches a rack", cls: "bg-green-900 text-green-100" },
  ambiguous: { label: "ambiguous", cls: "bg-yellow-900 text-yellow-100" },
  conflict: { label: "conflict", cls: "bg-red-900 text-red-100" },
  duplicate: { label: "duplicate of accepted shape", cls: "bg-purple-900 text-purple-100" },
};

interface Props {
  view: RoomSpatialView | undefined;
  candidates: ImportCandidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  calibration: Calibration | null;
  busy: boolean;
  error: string | null;
  onCorrect: (candidate: ImportCandidate, body: CandidateCorrection) => void;
  onUndo: (candidate: ImportCandidate) => void;
  onAccept: (candidate: ImportCandidate, body: AcceptCandidateRequest) => void;
  onReject: (candidate: ImportCandidate) => void;
}

const fmt = (n: number) => String(Math.round(n * 100) / 100);

export function CandidateReview({ view, candidates, selectedId, onSelect, calibration, busy, error, onCorrect, onUndo, onAccept, onReject }: Props) {
  const id = useId();
  const [filter, setFilter] = useState<"all" | "racks" | "review">("all");
  const selected = candidates.find((c) => c.id === selectedId) ?? null;

  const visible = useMemo(
    () =>
      candidates.filter((c) =>
        filter === "all" ? true : filter === "racks" ? c.effective_object_type === "rack" : ["ambiguous", "conflict", "duplicate"].includes(c.match_status) || (c.confidence ?? 0) < 0.7,
      ),
    [candidates, filter],
  );

  return (
    <section aria-labelledby={`${id}-h`} className="rounded-sm border border-slate-800 bg-slate-900 p-4" data-testid="candidate-review">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <h3 id={`${id}-h`} className="text-sm font-semibold text-slate-300">
          Detected shapes ({candidates.length} pending)
        </h3>
        <label className="text-xs text-slate-400">
          Show{" "}
          <select aria-label="Filter candidates" value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)} className="rounded-sm border border-slate-700 bg-slate-950 px-1 py-0.5">
            <option value="all">everything</option>
            <option value="racks">racks only</option>
            <option value="review">needs closer review</option>
          </select>
        </label>
      </div>
      <p className="mb-2 text-xs text-slate-500">
        Detection is a suggestion only. Nothing here changes inventory until you accept a shape; unmatched, ambiguous, conflicting and duplicate shapes are never accepted automatically.
      </p>
      <ul className="mb-3 max-h-56 space-y-1 overflow-auto" data-testid="candidate-list">
        {visible.map((c) => {
          const badge = MATCH_BADGE[c.match_status];
          return (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => onSelect(c.id)}
                aria-pressed={c.id === selectedId}
                data-testid={`row-${c.id}`}
                className={`flex w-full items-center justify-between rounded-sm px-2 py-1 text-left text-sm ${c.id === selectedId ? "bg-slate-700" : "bg-slate-800/50 hover:bg-slate-800"}`}
              >
                <span>
                  {c.effective_label ?? c.raw_geometry.shape_type}
                  <span className="ml-2 rounded-sm bg-slate-700 px-1.5 py-0.5 text-xs">{c.effective_object_type ?? "unclassified"}</span>
                  {c.correction && <span className="ml-1 rounded-sm bg-blue-900 px-1.5 py-0.5 text-xs">edited</span>}
                </span>
                <span className="flex items-center gap-2 text-xs">
                  {c.confidence != null && <span className="text-slate-400">{Math.round(c.confidence * 100)}%</span>}
                  {c.effective_object_type === "rack" && <span className={`rounded-sm px-1.5 py-0.5 ${badge.cls}`}>{badge.label}</span>}
                </span>
              </button>
            </li>
          );
        })}
        {visible.length === 0 && <li className="py-3 text-center text-xs text-slate-500">No shapes match this filter.</li>}
      </ul>
      {selected ? (
        <CandidateEditor
          key={`${selected.id}:${selected.version}`}
          view={view}
          candidate={selected}
          calibration={calibration}
          busy={busy}
          error={error}
          onCorrect={onCorrect}
          onUndo={onUndo}
          onAccept={onAccept}
          onReject={onReject}
        />
      ) : (
        <p className="text-xs text-slate-500">Select a shape in the list or on the drawing to review, correct and accept it.</p>
      )}
    </section>
  );
}

function CandidateEditor({
  view,
  candidate,
  calibration,
  busy,
  error,
  onCorrect,
  onUndo,
  onAccept,
  onReject,
}: Omit<Props, "candidates" | "selectedId" | "onSelect"> & { candidate: ImportCandidate }) {
  const id = useId();
  const g = candidate.effective_geometry;
  const rect = isRect(g) ? g : null;
  const mm = rect && calibration ? rectSourceToMm(calibration, { cx: rect.cx, cy: rect.cy, width: rect.width, height: rect.height, rotation_deg: rect.rotation_deg }) : null;
  const unit = calibration ? "mm" : "src units";

  const initial = {
    x: rect ? fmt(mm ? mm.x_mm : rect.cx) : "",
    y: rect ? fmt(mm ? mm.y_mm : rect.cy) : "",
    w: rect ? fmt(mm ? mm.width_mm : rect.width) : "",
    h: rect ? fmt(mm ? mm.height_mm : rect.height) : "",
    r: rect ? fmt(mm ? mm.rotation_deg : (rect.rotation_deg ?? 0)) : "",
  };
  const [geo, setGeo] = useState(initial);
  const [label, setLabel] = useState(candidate.effective_label ?? "");
  const [type, setType] = useState(candidate.effective_object_type ?? "");
  const [matchId, setMatchId] = useState<string>(candidate.matched_asset_id ?? "");
  const [placement, setPlacement] = useState<"record" | "link" | "move">("record");
  const [confirmMove, setConfirmMove] = useState(false);
  const [boundaryReason, setBoundaryReason] = useState("");
  useEffect(() => setPlacement("record"), [matchId]);

  const racks = view?.racks ?? [];
  const equipment = (view?.equipment ?? []).filter((e) => e.placement_type !== "rack_mounted");
  const matchedRack = type === "rack" ? racks.find((r) => r.id === matchId) : undefined;
  const geometryDirty = JSON.stringify(geo) !== JSON.stringify(initial);

  function applyCorrection() {
    const body: CandidateCorrection = {};
    if (label !== (candidate.effective_label ?? "")) body.label = label;
    if (type && type !== candidate.effective_object_type) body.object_type = type;
    if (rect && geometryDirty) {
      const n = (s: string) => Number(s);
      if (calibration) {
        const src = rectMmToSource(calibration, { x_mm: n(geo.x), y_mm: n(geo.y), width_mm: n(geo.w), height_mm: n(geo.h), rotation_deg: n(geo.r) });
        Object.assign(body, { cx: src.cx, cy: src.cy, width: src.width, height: src.height, rotation_deg: src.rotation_deg });
      } else {
        Object.assign(body, { cx: n(geo.x), cy: n(geo.y), width: n(geo.w), height: n(geo.h), rotation_deg: n(geo.r) });
      }
    }
    if (matchId && matchId !== candidate.matched_asset_id) body.matched_asset_id = matchId;
    if (!matchId && candidate.matched_asset_id) body.clear_match = true;
    if (Object.keys(body).length) onCorrect(candidate, body);
  }

  function accept() {
    const body: AcceptCandidateRequest = { object_type: type || undefined, label: label || undefined };
    if (matchId && (type === "rack" || type === "equipment")) {
      body.matched_asset_id = matchId;
      if (placement === "link") body.link_placement = true;
      if (placement === "move" && matchedRack) {
        body.apply_position = true;
        body.placement_version = matchedRack.placement_version;
      }
    }
    if (boundaryReason.trim()) body.boundary_exception_reason = boundaryReason.trim();
    onAccept(candidate, body);
  }

  const evidence = candidate.evidence ?? [];
  const canonicalNote = candidate.canonical
    ? `Will be recorded at ${candidate.canonical.x_mm}, ${candidate.canonical.y_mm} mm${candidate.canonical.width_mm ? `, ${candidate.canonical.width_mm} × ${candidate.canonical.height_mm} mm` : ""}, ${candidate.canonical.rotation_deg}°`
    : "Calibrate to see the real-world position this shape will be recorded at.";
  const numField = (key: keyof typeof geo, labelText: string) => (
    <label className="text-xs text-slate-400">
      {labelText} ({unit})
      <input
        aria-label={`${labelText} (${unit})`}
        inputMode="decimal"
        value={geo[key]}
        onChange={(e) => setGeo({ ...geo, [key]: e.target.value })}
        className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm"
      />
    </label>
  );

  return (
    <div className="space-y-3 border-t border-slate-800 pt-3 text-sm" data-testid="candidate-editor">
      <div className="grid grid-cols-2 gap-2">
        <label className="text-xs text-slate-400">
          Label
          <input aria-label="Label" value={label} maxLength={255} onChange={(e) => setLabel(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
        </label>
        <label className="text-xs text-slate-400">
          Classification
          <select aria-label="Classification" value={type} onChange={(e) => setType(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm">
            <option value="">choose…</option>
            {OBJECT_TYPES.map((t) => (
              <option key={t} value={t}>
                {t.replace("_", " ")}
              </option>
            ))}
          </select>
        </label>
      </div>
      {rect && (
        <fieldset className="grid grid-cols-5 gap-2">
          <legend className="mb-1 text-xs text-slate-500">{calibration ? "Position and size (real-world, room-local origin)" : "Position and size (drawing units, uncalibrated)"}</legend>
          {numField("x", calibration ? "Left X" : "Centre X")}
          {numField("y", calibration ? "Top Y" : "Centre Y")}
          {numField("w", "Width")}
          {numField("h", "Depth")}
          {numField("r", "Rotation °")}
        </fieldset>
      )}
      <p className="text-xs text-slate-500" data-testid="canonical-note">
        {canonicalNote}
      </p>

      <div className="flex flex-wrap gap-2">
        <button type="button" disabled={busy} onClick={applyCorrection} className="rounded-sm border border-slate-600 px-2 py-1 text-xs hover:bg-slate-800 disabled:opacity-50">
          Stage correction
        </button>
        <button type="button" disabled={busy || !candidate.can_undo} onClick={() => onUndo(candidate)} className="rounded-sm border border-slate-600 px-2 py-1 text-xs hover:bg-slate-800 disabled:opacity-40">
          Undo last correction
        </button>
      </div>

      {(type === "rack" || type === "equipment") && (
        <fieldset className="space-y-2 rounded-sm bg-slate-800/40 p-2">
          <legend className="text-xs text-slate-500">{type === "rack" ? "Match to an existing rack" : "Match to floor-standing equipment"}</legend>
          {type === "rack" && candidate.match_status !== "not_applicable" && (
            <p className="text-xs" data-testid="match-summary">
              <span className={`rounded-sm px-1.5 py-0.5 ${MATCH_BADGE[candidate.match_status].cls}`}>{MATCH_BADGE[candidate.match_status].label}</span>
              {candidate.matched_asset_name && <span className="ml-2 text-slate-300">suggested: {candidate.matched_asset_name}{candidate.match_score != null && ` (${Math.round(candidate.match_score * 100)}%)`}</span>}
            </p>
          )}
          <label className="block text-xs text-slate-400">
            {type === "rack" ? "Rack" : "Equipment"}
            <select aria-label={type === "rack" ? "Matched rack" : "Matched equipment"} value={matchId} onChange={(e) => setMatchId(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm">
              <option value="">none (record the drawn shape only)</option>
              {(type === "rack" ? racks.map((r) => ({ id: r.id, name: `${r.name} (${r.asset_tag})` })) : equipment.map((e) => ({ id: e.id, name: `${e.hostname ?? e.asset_tag}` }))).map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                </option>
              ))}
            </select>
          </label>
          {matchId && (
            <div role="radiogroup" aria-label="Placement action" className="space-y-1 text-xs text-slate-300">
              <label className="flex items-center gap-2">
                <input type="radio" name={`${id}-placement`} checked={placement === "record"} onChange={() => setPlacement("record")} />
                Record the drawn shape only; do not touch the asset's recorded position
              </label>
              <label className="flex items-center gap-2">
                <input type="radio" name={`${id}-placement`} checked={placement === "link"} onChange={() => setPlacement("link")} />
                Link the shape to the asset's existing placement (its position stays as recorded)
              </label>
              {type === "rack" && (
                <label className="flex items-center gap-2">
                  <input type="radio" name={`${id}-placement`} checked={placement === "move"} onChange={() => setPlacement("move")} />
                  Move/place the rack at this shape's position
                </label>
              )}
              {placement === "move" && matchedRack && (
                <label className="ml-6 flex items-center gap-2 text-yellow-300">
                  <input type="checkbox" checked={confirmMove} onChange={(e) => setConfirmMove(e.target.checked)} />
                  I confirm moving {matchedRack.name} from {matchedRack.x_mm == null ? "no recorded position" : `${matchedRack.x_mm}, ${matchedRack.y_mm} mm`} to{" "}
                  {candidate.canonical ? `${candidate.canonical.x_mm}, ${candidate.canonical.y_mm} mm` : "the calibrated position"}
                </label>
              )}
            </div>
          )}
        </fieldset>
      )}

      <details className="text-xs text-slate-400">
        <summary className="cursor-pointer">Evidence ({evidence.length})</summary>
        <ul className="mt-1 list-disc space-y-0.5 pl-5" data-testid="evidence-list">
          {evidence.map((e, i) => (
            <li key={`${e.code}${i}`}>
              <span className="text-slate-300">{e.phase === "match" ? "match" : "detection"} · {e.code.replace(/_/g, " ")}</span>: {e.detail}
              {e.score != null && ` (${Math.round(e.score * 100)}%)`}
            </li>
          ))}
          {evidence.length === 0 && <li>No evidence recorded.</li>}
        </ul>
      </details>

      <label className="block text-xs text-slate-400">
        Outside the room boundary? Record why (only needed when the server refuses the position)
        <input aria-label="Boundary exception reason" value={boundaryReason} maxLength={500} onChange={(e) => setBoundaryReason(e.target.value)} className="mt-1 w-full rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
      </label>

      {error && (
        <p role="alert" className="text-xs text-red-400" data-testid="candidate-error">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="button"
          disabled={busy || !type || !calibration || candidate.match_status === "duplicate" || (placement === "move" && !confirmMove)}
          onClick={accept}
          className="rounded-sm bg-green-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-600 disabled:opacity-40"
        >
          Accept as authoritative geometry
        </button>
        <button type="button" disabled={busy} onClick={() => onReject(candidate)} className="rounded-sm border border-red-800 px-3 py-1.5 text-xs text-red-300 hover:bg-red-950 disabled:opacity-50">
          Reject
        </button>
      </div>
      {!calibration && <p className="text-xs text-yellow-400">Calibrate the drawing first; accepting is disabled until a scale exists.</p>}
      {candidate.match_status === "duplicate" && <p className="text-xs text-purple-300">This shape overlaps one you already accepted. Reject it rather than creating a duplicate.</p>}
    </div>
  );
}
