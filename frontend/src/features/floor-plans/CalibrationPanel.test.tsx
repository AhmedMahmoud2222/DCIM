import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { CalibrationPanel } from "@/features/floor-plans/CalibrationPanel";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { FloorPlan, ImportDiagnostics } from "@/types";

const plan: FloorPlan = {
  id: "fp", room_id: "r", revision_number: 1, status: "draft", source_file_name: null, source_format: null, calibration_scale_mm_per_px: null,
  room_width_mm: null, room_height_mm: null, version: 4, created_at: "", current_calibration: null,
};
const dxfDiag = { job_id: "j", source_units: "mm", units_trusted: true } as ImportDiagnostics;
const svgDiag = { job_id: "j", source_units: "px", units_trusted: false } as ImportDiagnostics;

const props = (over = {}) => ({
  floorPlan: plan, jobId: "j", diagnostics: dxfDiag, picks: [] as [number, number][], pickMode: "none" as const, onPickMode: vi.fn(), onClearPicks: vi.fn(),
  origin: null, onClearOrigin: vi.fn(), pending: false, error: null, locked: false, onSubmit: vi.fn(), ...over,
});

describe("CalibrationPanel", () => {
  it("defaults to the file's declared units when they are usable and submits just that", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<CalibrationPanel {...props({ onSubmit })} />);
    expect(screen.getByRole("radio", { name: /declared units \(mm\)/ })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Set calibration" }));
    expect(onSubmit).toHaveBeenCalledWith({ method: "declared_units", job_id: "j", rotation_degrees: 0 });
  });

  it("disables declared units for a pixel drawing and requires two picked points plus a real distance", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    const { rerender } = renderWithProviders(<CalibrationPanel {...props({ diagnostics: svgDiag, onSubmit })} />);
    expect(screen.getByRole("radio", { name: /declared units/ })).toBeDisabled();
    expect(screen.getByRole("radio", { name: /Two points/ })).toBeChecked();
    expect(screen.getByRole("button", { name: "Set calibration" })).toBeDisabled();

    rerender(<CalibrationPanel {...props({ diagnostics: svgDiag, onSubmit, picks: [[0, 0], [800, 0]] })} />);
    await user.type(screen.getByLabelText("Real distance (mm)"), "8000");
    expect(screen.getByTestId("scale-preview")).toHaveTextContent("1 source unit = 10.000 mm");
    await user.click(screen.getByRole("button", { name: "Set calibration" }));
    expect(onSubmit).toHaveBeenCalledWith({
      method: "two_point", job_id: "j", rotation_degrees: 0, p1: [0, 0], p2: [800, 0], distance_mm: 8000, tolerance_mm: 1, pick_tolerance_src: 0,
    });
  });

  it("sends the picked origin and the plan rotation", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<CalibrationPanel {...props({ onSubmit, origin: [10, 20] })} />);
    expect(screen.getByTestId("origin-status")).toHaveTextContent("origin at (10.0, 20.0)");
    await user.selectOptions(screen.getByLabelText("Rotate plan"), "90");
    await user.click(screen.getByRole("button", { name: "Set calibration" }));
    expect(onSubmit).toHaveBeenCalledWith({ method: "declared_units", job_id: "j", rotation_degrees: 90, origin: [10, 20] });
  });

  it("supports a known room dimension and a manual scale", async () => {
    const onSubmit = vi.fn();
    const user = userEvent.setup();
    renderWithProviders(<CalibrationPanel {...props({ diagnostics: svgDiag, onSubmit })} />);
    await user.click(screen.getByRole("radio", { name: /Known room width/ }));
    await user.type(screen.getByLabelText("Drawn width (source units)"), "800");
    await user.type(screen.getByLabelText("Real width (mm)"), "8000");
    await user.type(screen.getByLabelText("Drawn height (optional)"), "400");
    expect(screen.getByRole("button", { name: "Set calibration" })).toBeDisabled(); // a half-given height pair is not allowed
    await user.type(screen.getByLabelText("Real height (optional, mm)"), "4000");
    await user.click(screen.getByRole("button", { name: "Set calibration" }));
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ method: "room_dimension", src_width: 800, real_width_mm: 8000, src_height: 400, real_height_mm: 4000 }));

    await user.click(screen.getByRole("radio", { name: /Enter the scale directly/ }));
    await user.type(screen.getByLabelText("Millimetres per source unit"), "2.5");
    await user.click(screen.getByRole("button", { name: "Set calibration" }));
    expect(onSubmit).toHaveBeenLastCalledWith(expect.objectContaining({ method: "manual_scale", mm_per_unit: 2.5 }));
  });

  it("shows the current calibration with its visible error bound and confidence", () => {
    const calibrated: FloorPlan = {
      ...plan,
      current_calibration: {
        id: "c", sequence: 2, method: "two_point", source_units: "px", mm_per_unit: 10, origin_x: 0, origin_y: 0, y_axis: "down", rotation_quadrants: 0,
        error_bound_mm: 45.05, relative_error: 0.0055, confidence: "medium", reference: {}, warnings: ["The calibration baseline is under 5% of the drawing extent."],
        job_id: "j", supersedes_id: "c0", created_at: "",
      },
    };
    renderWithProviders(<CalibrationPanel {...props({ floorPlan: calibrated })} />);
    const text = screen.getByTestId("current-calibration");
    expect(text).toHaveTextContent("Current calibration #2 (two point)");
    expect(text).toHaveTextContent("error ≤ ±45.05 mm");
    expect(text).toHaveTextContent("medium confidence");
    expect(text).toHaveTextContent("baseline is under 5%");
    expect(screen.getByRole("button", { name: "Recalibrate" })).toBeInTheDocument();
  });

  it("announces actionable server errors with role=alert and locks after the first acceptance", () => {
    const { rerender } = renderWithProviders(<CalibrationPanel {...props({ error: "The width and height scales differ by 9.1%" })} />);
    expect(screen.getByRole("alert")).toHaveTextContent("differ by 9.1%");
    rerender(<CalibrationPanel {...props({ locked: true })} />);
    expect(screen.getByRole("status")).toHaveTextContent(/can no longer be changed on this revision/);
    expect(screen.queryByRole("button", { name: /calibration|Recalibrate/ })).not.toBeInTheDocument();
  });
});
