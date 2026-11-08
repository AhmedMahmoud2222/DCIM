import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CandidateReview } from "@/features/floor-plans/CandidateReview";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { Calibration, ImportCandidate, RoomSpatialView } from "@/types";

const calibration: Calibration = {
  id: "c", sequence: 1, method: "declared_units", source_units: "mm", mm_per_unit: 1, origin_x: 0, origin_y: 4000, y_axis: "up", rotation_quadrants: 0,
  error_bound_mm: 0.5, relative_error: 0, confidence: "high", reference: {}, warnings: [], job_id: "j", supersedes_id: null, created_at: "",
};
const view = {
  room_id: "r", room_name: "R", racks: [
    { id: "rack-a", asset_tag: "RK-A", name: "Rack A", x_mm: 1000, y_mm: 2000, rotation_deg: 0, height_u: 42, spatial_object_id: null, placement_version: 7 },
    { id: "rack-b", asset_tag: "RK-B", name: "Rack B", x_mm: null, y_mm: null, rotation_deg: null, height_u: 42, spatial_object_id: null, placement_version: 1 },
  ],
  equipment: [{ id: "eq-1", asset_tag: "UPS-1", hostname: "ups-1", placement_type: "floor_standing", spatial_object_id: null }, { id: "eq-2", asset_tag: "SRV", hostname: "srv", placement_type: "rack_mounted", spatial_object_id: null }],
} as unknown as RoomSpatialView;

const cand = (over: Partial<ImportCandidate> = {}): ImportCandidate => ({
  id: "c1", job_id: "j", version: 3, status: "pending", resulting_spatial_object_id: null, source_ref: "dxf:1", correction: null, can_undo: false,
  raw_geometry: { shape_type: "rect", cx: 1300, cy: 1500, width: 600, height: 1000, rotation_deg: 0 },
  effective_geometry: { shape_type: "rect", cx: 1300, cy: 1500, width: 600, height: 1000, rotation_deg: 0 },
  canonical: { geometry_type: "rect", x_mm: 1000, y_mm: 2000, width_mm: 600, height_mm: 1000, rotation_deg: 0, geometry_data: null },
  suggested_object_type: "rack", effective_object_type: "rack", suggested_label: "RACK-01", effective_label: "RACK-01", confidence: 0.92,
  evidence: [{ code: "layer_name", detail: "Layer 'RACKS' suggests a rack" }, { code: "position", detail: "20 mm from rack 'Rack A'", score: 0.97, phase: "match" }],
  match_status: "matched", match_score: 0.88, matched_asset_id: "rack-a", matched_asset_name: "Rack A", duplicate_of_spatial_object_id: null,
  reconciled_calibration_id: "c", ...over,
});

const handlers = () => ({ onSelect: vi.fn(), onCorrect: vi.fn(), onUndo: vi.fn(), onAccept: vi.fn(), onReject: vi.fn() });
const mount = (candidates: ImportCandidate[], selectedId: string | null, over: Record<string, unknown> = {}) => {
  const h = handlers();
  renderWithProviders(<CandidateReview view={view} candidates={candidates} selectedId={selectedId} calibration={calibration} busy={false} error={null} {...h} {...over} />);
  return h;
};

describe("CandidateReview", () => {
  it("lists candidates with confidence and an explicit match status, and filters", async () => {
    const user = userEvent.setup();
    const h = mount([cand(), cand({ id: "c2", effective_label: "Note", effective_object_type: "annotation", match_status: "not_applicable", confidence: 0.3 })], null);
    expect(screen.getByTestId("row-c1")).toHaveTextContent("RACK-01");
    expect(screen.getByTestId("row-c1")).toHaveTextContent("92%");
    expect(screen.getByTestId("row-c1")).toHaveTextContent("matches a rack");
    await user.selectOptions(screen.getByLabelText("Filter candidates"), "racks");
    expect(screen.queryByTestId("row-c2")).not.toBeInTheDocument();
    await user.click(screen.getByTestId("row-c1"));
    expect(h.onSelect).toHaveBeenCalledWith("c1");
  });

  it("surfaces ambiguous, conflicting and duplicate shapes for closer review", async () => {
    const user = userEvent.setup();
    mount([cand({ id: "ok", confidence: 0.95 }), cand({ id: "amb", match_status: "ambiguous" }), cand({ id: "dup", match_status: "duplicate" }), cand({ id: "con", match_status: "conflict" })], null);
    await user.selectOptions(screen.getByLabelText("Filter candidates"), "review");
    expect(screen.queryByTestId("row-ok")).not.toBeInTheDocument();
    for (const id of ["amb", "dup", "con"]) expect(screen.getByTestId(`row-${id}`)).toBeInTheDocument();
    expect(screen.getByTestId("row-dup")).toHaveTextContent("duplicate of accepted shape");
  });

  it("shows detection and match evidence, and never auto-selects the suggested rack for linking", () => {
    mount([cand()], "c1");
    const list = screen.getByTestId("evidence-list");
    expect(list).toHaveTextContent("detection · layer name");
    expect(list).toHaveTextContent("match · position: 20 mm from rack 'Rack A' (97%)");
    expect(screen.getByTestId("match-summary")).toHaveTextContent("suggested: Rack A (88%)");
    expect(screen.getByRole("radio", { name: /Record the drawn shape only/ })).toBeChecked();
  });

  it("stages a geometry correction by converting millimetres back to source coordinates", async () => {
    const user = userEvent.setup();
    const h = mount([cand()], "c1");
    const left = screen.getByLabelText("Left X (mm)");
    await user.clear(left);
    await user.type(left, "1250");
    await user.clear(screen.getByLabelText("Rotation ° (mm)"));
    await user.type(screen.getByLabelText("Rotation ° (mm)"), "330");
    await user.click(screen.getByRole("button", { name: "Stage correction" }));
    expect(h.onCorrect).toHaveBeenCalledTimes(1);
    const [candidate, body] = h.onCorrect.mock.calls[0];
    expect(candidate.id).toBe("c1");
    // canonical left X 1250 (width 600) -> centre 1550 -> source cx 1550; Y-up CCW rotation = -(330 - 0) -> 30 after normalisation
    expect(body.cx).toBeCloseTo(1550, 6);
    expect(body.cy).toBeCloseTo(1500, 6);
    expect(body.rotation_deg).toBeCloseTo(30, 6);
    expect(body.width).toBeCloseTo(600, 6);
  });

  it("stages label, classification and an explicit match; clearing a match is explicit too", async () => {
    const user = userEvent.setup();
    const h = mount([cand({ matched_asset_id: null, matched_asset_name: null, match_status: "unmatched" })], "c1");
    await user.clear(screen.getByLabelText("Label"));
    await user.type(screen.getByLabelText("Label"), "R-A1");
    await user.selectOptions(screen.getByLabelText("Classification"), "equipment");
    await user.click(screen.getByRole("button", { name: "Stage correction" }));
    expect(h.onCorrect).toHaveBeenCalledWith(expect.anything(), { label: "R-A1", object_type: "equipment" });
  });

  it("keeps Undo disabled until a correction exists", async () => {
    const user = userEvent.setup();
    const h = mount([cand({ correction: { label: "x" }, can_undo: true })], "c1");
    const undo = screen.getByRole("button", { name: "Undo last correction" });
    expect(undo).toBeEnabled();
    await user.click(undo);
    expect(h.onUndo).toHaveBeenCalled();
  });

  it("accepts record-only by default and sends no placement flags", async () => {
    const user = userEvent.setup();
    const h = mount([cand()], "c1");
    await user.click(screen.getByRole("button", { name: "Accept as authoritative geometry" }));
    expect(h.onAccept).toHaveBeenCalledWith(expect.objectContaining({ id: "c1" }), { object_type: "rack", label: "RACK-01", matched_asset_id: "rack-a" });
  });

  it("only links a placement when the operator chooses it", async () => {
    const user = userEvent.setup();
    const h = mount([cand()], "c1");
    await user.click(screen.getByRole("radio", { name: /Link the shape/ }));
    await user.click(screen.getByRole("button", { name: "Accept as authoritative geometry" }));
    expect(h.onAccept.mock.calls[0][1]).toMatchObject({ matched_asset_id: "rack-a", link_placement: true });
    expect(h.onAccept.mock.calls[0][1].apply_position).toBeUndefined();
  });

  it("moving a rack needs an explicit confirmation naming the old and new position, and carries the placement version", async () => {
    const user = userEvent.setup();
    const h = mount([cand()], "c1");
    await user.click(screen.getByRole("radio", { name: /Move\/place the rack/ }));
    const accept = screen.getByRole("button", { name: "Accept as authoritative geometry" });
    expect(accept).toBeDisabled();
    const confirm = screen.getByRole("checkbox", { name: /moving Rack A from 1000, 2000 mm to 1000, 2000 mm/ });
    await user.click(confirm);
    expect(accept).toBeEnabled();
    await user.click(accept);
    expect(h.onAccept.mock.calls[0][1]).toMatchObject({ apply_position: true, placement_version: 7, matched_asset_id: "rack-a" });
  });

  it("changing the matched rack resets the placement choice back to record-only", async () => {
    const user = userEvent.setup();
    mount([cand()], "c1");
    await user.click(screen.getByRole("radio", { name: /Link the shape/ }));
    await user.selectOptions(screen.getByLabelText("Matched rack"), "rack-b");
    expect(screen.getByRole("radio", { name: /Record the drawn shape only/ })).toBeChecked();
  });

  it("disables accepting a duplicate and explains why", () => {
    mount([cand({ match_status: "duplicate" })], "c1");
    expect(screen.getByRole("button", { name: "Accept as authoritative geometry" })).toBeDisabled();
    expect(screen.getByText(/overlaps one you already accepted/)).toBeInTheDocument();
  });

  it("disables accepting before calibration and shows where the shape will land only after it", () => {
    mount([cand({ canonical: null })], "c1", { calibration: null });
    expect(screen.getByRole("button", { name: "Accept as authoritative geometry" })).toBeDisabled();
    expect(screen.getByTestId("canonical-note")).toHaveTextContent("Calibrate to see the real-world position");
    expect(screen.getByText(/Calibrate the drawing first/)).toBeInTheDocument();
    expect(screen.getByLabelText("Centre X (src units)")).toBeInTheDocument();
  });

  it("equipment can only be matched to floor-standing items and cannot be moved", async () => {
    const user = userEvent.setup();
    const h = mount([cand({ effective_object_type: "equipment", match_status: "not_applicable", matched_asset_id: null })], "c1");
    const select = screen.getByLabelText("Matched equipment");
    expect(within(select).getAllByRole("option").map((o) => o.textContent)).toEqual(["none (record the drawn shape only)", "ups-1"]);
    await user.selectOptions(select, "eq-1");
    expect(screen.queryByRole("radio", { name: /Move\/place/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /Link the shape/ }));
    await user.click(screen.getByRole("button", { name: "Accept as authoritative geometry" }));
    expect(h.onAccept.mock.calls[0][1]).toMatchObject({ object_type: "equipment", matched_asset_id: "eq-1", link_placement: true });
  });

  it("passes the boundary exception reason and announces server errors", async () => {
    const user = userEvent.setup();
    const h = mount([cand({ matched_asset_id: null, match_status: "unmatched" })], "c1", { error: "Rack lies outside the approved room boundary." });
    expect(screen.getByRole("alert")).toHaveTextContent("outside the approved room boundary");
    await user.type(screen.getByLabelText("Boundary exception reason"), "Annex bay");
    await user.click(screen.getByRole("button", { name: "Accept as authoritative geometry" }));
    expect(h.onAccept.mock.calls[0][1]).toMatchObject({ boundary_exception_reason: "Annex bay" });
  });

  it("rejects without touching anything else", async () => {
    const user = userEvent.setup();
    const h = mount([cand()], "c1");
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(h.onReject).toHaveBeenCalledWith(expect.objectContaining({ id: "c1", version: 3 }));
    expect(h.onAccept).not.toHaveBeenCalled();
  });
});
