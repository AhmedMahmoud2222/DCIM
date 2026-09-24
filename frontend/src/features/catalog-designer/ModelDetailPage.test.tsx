import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ModelDetailPage } from "@/features/catalog-designer/ModelDetailPage";
import * as api from "@/features/catalog-designer/api";
import { renderWithProviders } from "@/test/renderWithProviders";
import { CatalogModelDetail, CatalogModelRevision } from "@/types";

vi.mock("@/features/catalog-designer/api");

const modelDetail: CatalogModelDetail = {
  id: "model-1",
  manufacturer_id: "mfr-1",
  category: "rack",
  subtype: null,
  model_name: "R4200",
  model_number: "R4200-A",
  description: "A 42U rack",
  tags: ["popular"],
  status: "active",
  created_at: "2026-01-01T00:00:00Z",
  revisions: [
    { id: "rev-1", revision_number: 1, lifecycle_status: "published", published_at: "2026-01-02T00:00:00Z", retired_at: null },
    { id: "rev-2", revision_number: 2, lifecycle_status: "draft", published_at: null, retired_at: null },
  ],
};

describe("ModelDetailPage", () => {
  beforeEach(() => {
    vi.mocked(api.getCatalogModel).mockResolvedValue(modelDetail);
  });

  it("renders model identity and revision history", async () => {
    renderWithProviders(<ModelDetailPage />, { route: "/admin/catalog/models/model-1", path: "/admin/catalog/models/:modelId" });

    expect(await screen.findByRole("heading", { name: "R4200" })).toBeInTheDocument();
    expect(screen.getByText("A 42U rack")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Rev 1" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Rev 2" })).toBeInTheDocument();
    expect(screen.getByText("published")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
  });

  it("only offers Clone for a non-draft revision", async () => {
    renderWithProviders(<ModelDetailPage />, { route: "/admin/catalog/models/model-1", path: "/admin/catalog/models/:modelId" });
    await screen.findByRole("heading", { name: "R4200" });

    expect(screen.getAllByRole("button", { name: "Clone" })).toHaveLength(1);
  });

  it("creates a new draft revision", async () => {
    const user = userEvent.setup();
    const newDraft: CatalogModelRevision = {
      id: "rev-3",
      catalog_model_id: "model-1",
      revision_number: 3,
      lifecycle_status: "draft",
      dimension_unit: null,
      width_value: null,
      height_value: null,
      depth_value: null,
      rack_unit_height: null,
      weight_unit: null,
      weight_value: null,
      mounting_orientation: null,
      supported_placement_types: null,
      airflow_direction: null,
      rated_power_w: null,
      typical_power_w: null,
      max_power_w: null,
      heat_dissipation_btu_hr: null,
      power_redundancy_mode: null,
      cloned_from_revision_id: null,
      created_by_user_id: "user-1",
      published_at: null,
      published_by_user_id: null,
      retired_at: null,
      retired_by_user_id: null,
      retirement_reason: null,
      allow_installation_when_retired: false,
      version: 1,
      created_at: "2026-01-03T00:00:00Z",
    };
    vi.mocked(api.createDraftRevision).mockResolvedValue(newDraft);

    renderWithProviders(<ModelDetailPage />, { route: "/admin/catalog/models/model-1", path: "/admin/catalog/models/:modelId" });
    await screen.findByRole("heading", { name: "R4200" });

    await user.click(screen.getByRole("button", { name: "New Draft" }));

    await waitFor(() => expect(api.createDraftRevision).toHaveBeenCalledWith("model-1"));
  });
});
