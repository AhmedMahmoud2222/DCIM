import { fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ImportReviewCanvas } from "@/features/floor-plans/ImportReviewCanvas";
import { provisionalCalibration } from "@/features/floor-plans/spatialMath";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { Calibration, ImportCandidate, SourceGeometry } from "@/types";

const source: SourceGeometry = {
  job_id: "j", source_format: "dxf", source_units: "mm", units_trusted: true, y_axis: "up", layers: [], sir_sha256: "x",
  bbox: { min_x: 0, min_y: 0, max_x: 6000, max_y: 4000 }, total_entities: 1, truncated: false,
  entities: [{ kind: "rect", ref: "w", cx: 3000, cy: 2000, width: 6000, height: 4000 }],
};
const calibration: Calibration = {
  id: "c", sequence: 1, method: "declared_units", source_units: "mm", mm_per_unit: 1, origin_x: 0, origin_y: 4000, y_axis: "up", rotation_quadrants: 0,
  error_bound_mm: 0.5, relative_error: 0, confidence: "high", reference: {}, warnings: [], job_id: "j", supersedes_id: null, created_at: "",
};
const candidate = (over: Partial<ImportCandidate> = {}): ImportCandidate => ({
  id: "c1", job_id: "j", version: 1, status: "pending", resulting_spatial_object_id: null, source_ref: "dxf:1", correction: null, can_undo: false,
  raw_geometry: { shape_type: "rect", cx: 1300, cy: 1500, width: 600, height: 1000, rotation_deg: 0 },
  effective_geometry: { shape_type: "rect", cx: 1300, cy: 1500, width: 600, height: 1000, rotation_deg: 0 },
  canonical: null, suggested_object_type: "rack", effective_object_type: "rack", suggested_label: "RACK-01", effective_label: "RACK-01",
  confidence: 0.9, evidence: [], match_status: "unmatched", match_score: null, matched_asset_id: null, matched_asset_name: null,
  duplicate_of_spatial_object_id: null, reconciled_calibration_id: null, ...over,
});
const baseProps = { source, candidates: [candidate()], calibration, selectedId: null, onSelect: vi.fn(), onMove: vi.fn(), pickMode: "none" as const, picks: [], onPick: vi.fn(), showGrid: true, snapMm: 0 };

describe("ImportReviewCanvas", () => {
  it("labels an uncalibrated view and never claims millimetres", () => {
    renderWithProviders(<ImportReviewCanvas {...baseProps} calibration={null} />);
    expect(screen.getByTestId("import-review-canvas")).toHaveAttribute("data-calibrated", "false");
    expect(screen.getByRole("status")).toHaveTextContent(/Uncalibrated view in the drawing's own units \(mm\)/);
    expect(provisionalCalibration(source)).toMatchObject({ mm_per_unit: 1, origin_y: 4000, y_axis: "up" });
  });

  it("states the calibrated view uses real-world millimetres", () => {
    renderWithProviders(<ImportReviewCanvas {...baseProps} />);
    expect(screen.getByRole("status")).toHaveTextContent("Calibrated view: distances are real-world millimetres.");
  });

  it("reports picked points in source coordinates (two-point calibration)", () => {
    const onPick = vi.fn();
    renderWithProviders(<ImportReviewCanvas {...baseProps} pickMode="two-point" onPick={onPick} />);
    const svg = screen.getByRole("group", { name: "Import review canvas" });
    svg.getBoundingClientRect = () => ({ left: 0, top: 0, width: svg.getAttribute("width") ? Number(svg.getAttribute("width")) : 880, height: 600, right: 0, bottom: 0, x: 0, y: 0, toJSON: () => ({}) });
    fireEvent.pointerDown(svg, { clientX: 44, clientY: 44, pointerId: 1 });
    expect(onPick).toHaveBeenCalledTimes(1);
    const [x, y] = onPick.mock.calls[0][0];
    // the top-left of the fitted drawing (padding 44 px) is the source point (0, 4000): origin at the top of a Y-up drawing
    expect(x).toBeCloseTo(0, 0);
    expect(y).toBeCloseTo(4000, 0);
    expect(screen.getByRole("status")).toHaveTextContent("Click the two reference points on the drawing.");
  });

  it("does not report picks when not in a pick mode", () => {
    const onPick = vi.fn();
    renderWithProviders(<ImportReviewCanvas {...baseProps} onPick={onPick} />);
    fireEvent.pointerDown(screen.getByRole("group", { name: "Import review canvas" }), { clientX: 10, clientY: 10, pointerId: 1 });
    expect(onPick).not.toHaveBeenCalled();
  });

  it("moves a candidate from the keyboard, honouring the snap interval, and in source coordinates", async () => {
    const onMove = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<ImportReviewCanvas {...baseProps} snapMm={50} onMove={onMove} />);
    const shape = screen.getByRole("button", { name: /Candidate RACK-01, rack, unmatched/ });
    shape.focus();
    await user.keyboard("{ArrowRight}");
    // centre (1300,1500) = canonical (1300, 2500); +50 mm snapped -> (1350, 2500) -> source (1350, 1500)
    expect(onMove).toHaveBeenCalledWith(expect.objectContaining({ id: "c1" }), { cx: 1350, cy: 1500 });
    await user.keyboard("{Shift>}{ArrowDown}{/Shift}");
    expect(onMove).toHaveBeenLastCalledWith(expect.anything(), { cx: 1300, cy: 1000 }); // +500 mm down = -500 in Y-up source
  });

  it("selects on focus and exposes the match status to assistive technology", () => {
    const onSelect = vi.fn();
    renderWithProviders(<ImportReviewCanvas {...baseProps} candidates={[candidate({ match_status: "conflict" })]} onSelect={onSelect} />);
    const shape = screen.getByRole("button", { name: /conflict/ });
    shape.focus();
    expect(onSelect).toHaveBeenCalledWith("c1");
  });

  it("draws the rectangle at its calibrated size (real proportions)", () => {
    renderWithProviders(<ImportReviewCanvas {...baseProps} />);
    const rect = screen.getByTestId("candidate-c1").querySelector("rect")!;
    expect(Number(rect.getAttribute("width")) / Number(rect.getAttribute("height"))).toBeCloseTo(0.6, 3);
  });
});
