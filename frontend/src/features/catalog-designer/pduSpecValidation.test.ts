import { describe, expect, it } from "vitest";

import { nextOutletNumber, OutletRow, rowsFromDraft, validatePduInput } from "@/features/catalog-designer/pduSpecValidation";
import { PDUDraft } from "@/types";

function makeDraft(overrides: Partial<PDUDraft> = {}): PDUDraft {
  return {
    revision_id: "rev-1",
    lifecycle_status: "draft",
    pdu_input_voltage_v: 230,
    pdu_input_current_a: 16,
    pdu_phase: "single",
    mounting_orientation: "vertical",
    outlets: [
      {
        id: "outlet-1",
        stable_key: "c13-1",
        outlet_number: 1,
        label: "Outlet 1",
        outlet_type: "iec_c13",
        outlet_count: 1,
        phase: "single",
        rated_voltage_v: null,
        rated_current_a: null,
        position: null,
        side: "front",
        sort_order: 0,
      },
    ],
    version: 1,
    ...overrides,
  };
}

function makeRow(overrides: Partial<OutletRow> = {}): OutletRow {
  return {
    reactKey: "r1",
    stable_key: "c13-1",
    outlet_number: "1",
    label: "Outlet 1",
    outlet_type: "iec_c13",
    outlet_count: "1",
    phase: "single",
    rated_voltage_v: "",
    rated_current_a: "",
    side: "front",
    position: "",
    ...overrides,
  };
}

describe("validatePduInput", () => {
  it("accepts a valid specification, including empty outlet-rated values", () => {
    expect(validatePduInput("230", "16", "single", "vertical", [makeRow()])).toEqual([]);
  });

  it("accepts an entirely empty draft — publish-time requiredness is not an edit-time rule", () => {
    expect(validatePduInput("", "", "", "", [])).toEqual([]);
  });

  it("mirrors the server's electrical messages", () => {
    expect(validatePduInput("-1", "0", "", "", [])).toEqual([
      "Input voltage must be >= 0.",
      "Input current must be > 0.",
    ]);
  });

  it("rejects values outside the allowed vocabularies (defence in depth behind the selects)", () => {
    expect(validatePduInput("", "", "dc", "upside-down", [])).toEqual([
      `Phase must be one of (single, three).`,
      "Invalid mounting orientation",
    ]);
  });

  it("requires an integer outlet number and a stable key per outlet", () => {
    const errors = validatePduInput("", "", "", "", [makeRow({ outlet_number: "", stable_key: "  " })]);
    expect(errors).toContain("outlets[0].outlet_number required");
    expect(errors).toContain("outlets[0].stable_key required");
  });

  it("reports each duplicated outlet number exactly once", () => {
    const rows = [makeRow({ reactKey: "a" }), makeRow({ reactKey: "b" }), makeRow({ reactKey: "c" })];
    expect(validatePduInput("", "", "", "", rows).filter((e) => e.startsWith("Duplicate"))).toEqual([
      "Duplicate outlet number 1",
    ]);
  });

  it("rejects non-positive outlet quantities and ratings", () => {
    const errors = validatePduInput("", "", "", "", [
      makeRow({ outlet_count: "0", rated_current_a: "0", rated_voltage_v: "-5" }),
    ]);
    expect(errors).toEqual([
      "outlets[0].rated_voltage_v: invalid voltage",
      "outlets[0].rated_current_a: invalid current",
      "outlets[0].outlet_count: count must be > 0",
    ]);
  });
});

describe("rowsFromDraft / nextOutletNumber", () => {
  it("keeps server values as strings and nulls as empty inputs", () => {
    const [row] = rowsFromDraft(makeDraft());
    expect(row).toEqual(expect.objectContaining({ stable_key: "c13-1", outlet_number: "1", rated_voltage_v: "" }));
  });

  it("continues numbering past the highest existing outlet number", () => {
    expect(nextOutletNumber(rowsFromDraft(makeDraft()))).toBe(2);
    expect(nextOutletNumber([])).toBe(1);
  });
});
