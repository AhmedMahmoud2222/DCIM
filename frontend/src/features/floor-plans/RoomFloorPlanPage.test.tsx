import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RoomFloorPlanPage } from "@/features/floor-plans/RoomFloorPlanPage";
import * as api from "@/features/floor-plans/api";
import * as racksApi from "@/features/racks/api";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { Calibration, FloorPlan, ImportCandidate, ImportDiagnostics, ImportJob, Page, Room, RoomSpatialView, SourceGeometry } from "@/types";

vi.mock("@/features/floor-plans/api");
vi.mock("@/features/racks/api");

const page = <T,>(items: T[]): Page<T> => ({ items, total: items.length, limit: 200, offset: 0 });
const room: Room = { id: "room-1", floor_id: "f", code: "A", name: "Hall A", room_type: "hall", version: 1 };
const calibration: Calibration = {
  id: "cal-1", sequence: 1, method: "declared_units", source_units: "mm", mm_per_unit: 1, origin_x: 0, origin_y: 4000, y_axis: "up", rotation_quadrants: 0,
  error_bound_mm: 0.5, relative_error: 0, confidence: "high", reference: {}, warnings: [], job_id: "job-1", supersedes_id: null, created_at: "",
};
const plan = (over: Partial<FloorPlan> = {}): FloorPlan => ({
  id: "fp-1", room_id: "room-1", revision_number: 1, status: "draft", source_file_name: null, source_format: null, calibration_scale_mm_per_px: null,
  room_width_mm: null, room_height_mm: null, version: 2, created_at: "", current_calibration: null, ...over,
});
const job = (over: Partial<ImportJob> = {}): ImportJob => ({
  id: "job-1", floor_plan_id: "fp-1", status: "parsed", original_filename: "hall.dxf", file_size_bytes: 1000, rejection_reason: null, created_at: "", detected_format: "dxf", ...over,
});
const diag: ImportDiagnostics = {
  job_id: "job-1", source_format: "dxf", parser_name: "dxf_ascii_subset", parser_version: "1", objects_discovered: 9, objects_classified: 5, racks_detected: 4,
  unsupported_object_count: 1, warnings: ["1 SPLINE entity ignored"], errors: [], ambiguous_count: 1, confirmed_count: 4, duration_ms: 12, source_units: "mm",
  units_trusted: true, y_axis: "up", candidate_count: 5, sir_sha256: "a".repeat(64),
};
const source: SourceGeometry = { job_id: "job-1", source_format: "dxf", source_units: "mm", units_trusted: true, y_axis: "up", layers: [], sir_sha256: "a".repeat(64), bbox: { min_x: 0, min_y: 0, max_x: 6000, max_y: 4000 }, entities: [], total_entities: 0, truncated: false };
const cand: ImportCandidate = {
  id: "c1", job_id: "job-1", version: 1, status: "pending", resulting_spatial_object_id: null, source_ref: "dxf:1", correction: null, can_undo: false,
  raw_geometry: { shape_type: "rect", cx: 1300, cy: 1500, width: 600, height: 1000, rotation_deg: 0 },
  effective_geometry: { shape_type: "rect", cx: 1300, cy: 1500, width: 600, height: 1000, rotation_deg: 0 },
  canonical: null, suggested_object_type: "rack", effective_object_type: "rack", suggested_label: "RACK-01", effective_label: "RACK-01", confidence: 0.9,
  evidence: [], match_status: "unmatched", match_score: null, matched_asset_id: null, matched_asset_name: null, duplicate_of_spatial_object_id: null, reconciled_calibration_id: null,
};
const view = (over: Partial<RoomSpatialView> = {}): RoomSpatialView => ({
  room_id: "room-1", room_name: "Hall A", active_floor_plan_id: null, active_floor_plan_revision: null, room_width_mm: null, room_height_mm: null, generated_at: "",
  racks: [{ id: "rack-a", asset_tag: "RK-A", name: "Rack A", x_mm: 1000, y_mm: 2000, rotation_deg: 0, spatial_object_id: null, height_u: 42, width_mm: 600, depth_mm: 1000, placement_version: 1 }],
  equipment: [], objects: [], rack_equipment: [], layout_state: "incomplete", incomplete_reasons: ["no_active_floor_plan"], ...over,
});

function mountPage() {
  return renderWithProviders(<RoomFloorPlanPage />, { route: "/floor-plans/room/room-1", path: "/floor-plans/room/:roomId" });
}

describe("RoomFloorPlanPage workflow", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(racksApi.listRooms).mockResolvedValue(page([room]));
    vi.mocked(api.getRoomSpatialView).mockResolvedValue(view());
    vi.mocked(api.listFloorPlans).mockResolvedValue(page([plan()]));
    vi.mocked(api.listImportJobs).mockResolvedValue(page([job()]));
    vi.mocked(api.getImportDiagnostics).mockResolvedValue(diag);
    vi.mocked(api.getSourceGeometry).mockResolvedValue(source);
    vi.mocked(api.listImportCandidates).mockResolvedValue(page([cand]));
    vi.mocked(api.listFloorPlanObjects).mockResolvedValue([]);
  });

  it("walks the operator through upload, review, calibrate and accept with the workflow step markers", async () => {
    mountPage();
    const steps = await screen.findByTestId("workflow-steps");
    await screen.findByTestId("review-workspace");
    expect(within(steps).getByText(/1\. Upload/).closest("li")).toHaveAttribute("data-done", "true");
    await waitFor(() => expect(within(steps).getByText(/2\. Review/).closest("li")).toHaveAttribute("data-done", "true"));
    expect(within(steps).getByText(/3\. Calibrate/).closest("li")).toHaveAttribute("data-done", "false");
    expect(within(steps).getByText(/3\. Calibrate/).closest("li")).toHaveAttribute("aria-current", "step");
    expect(screen.getByTestId("racks-detected")).toHaveTextContent("4");
    expect(screen.getByTestId("diagnostics-panel")).toHaveTextContent("dxf_ascii_subset v1");
    expect(screen.getByTestId("diagnostics-panel")).toHaveTextContent("1 SPLINE entity ignored");
    expect(screen.getByTestId("diagnostics-panel")).toHaveTextContent("aaaaaaaaaaaa…");
  });

  it("calibrates with the declared units, with the floor plan's version as the concurrency token", async () => {
    vi.mocked(api.setCalibration).mockResolvedValue({ floor_plan: plan({ current_calibration: calibration, version: 3 }), calibration, reconciled: { matched: 1 } });
    const user = userEvent.setup();
    mountPage();
    await screen.findByTestId("calibration-panel");
    await user.click(await screen.findByRole("button", { name: "Set calibration" }));
    await waitFor(() => expect(api.setCalibration).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.setCalibration).mock.calls[0][0]).toMatchObject({ id: "fp-1", version: 2 });
    expect(vi.mocked(api.setCalibration).mock.calls[0][1]).toEqual({ method: "declared_units", job_id: "job-1", rotation_degrees: 0 });
    expect(await screen.findByTestId("notice")).toHaveTextContent("Calibration recorded (high confidence, error ≤ ±0.5 mm)");
  });

  it("locks the calibration panel once a shape of the selected revision has been accepted", async () => {
    vi.mocked(api.listFloorPlans).mockResolvedValue(page([plan({ current_calibration: calibration })]));
    vi.mocked(api.listFloorPlanObjects).mockResolvedValue([
      { id: "o1", object_type: "rack", geometry_type: "rect", x_mm: 1, y_mm: 2, width_mm: 3, height_mm: 4, rotation_deg: 0, label: null, source: "imported" },
    ]);
    mountPage();
    expect(await screen.findByText(/can no longer be changed on this revision/)).toBeInTheDocument();
  });

  it("accepts a calibrated candidate with the candidate version as the concurrency token and refreshes", async () => {
    vi.mocked(api.listFloorPlans).mockResolvedValue(page([plan({ current_calibration: calibration })]));
    vi.mocked(api.listImportCandidates).mockResolvedValue(page([{ ...cand, canonical: { geometry_type: "rect", x_mm: 1000, y_mm: 2000, width_mm: 600, height_mm: 1000, rotation_deg: 0, geometry_data: null } }]));
    vi.mocked(api.acceptCandidate).mockResolvedValue({ ...cand, status: "accepted" });
    const user = userEvent.setup();
    mountPage();
    await user.click(await screen.findByRole("button", { name: "Accept as authoritative geometry" }));
    await waitFor(() => expect(api.acceptCandidate).toHaveBeenCalled());
    expect(vi.mocked(api.acceptCandidate).mock.calls[0][0]).toBe("job-1");
    expect(vi.mocked(api.acceptCandidate).mock.calls[0][1]).toMatchObject({ id: "c1", version: 1 });
    expect(vi.mocked(api.acceptCandidate).mock.calls[0][2]).toEqual({ object_type: "rack", label: "RACK-01" });
    expect(await screen.findByTestId("notice")).toHaveTextContent("Accepted");
  });

  it("surfaces a stale-version or boundary rejection from the server as an alert and reloads the candidates", async () => {
    vi.mocked(api.listFloorPlans).mockResolvedValue(page([plan({ current_calibration: calibration })]));
    vi.mocked(api.acceptCandidate).mockRejectedValue(new ApiError(409, "Conflict", "Candidate was modified by another request (expected version 1, current version 2).", null));
    const user = userEvent.setup();
    mountPage();
    await user.click(await screen.findByRole("button", { name: "Accept as authoritative geometry" }));
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => /modified by another request/.test(a.textContent ?? ""))).toBe(true);
    await waitFor(() => expect(vi.mocked(api.listImportCandidates).mock.calls.length).toBeGreaterThan(1));
  });

  it("shows a failed import's reason as an alert and offers no review workspace", async () => {
    vi.mocked(api.listImportJobs).mockResolvedValue(page([job({ status: "failed", rejection_reason: "The file was rejected (vsdx path traversal). package entry name is not a safe relative path" })]));
    vi.mocked(api.listImportCandidates).mockResolvedValue(page([]));
    mountPage();
    expect(await screen.findByTestId("job-failure")).toHaveTextContent("vsdx path traversal");
    expect(screen.getByTestId("job-failure")).toHaveAttribute("role", "alert");
    expect(screen.queryByTestId("review-workspace")).not.toBeInTheDocument();
  });

  it("uploads any of the supported formats and reports a duplicate upload", async () => {
    vi.mocked(api.uploadFloorPlanFile).mockResolvedValue(job({ deduplicated: true }));
    const user = userEvent.setup();
    mountPage();
    const input = await screen.findByLabelText("Upload floor plan file");
    expect(input).toHaveAttribute("accept", expect.stringContaining(".dxf"));
    expect(input).toHaveAttribute("accept", expect.stringContaining(".vsdx"));
    await user.upload(input, new File(["0\nEOF\n"], "hall.dxf", { type: "application/octet-stream" }));
    await waitFor(() => expect(api.uploadFloorPlanFile).toHaveBeenCalledWith("fp-1", expect.objectContaining({ name: "hall.dxf" })));
    expect(await screen.findByTestId("notice")).toHaveTextContent("already uploaded");
  });

  it("shows an upload rejection (declared/detected mismatch) to the operator", async () => {
    vi.mocked(api.uploadFloorPlanFile).mockRejectedValue(new ApiError(422, "File Type Mismatch", "File is declared as dxf but its content is png.", null));
    const user = userEvent.setup();
    mountPage();
    await user.upload(await screen.findByLabelText("Upload floor plan file"), new File(["x"], "plan.dxf"));
    expect(await screen.findByTestId("action-error")).toHaveTextContent("declared as dxf but its content is png");
    expect(screen.getByTestId("action-error")).toHaveAttribute("role", "alert");
  });

  it("sets a rectangular room boundary from millimetre inputs", async () => {
    vi.mocked(api.setRoomBoundary).mockResolvedValue({ spatial_object_id: "o", floor_plan: plan(), racks_outside_boundary: ["Rack Z"] });
    const user = userEvent.setup();
    mountPage();
    await user.type(await screen.findByLabelText("Boundary width (mm)"), "6000");
    await user.type(screen.getByLabelText("Boundary depth (mm)"), "4000");
    await user.click(screen.getByRole("button", { name: "Set boundary" }));
    await waitFor(() => expect(api.setRoomBoundary).toHaveBeenCalledWith(expect.objectContaining({ id: "fp-1" }), { shape: "rect", x_mm: 0, y_mm: 0, width_mm: 6000, height_mm: 4000 }));
    expect(await screen.findByTestId("notice")).toHaveTextContent("Racks outside it (not moved): Rack Z");
  });

  it("switches the 2D plan to an operational overlay and links to the 3D twin for this room", async () => {
    vi.mocked(api.getRoomOverlays).mockResolvedValue({
      room_id: "room-1", generated_at: "",
      overlays: { power: { source: "power_topology", truncated: false, items: [{ asset_id: "rack-a", asset_kind: "rack", state: "critical", reason: "No live power path" }] } },
    });
    const user = userEvent.setup();
    mountPage();
    await screen.findByTestId("verify-2d");
    expect(screen.getByRole("link", { name: "Open 3D twin" })).toHaveAttribute("href", "/floor-plans/3d-layout?room=room-1");
    await user.selectOptions(screen.getByLabelText("Operational overlay"), "power");
    await waitFor(() => expect(api.getRoomOverlays).toHaveBeenCalledWith("room-1", ["power"]));
    expect(await screen.findByRole("link", { name: /Rack Rack A.*power: Critical/ })).toBeInTheDocument();
    expect(screen.getByTestId("overlay-source")).toHaveTextContent("Power topology and protection-device state");
  });
});
