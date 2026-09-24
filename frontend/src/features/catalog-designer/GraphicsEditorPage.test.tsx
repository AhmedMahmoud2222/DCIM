import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/catalog-designer/api";
import { GraphicsEditorPage } from "@/features/catalog-designer/GraphicsEditorPage";
import { renderWithProviders } from "@/test/renderWithProviders";
import { CatalogGraphic, CatalogGraphicMarker, CatalogModelRevisionDetail } from "@/types";

vi.mock("@/features/catalog-designer/api");

// jsdom has no createObjectURL/revokeObjectURL implementation.
beforeEach(() => {
  vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:mock-url"), revokeObjectURL: vi.fn() });
  vi.mocked(api.fetchGraphicImage).mockResolvedValue(new Blob(["fake-bytes"], { type: "image/png" }));
});

function makeMarker(overrides: Partial<CatalogGraphicMarker> = {}): CatalogGraphicMarker {
  return {
    id: "marker-1", catalog_graphic_id: "graphic-1", revision_version: 1, marker_type: "network_port",
    network_port_template_id: "port-1", power_supply_template_id: null, label: null, marker_x: 0.5, marker_y: 0.5,
    sort_order: 0, ...overrides,
  };
}

function makeGraphic(overrides: Partial<CatalogGraphic> = {}): CatalogGraphic {
  return {
    id: "graphic-1", catalog_model_revision_id: "rev-1", revision_version: 1, side: "front",
    original_filename: "front.png", mime_type: "image/png", file_size_bytes: 100, width_px: 800, height_px: 600,
    uploaded_at: "2026-01-01T00:00:00Z", markers: [], ...overrides,
  };
}

function makeRevision(overrides: Partial<CatalogModelRevisionDetail> = {}): CatalogModelRevisionDetail {
  return {
    id: "rev-1", catalog_model_id: "model-1", revision_number: 1, lifecycle_status: "draft", dimension_unit: "mm",
    width_value: 600, height_value: 2000, depth_value: 1000, rack_unit_height: 42, weight_unit: "kg", weight_value: 120,
    mounting_orientation: null, supported_placement_types: null, airflow_direction: null, rated_power_w: null,
    typical_power_w: null, max_power_w: null, heat_dissipation_btu_hr: null, power_redundancy_mode: null,
    cloned_from_revision_id: null, created_by_user_id: "user-1", published_at: null, published_by_user_id: null,
    retired_at: null, retired_by_user_id: null, retirement_reason: null, allow_installation_when_retired: false,
    version: 1, created_at: "2026-01-01T00:00:00Z", network_ports: [], power_supplies: [], monitoring_metrics: [],
    graphics: [], ...overrides,
  };
}

describe("GraphicsEditorPage", () => {
  it("shows an upload button and placeholder text when no graphic exists yet, for an editor", () => {
    renderWithProviders(<GraphicsEditorPage revision={makeRevision()} readOnly={false} onChanged={vi.fn()} />);

    expect(screen.getAllByRole("button", { name: "Upload image" })).toHaveLength(2); // front + rear
    expect(screen.getByText("No front image uploaded yet.")).toBeInTheDocument();
    expect(screen.getByText("No rear image uploaded yet.")).toBeInTheDocument();
  });

  it("hides the upload button when readOnly (non-administrator or non-draft)", () => {
    renderWithProviders(<GraphicsEditorPage revision={makeRevision()} readOnly={true} onChanged={vi.fn()} />);

    expect(screen.queryByRole("button", { name: "Upload image" })).not.toBeInTheDocument();
  });

  it("uploads a selected file with the revision's current version as If-Match", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    vi.mocked(api.uploadGraphic).mockResolvedValue(makeGraphic());
    const revision = makeRevision({ version: 3 });

    renderWithProviders(<GraphicsEditorPage revision={revision} readOnly={false} onChanged={onChanged} />);

    const file = new File(["fake-png-bytes"], "front.png", { type: "image/png" });
    const [frontInput] = document.querySelectorAll('input[type="file"]');
    await user.upload(frontInput as HTMLInputElement, file);

    await waitFor(() => expect(api.uploadGraphic).toHaveBeenCalledWith("rev-1", "front", file, 3));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("renders an existing graphic's markers with an accessible label", async () => {
    const graphic = makeGraphic({ markers: [makeMarker({ label: "Uplink" })] });
    renderWithProviders(
      <GraphicsEditorPage revision={makeRevision({ graphics: [graphic] })} readOnly={false} onChanged={vi.fn()} />,
    );

    expect(await screen.findByRole("button", { name: /Network port marker/ })).toBeInTheDocument();
  });

  it("nudges a focused marker with the arrow keys and commits the new position", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    const graphic = makeGraphic({ markers: [makeMarker({ marker_x: 0.5, marker_y: 0.5 })] });
    vi.mocked(api.updateGraphicMarker).mockResolvedValue(makeMarker({ marker_x: 0.51, marker_y: 0.5 }));

    renderWithProviders(
      <GraphicsEditorPage revision={makeRevision({ version: 2, graphics: [graphic] })} readOnly={false} onChanged={onChanged} />,
    );

    const marker = await screen.findByRole("button", { name: /Network port marker/ });
    marker.focus();
    await user.keyboard("{ArrowRight}");

    await waitFor(() =>
      expect(api.updateGraphicMarker).toHaveBeenCalledWith(
        "rev-1", "graphic-1", "marker-1", { marker_x: 0.51, marker_y: 0.5 }, 2,
      ),
    );
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("deletes a focused marker on Delete after confirmation", async () => {
    const user = userEvent.setup();
    const onChanged = vi.fn();
    const graphic = makeGraphic({ markers: [makeMarker()] });
    vi.mocked(api.deleteGraphicMarker).mockResolvedValue(undefined);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithProviders(
      <GraphicsEditorPage revision={makeRevision({ version: 1, graphics: [graphic] })} readOnly={false} onChanged={onChanged} />,
    );

    const marker = await screen.findByRole("button", { name: /Network port marker/ });
    marker.focus();
    await user.keyboard("{Delete}");

    await waitFor(() => expect(api.deleteGraphicMarker).toHaveBeenCalledWith("rev-1", "graphic-1", "marker-1", 1));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("does not allow keyboard focus on markers when readOnly", async () => {
    const graphic = makeGraphic({ markers: [makeMarker()] });
    renderWithProviders(
      <GraphicsEditorPage revision={makeRevision({ graphics: [graphic] })} readOnly={true} onChanged={vi.fn()} />,
    );

    const marker = await screen.findByRole("button", { name: /Network port marker/ });
    expect(marker).toHaveAttribute("tabindex", "-1");
  });

  it("hides marker-placement type buttons when readOnly", () => {
    const graphic = makeGraphic();
    renderWithProviders(
      <GraphicsEditorPage revision={makeRevision({ graphics: [graphic] })} readOnly={true} onChanged={vi.fn()} />,
    );

    expect(screen.queryByRole("button", { name: /Network port/ })).not.toBeInTheDocument();
  });
});
