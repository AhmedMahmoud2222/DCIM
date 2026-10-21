import { useMutation } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { PDUOutletInput, PDUDraftUpdateInput, updatePduDraft } from "@/features/catalog-designer/api";
import { nextOutletNumber, OutletRow, rowsFromDraft, validatePduInput } from "@/features/catalog-designer/pduSpecValidation";
import { ApiError } from "@/lib/apiClient";
import {
  CatalogModelRevisionDetail,
  PDUDraft,
  PDU_MOUNTING_ORIENTATIONS,
  PDU_OUTLET_TYPES,
  PDU_PHASES,
  PDU_SIDES,
} from "@/types";

/** The backend answers a failed PATCH with a 422 whose detail is the JSON-encoded
 * `ValidationSummary` (ValueError → HTTPException). Decode it for display; anything
 * else shows raw. */
function describeApiError(error: unknown): string {
  if (!(error instanceof ApiError)) return (error as Error).message;
  if (error.status === 409)
    return "This revision was changed outside this session (optimistic-concurrency conflict). Reload to pick up the latest version, then re-apply your changes.";
  try {
    const parsed = JSON.parse(error.detail) as { errors?: { field: string; message: string }[] };
    if (Array.isArray(parsed.errors)) return parsed.errors.map((e) => `${e.field}: ${e.message}`).join(" · ");
  } catch {
    // detail is not the JSON validation payload — fall through to the raw string
  }
  return error.detail;
}

const inputClass =
  "w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100 disabled:opacity-60";
const labelClass = "block text-[11px] text-slate-400";

/** Issue #150 M5 — the PDU electrical specification + outlet-template editor, embedded
 * in RevisionEditorPage for `category === "pdu"` drafts. Reads from the M2 narrow
 * sub-resource (`GET /catalog/revisions/{id}/pdu`) and saves the whole outlet table in
 * one `PATCH` with `If-Match` against the draft version, matching the backend's
 * wholesale replace persistence. `key` the parent on `draft.version` so a fresh fetch
 * (after save/conflict) reseeds the local state instead of clobbering live edits. */
export function PduSpecPanel({
  revision,
  draft,
  readOnly,
  onSaved,
}: {
  revision: CatalogModelRevisionDetail;
  draft: PDUDraft;
  readOnly: boolean;
  onSaved: () => void;
}) {
  const [voltage, setVoltage] = useState(draft.pdu_input_voltage_v === null ? "" : String(draft.pdu_input_voltage_v));
  const [current, setCurrent] = useState(draft.pdu_input_current_a === null ? "" : String(draft.pdu_input_current_a));
  const [phase, setPhase] = useState(draft.pdu_phase ?? "");
  const [mounting, setMounting] = useState(draft.mounting_orientation ?? "");
  const [rows, setRows] = useState<OutletRow[]>(() => rowsFromDraft(draft));
  const [clientErrors, setClientErrors] = useState<string[]>([]);

  const saveMutation = useMutation({
    mutationFn: (body: PDUDraftUpdateInput) => updatePduDraft(revision.id, body, draft.version),
    onSuccess: () => {
      setClientErrors([]);
      onSaved();
    },
  });

  function buildInput(): PDUDraftUpdateInput {
    return {
      pdu_input_voltage_v: voltage.trim() === "" ? null : Number(voltage),
      pdu_input_current_a: current.trim() === "" ? null : Number(current),
      pdu_phase: phase === "" ? null : phase,
      mounting_orientation: mounting === "" ? null : mounting,
      outlets: rows.map((row, index): PDUOutletInput => {
        const n = Number(row.outlet_number);
        return {
          stable_key: row.stable_key.trim(),
          outlet_number: Number.isInteger(n) ? n : 0,
          label: row.label.trim() || null,
          outlet_type: row.outlet_type,
          outlet_count: Number(row.outlet_count) || 1,
          phase: row.phase,
          rated_voltage_v: row.rated_voltage_v.trim() === "" ? null : Number(row.rated_voltage_v),
          rated_current_a: row.rated_current_a.trim() === "" ? null : Number(row.rated_current_a),
          position: row.position.trim() === "" ? null : row.position.trim(),
          side: row.side,
          sort_order: index,
        };
      }),
    };
  }

  function handleSave(e: FormEvent) {
    e.preventDefault();
    const errors = validatePduInput(voltage, current, phase, mounting, rows);
    if (errors.length > 0) {
      setClientErrors(errors);
      return;
    }
    saveMutation.mutate(buildInput());
  }

  function addOutlet() {
    setRows((currentRows) => {
      const n = nextOutletNumber(currentRows);
      return [
        ...currentRows,
        {
          reactKey: `new-${n}-${currentRows.length}`,
          stable_key: `outlet-${n}`,
          outlet_number: String(n),
          label: `Outlet ${n}`,
          outlet_type: "iec_c13",
          outlet_count: "1",
          phase: phase === "" ? "single" : phase,
          rated_voltage_v: "",
          rated_current_a: "",
          side: "front",
          position: "",
        },
      ];
    });
  }

  function removeOutlet(reactKey: string) {
    setRows((currentRows) => currentRows.filter((r) => r.reactKey !== reactKey));
  }

  function patchRow(reactKey: string, patch: Partial<OutletRow>) {
    setRows((currentRows) => currentRows.map((r) => (r.reactKey === reactKey ? { ...r, ...patch } : r)));
  }

  return (
    <section className="mb-6 rounded-sm border border-slate-800 bg-slate-900 p-4" aria-label="PDU electrical specification">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-300">PDU Electrical Specification</h2>
        {readOnly && <span className="rounded-sm bg-slate-800 px-2 py-0.5 text-[10px] text-slate-400">Read-only</span>}
      </div>

      <form onSubmit={handleSave} className="space-y-4">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
          <label className={labelClass}>
            Rated input voltage (V)
            <input
              type="number"
              step="any"
              value={voltage}
              onChange={(e) => setVoltage(e.target.value)}
              disabled={readOnly}
              placeholder="e.g. 230"
              aria-label="Rated input voltage (V)"
              className={inputClass}
            />
          </label>
          <label className={labelClass}>
            Rated input current (A)
            <input
              type="number"
              step="any"
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
              disabled={readOnly}
              placeholder="e.g. 32"
              aria-label="Rated input current (A)"
              className={inputClass}
            />
          </label>
          <label className={labelClass}>
            Phase
            <select
              value={phase}
              onChange={(e) => setPhase(e.target.value)}
              disabled={readOnly}
              aria-label="Phase"
              className={inputClass}
            >
              <option value="">Select phase…</option>
              {PDU_PHASES.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label className={labelClass}>
            Mounting orientation
            <select
              value={mounting}
              onChange={(e) => setMounting(e.target.value)}
              disabled={readOnly}
              aria-label="Mounting orientation"
              className={inputClass}
            >
              <option value="">Select orientation…</option>
              {PDU_MOUNTING_ORIENTATIONS.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </label>
        </div>

        <p className="rounded-sm border border-slate-800 bg-slate-950 p-2 text-xs leading-relaxed text-slate-400">
          Vertical (0U rail) and horizontal (1U shelf) mounting are <strong className="text-slate-300">catalog
          specification metadata</strong>. This describes how the physical unit mounts; it does not itself create a
          placement record. Placing an instantiated PDU (horizontal rack-mounted or vertical 0U) is done separately
          through the power PDU placement API, after instantiation.
        </p>

        <div>
          <div className="mb-2 flex items-center justify-between">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-400">Outlet templates</h3>
            {!readOnly && (
              <button
                type="button"
                onClick={addOutlet}
                className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700"
              >
                + Add outlet
              </button>
            )}
          </div>

          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-xs">
              <thead>
                <tr className="text-left text-[10px] uppercase tracking-wide text-slate-500">
                  <th className="border-b border-slate-800 px-1 py-1">No.</th>
                  <th className="border-b border-slate-800 px-1 py-1">Stable key</th>
                  <th className="border-b border-slate-800 px-1 py-1">Label</th>
                  <th className="border-b border-slate-800 px-1 py-1">Type</th>
                  <th className="border-b border-slate-800 px-1 py-1">Phase</th>
                  <th className="border-b border-slate-800 px-1 py-1">Side</th>
                  <th
                    className="border-b border-slate-800 px-1 py-1"
                    title="Per-template metadata. Each row is one physical outlet; this count does not multiply outlets at instantiation."
                  >
                    Qty
                  </th>
                  <th className="border-b border-slate-800 px-1 py-1">V</th>
                  <th className="border-b border-slate-800 px-1 py-1">A</th>
                  <th className="border-b border-slate-800 px-1 py-1" />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.reactKey} data-testid="pdu-outlet-row">
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <input
                        type="number"
                        value={row.outlet_number}
                        onChange={(e) => patchRow(row.reactKey, { outlet_number: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} number`}
                        className={`${inputClass} w-16`}
                      />
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <input
                        value={row.stable_key}
                        onChange={(e) => patchRow(row.reactKey, { stable_key: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} stable key`}
                        className={`${inputClass} w-28`}
                      />
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <input
                        value={row.label}
                        onChange={(e) => patchRow(row.reactKey, { label: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} label`}
                        className={`${inputClass} w-32`}
                      />
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <select
                        value={row.outlet_type}
                        onChange={(e) => patchRow(row.reactKey, { outlet_type: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} type`}
                        className={`${inputClass} w-28`}
                      >
                        {PDU_OUTLET_TYPES.map((t) => (
                          <option key={t} value={t}>
                            {t}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <select
                        value={row.phase}
                        onChange={(e) => patchRow(row.reactKey, { phase: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} phase`}
                        className={`${inputClass} w-20`}
                      >
                        {PDU_PHASES.map((p) => (
                          <option key={p} value={p}>
                            {p}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <select
                        value={row.side}
                        onChange={(e) => patchRow(row.reactKey, { side: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} side`}
                        className={`${inputClass} w-20`}
                      >
                        {PDU_SIDES.map((s) => (
                          <option key={s} value={s}>
                            {s}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <input
                        type="number"
                        value={row.outlet_count}
                        onChange={(e) => patchRow(row.reactKey, { outlet_count: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} count`}
                        className={`${inputClass} w-14`}
                      />
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <input
                        type="number"
                        step="any"
                        value={row.rated_voltage_v}
                        onChange={(e) => patchRow(row.reactKey, { rated_voltage_v: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} rated voltage`}
                        className={`${inputClass} w-16`}
                      />
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1">
                      <input
                        type="number"
                        step="any"
                        value={row.rated_current_a}
                        onChange={(e) => patchRow(row.reactKey, { rated_current_a: e.target.value })}
                        disabled={readOnly}
                        aria-label={`Outlet ${row.outlet_number} rated current`}
                        className={`${inputClass} w-14`}
                      />
                    </td>
                    <td className="border-b border-slate-800/60 px-1 py-1 text-right">
                      {!readOnly && (
                        <button
                          type="button"
                          onClick={() => removeOutlet(row.reactKey)}
                          className="text-red-400 hover:text-red-300"
                          aria-label={`Remove outlet ${row.outlet_number}`}
                        >
                          Remove
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td colSpan={10} className="px-1 py-2 italic text-slate-500">
                      No outlet templates yet — a published PDU revision needs at least one.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        {clientErrors.length > 0 && (
          <ul className="space-y-1 text-xs text-red-400" aria-label="PDU validation errors">
            {clientErrors.map((err, i) => (
              <li key={`pdu-err-${i}`}>{err}</li>
            ))}
          </ul>
        )}

        {!readOnly && (
          <div className="flex items-center gap-3">
            <button
              type="submit"
              disabled={saveMutation.isPending}
              className="rounded-sm bg-blue-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              {saveMutation.isPending ? "Saving…" : "Save PDU specification"}
            </button>
            <span className="text-[11px] text-slate-500">
              Saved against If-Match version {draft.version}. Publish readiness (rated voltage/current, phase,
              mounting, ≥1 outlet) is checked by Validate &amp; Publish.
            </span>
          </div>
        )}
        {saveMutation.isError && (
          <p className="text-xs text-red-400" role="alert">
            {describeApiError(saveMutation.error)}
          </p>
        )}
      </form>
    </section>
  );
}
