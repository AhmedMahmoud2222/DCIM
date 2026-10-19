/** Client-side mirror of `validate_pdu_revision` (backend/app/application/pdu_catalog/
 * validators.py) for the PDU spec editor (Issue #150 M5). Kept in its own module so the
 * component file exports components only (react-refresh) and the rules can be unit
 * tested without rendering. */
import { PDUDraft, PDU_MOUNTING_ORIENTATIONS, PDU_PHASES } from "@/types";

/** Editable row state for one outlet template. All numeric fields are kept as strings so
 * an intermediate value like `""` or `-` can exist while typing without the controlled
 * input fighting the user; conversion to numbers happens at save time. */
export interface OutletRow {
  reactKey: string;
  stable_key: string;
  outlet_number: string;
  label: string;
  outlet_type: string;
  outlet_count: string;
  phase: string;
  rated_voltage_v: string;
  rated_current_a: string;
  side: string;
  position: string;
}

export function rowsFromDraft(draft: PDUDraft): OutletRow[] {
  return draft.outlets.map((o, i) => ({
    reactKey: `${o.stable_key}-${o.outlet_number}-${i}`,
    stable_key: o.stable_key,
    outlet_number: String(o.outlet_number),
    label: o.label,
    outlet_type: o.outlet_type,
    outlet_count: String(o.outlet_count),
    phase: o.phase,
    rated_voltage_v: o.rated_voltage_v === null ? "" : String(o.rated_voltage_v),
    rated_current_a: o.rated_current_a === null ? "" : String(o.rated_current_a),
    side: o.side,
    position: o.position ?? "",
  }));
}

export function nextOutletNumber(rows: OutletRow[]): number {
  let max = 0;
  for (const row of rows) {
    const n = Number(row.outlet_number);
    if (Number.isInteger(n) && n > max) max = n;
  }
  return max + 1;
}

/** Publish-time requiredness (rated voltage/current, phase, mounting, ≥1 outlet) is
 * deliberately NOT enforced here: the backend only requires those at publish, and
 * partial drafts are legal until then. Messages match the server's wording. */
export function validatePduInput(
  voltage: string,
  current: string,
  phase: string,
  mounting: string,
  rows: OutletRow[],
): string[] {
  const errors: string[] = [];
  if (voltage.trim() !== "" && Number(voltage) < 0) errors.push("Input voltage must be >= 0.");
  if (current.trim() !== "" && Number(current) <= 0) errors.push("Input current must be > 0.");
  if (phase !== "" && !PDU_PHASES.includes(phase as (typeof PDU_PHASES)[number]))
    errors.push(`Phase must be one of (${PDU_PHASES.join(", ")}).`);
  if (mounting !== "" && !PDU_MOUNTING_ORIENTATIONS.includes(mounting as (typeof PDU_MOUNTING_ORIENTATIONS)[number]))
    errors.push("Invalid mounting orientation");

  const seen = new Set<number>();
  const reported = new Set<number>();
  rows.forEach((row, i) => {
    if (row.stable_key.trim() === "") errors.push(`outlets[${i}].stable_key required`);
    const n = Number(row.outlet_number);
    if (row.outlet_number.trim() === "" || !Number.isInteger(n)) {
      errors.push(`outlets[${i}].outlet_number required`);
    } else if (seen.has(n)) {
      // The server reports each duplicated value once — mirror that instead of
      // repeating the same message for every extra copy.
      if (!reported.has(n)) {
        errors.push(`Duplicate outlet number ${n}`);
        reported.add(n);
      }
    } else {
      seen.add(n);
    }
    if (row.rated_voltage_v.trim() !== "" && Number(row.rated_voltage_v) < 0)
      errors.push(`outlets[${i}].rated_voltage_v: invalid voltage`);
    if (row.rated_current_a.trim() !== "" && Number(row.rated_current_a) <= 0)
      errors.push(`outlets[${i}].rated_current_a: invalid current`);
    if (row.outlet_count.trim() === "" || !Number.isInteger(Number(row.outlet_count)) || Number(row.outlet_count) <= 0)
      errors.push(`outlets[${i}].outlet_count: count must be > 0`);
  });
  return errors;
}
