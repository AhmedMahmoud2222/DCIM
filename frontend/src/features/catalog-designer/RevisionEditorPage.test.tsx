import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/catalog-designer/api";
import { RevisionEditorPage } from "@/features/catalog-designer/RevisionEditorPage";
import { renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";
import { CatalogModelDetail, CatalogModelRevisionDetail, PDUDraft } from "@/types";

vi.mock("@/features/catalog-designer/api");

function makeModel(overrides: Partial<CatalogModelDetail> = {}): CatalogModelDetail {
  return {
    id: "model-1",
    manufacturer_id: "mfr-1",
    category: "rack",
    subtype: null,
    model_name: "Test model",
    model_number: null,
    description: null,
    tags: [],
    status: "active",
    created_at: "2026-01-01T00:00:00Z",
    revisions: [],
    ...overrides,
  };
}

function makePduDraft(overrides: Partial<PDUDraft> = {}): PDUDraft {
  return {
    revision_id: "rev-1",
    lifecycle_status: "draft",
    pdu_input_voltage_v: 230,
    pdu_input_current_a: 16,
    pdu_phase: "single",
    mounting_orientation: "horizontal",
    pdu_rated_capacity_kw: null,
    pdu_plug_type: null,
    pdu_monitoring_protocol: null,
    pdu_monitoring_endpoint: null,
    outlets: [
      {
        id: "outlet-1",
        stable_key: "c13-1",
        outlet_number: 1,
        label: "Outlet 1",
        outlet_type: "iec_c13",
        outlet_count: 1,
        phase: "single",
        rated_voltage_v: 230,
        rated_current_a: 10,
        position: null,
        side: "front",
        sort_order: 0,
      },
    ],
    version: 1,
    ...overrides,
  };
}

/** A user with the power:manage permission the instantiate endpoint requires
 * (Administrator/DCIM Manager/Engineer on the backend — the frontend only needs the
 * code present, the API re-checks it). */
const POWER_MANAGER_TEST_USER = {
  ...VIEWER_TEST_USER,
  id: "user-power",
  role_names: ["Engineer"],
  permission_codes: ["catalog:read", "power:read", "power:manage"],
};

function makeRevision(overrides: Partial<CatalogModelRevisionDetail> = {}): CatalogModelRevisionDetail {
  return {
    id: "rev-1",
    catalog_model_id: "model-1",
    revision_number: 1,
    lifecycle_status: "draft",
    dimension_unit: "mm",
    width_value: 600,
    height_value: 2000,
    depth_value: 1000,
    rack_unit_height: 42,
    weight_unit: "kg",
    weight_value: 120,
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
    created_at: "2026-01-01T00:00:00Z",
    network_ports: [],
    power_supplies: [],
    monitoring_metrics: [],
    graphics: [],
    ...overrides,
  };
}

const routeOptions = { route: "/admin/catalog/revisions/rev-1", path: "/admin/catalog/revisions/:revisionId" };

describe("RevisionEditorPage", () => {
  beforeEach(() => {
    vi.mocked(api.getRevision).mockResolvedValue(makeRevision());
    // The page resolves the owning model's category to decide whether the PDU panels
    // apply; default the suite to a non-PDU model so those panels stay out of the way.
    vi.mocked(api.getCatalogModel).mockResolvedValue(makeModel());
  });

  it("renders draft scalar fields as editable and shows Validate/Publish controls", async () => {
    renderWithProviders(<RevisionEditorPage />, routeOptions);

    expect(await screen.findByRole("heading", { name: "Revision 1" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("600")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Validate" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete draft" })).toBeInTheDocument();
  });

  it("saves scalar field edits with the current version as If-Match", async () => {
    const user = userEvent.setup();
    vi.mocked(api.updateRevision).mockResolvedValue(makeRevision({ version: 2, width_value: 700 }));

    renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });

    const widthInput = screen.getByDisplayValue("600");
    await user.clear(widthInput);
    await user.type(widthInput, "700");
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(api.updateRevision).toHaveBeenCalledWith("rev-1", expect.objectContaining({ width_value: 700 }), 1),
    );
  });

  it("runs validation and surfaces errors", async () => {
    const user = userEvent.setup();
    vi.mocked(api.validateRevision).mockResolvedValue({
      valid: false,
      errors: [{ field: "rated_power_w", code: "required", message: "Rated power is required for this category." }],
      warnings: [],
    });

    renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });

    await user.click(screen.getByRole("button", { name: "Validate" }));

    expect(await screen.findByText(/Rated power is required/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Publish" })).toBeDisabled();
  });

  it("renders a published revision read-only with a Retire control instead of Publish", async () => {
    vi.mocked(api.getRevision).mockResolvedValue(
      makeRevision({ lifecycle_status: "published", published_at: "2026-01-05T00:00:00Z" }),
    );

    renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });

    expect(screen.getByDisplayValue("600")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Publish" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete draft" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retire this revision" })).toBeInTheDocument();
  });

  it("deletes a draft revision with the current version as If-Match", async () => {
    const user = userEvent.setup();
    vi.mocked(api.deleteDraftRevision).mockResolvedValue(undefined);

    renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });

    await user.click(screen.getByRole("button", { name: "Delete draft" }));

    await waitFor(() => expect(api.deleteDraftRevision).toHaveBeenCalledWith("rev-1", 1));
  });

  it("invalidates the parent model's cache on delete, not just the revision's own", async () => {
    const user = userEvent.setup();
    vi.mocked(api.deleteDraftRevision).mockResolvedValue(undefined);

    const { queryClient } = renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });
    // Seed the cache the way ModelDetailPage would have left it, so we can observe whether
    // deleting a draft here actually invalidates it too -- the gap this fix closes.
    queryClient.setQueryData(["catalog", "models", "model-1"], { id: "model-1" });

    await user.click(screen.getByRole("button", { name: "Delete draft" }));

    await waitFor(() => expect(api.deleteDraftRevision).toHaveBeenCalled());
    expect(queryClient.getQueryState(["catalog", "models", "model-1"])?.isInvalidated).toBe(true);
  });

  it("invalidates the parent model's cache on publish, not just the revision's own", async () => {
    const user = userEvent.setup();
    vi.mocked(api.publishRevision).mockResolvedValue(makeRevision({ lifecycle_status: "published", version: 2 }));

    const { queryClient } = renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });
    queryClient.setQueryData(["catalog", "models", "model-1"], { id: "model-1" });

    await user.click(screen.getByRole("button", { name: "Publish" }));

    await waitFor(() => expect(api.publishRevision).toHaveBeenCalled());
    await waitFor(() => expect(queryClient.getQueryState(["catalog", "models", "model-1"])?.isInvalidated).toBe(true));
  });

  it("invalidates both the revision's and the parent model's cache on a graphic upload", async () => {
    const user = userEvent.setup();
    vi.mocked(api.uploadGraphic).mockResolvedValue({
      id: "graphic-1", catalog_model_revision_id: "rev-1", revision_version: 1, side: "front",
      original_filename: "front.png", mime_type: "image/png", file_size_bytes: 10, width_px: 8, height_px: 8,
      uploaded_at: "2026-01-01T00:00:00Z", markers: [],
    });

    const { queryClient } = renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });
    queryClient.setQueryData(["catalog", "models", "model-1"], { id: "model-1" });

    const file = new File(["fake-png-bytes"], "front.png", { type: "image/png" });
    const [frontInput] = document.querySelectorAll('input[type="file"]');
    await user.upload(frontInput as HTMLInputElement, file);

    await waitFor(() => expect(api.uploadGraphic).toHaveBeenCalledWith("rev-1", "front", file, 1));
    // The revision query is actively rendered on this page, so invalidating it triggers
    // an immediate refetch (a stronger proof of invalidation than checking isInvalidated,
    // which clears the instant that refetch — using the same mocked getRevision — settles).
    await waitFor(() => expect(api.getRevision).toHaveBeenCalledTimes(2));
    // The models query has no active observer on this page, so it stays marked
    // invalidated rather than being refetched — the same signal the existing
    // delete/publish invalidation tests above already check.
    expect(queryClient.getQueryState(["catalog", "models", "model-1"])?.isInvalidated).toBe(true);
  });

  it("hides Delete/Publish controls for a non-administrator, and Retire on a published revision", async () => {
    vi.mocked(api.getRevision).mockResolvedValue(
      makeRevision({ lifecycle_status: "published", published_at: "2026-01-05T00:00:00Z" }),
    );

    renderWithProviders(<RevisionEditorPage />, { ...routeOptions, user: VIEWER_TEST_USER });
    await screen.findByRole("heading", { name: "Revision 1" });

    expect(screen.queryByRole("button", { name: "Retire this revision" })).not.toBeInTheDocument();
  });

  it("renders the PDU specification editor for a pdu-category draft revision", async () => {
    vi.mocked(api.getCatalogModel).mockResolvedValue(makeModel({ category: "pdu" }));
    vi.mocked(api.getPduDraft).mockResolvedValue(makePduDraft());

    renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });

    expect(await screen.findByRole("heading", { name: "PDU Electrical Specification" })).toBeInTheDocument();
    expect(api.getPduDraft).toHaveBeenCalledWith("rev-1");
    expect(screen.getByLabelText("Rated input voltage (V)")).toHaveValue(230);
    expect(screen.getAllByTestId("pdu-outlet-row")).toHaveLength(1);
  });

  it("never requests the PDU sub-resource for a non-pdu revision", async () => {
    renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });

    await waitFor(() => expect(api.getCatalogModel).toHaveBeenCalledWith("model-1"));
    expect(api.getPduDraft).not.toHaveBeenCalled();
    expect(screen.queryByRole("heading", { name: "PDU Electrical Specification" })).not.toBeInTheDocument();
  });

  it("does not request the PDU sub-resource once a pdu revision is published (it is immutable and 409s)", async () => {
    vi.mocked(api.getCatalogModel).mockResolvedValue(makeModel({ category: "pdu" }));
    vi.mocked(api.getRevision).mockResolvedValue(
      makeRevision({ lifecycle_status: "published", published_at: "2026-01-05T00:00:00Z" }),
    );

    renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });

    await waitFor(() => expect(api.getCatalogModel).toHaveBeenCalled());
    expect(api.getPduDraft).not.toHaveBeenCalled();
  });

  it("offers PDU instantiation on a published pdu revision to a power:manage user", async () => {
    vi.mocked(api.getCatalogModel).mockResolvedValue(makeModel({ category: "pdu" }));
    vi.mocked(api.getRevision).mockResolvedValue(
      makeRevision({ lifecycle_status: "published", published_at: "2026-01-05T00:00:00Z" }),
    );

    renderWithProviders(<RevisionEditorPage />, { ...routeOptions, user: POWER_MANAGER_TEST_USER });
    await screen.findByRole("heading", { name: "Revision 1" });

    expect(await screen.findByRole("button", { name: "Instantiate PDU" })).toBeInTheDocument();
  });

  it("hides PDU instantiation from a user without power:manage", async () => {
    vi.mocked(api.getCatalogModel).mockResolvedValue(makeModel({ category: "pdu" }));
    vi.mocked(api.getRevision).mockResolvedValue(
      makeRevision({ lifecycle_status: "published", published_at: "2026-01-05T00:00:00Z" }),
    );

    renderWithProviders(<RevisionEditorPage />, routeOptions);
    await screen.findByRole("heading", { name: "Revision 1" });

    await waitFor(() => expect(api.getCatalogModel).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Instantiate PDU" })).not.toBeInTheDocument();
  });

  it("blocks PDU instantiation on a retired revision that has no retirement override", async () => {
    vi.mocked(api.getCatalogModel).mockResolvedValue(makeModel({ category: "pdu" }));
    vi.mocked(api.getRevision).mockResolvedValue(
      makeRevision({ lifecycle_status: "retired", retired_at: "2026-02-01T00:00:00Z", retirement_reason: "EOL" }),
    );

    renderWithProviders(<RevisionEditorPage />, { ...routeOptions, user: POWER_MANAGER_TEST_USER });
    await screen.findByRole("heading", { name: "Revision 1" });

    expect(await screen.findByText(/new PDU installations from this revision are blocked/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Instantiate PDU" })).not.toBeInTheDocument();
  });

  it("allows PDU instantiation on a retired revision whose override permits installations", async () => {
    vi.mocked(api.getCatalogModel).mockResolvedValue(makeModel({ category: "pdu" }));
    vi.mocked(api.getRevision).mockResolvedValue(
      makeRevision({
        lifecycle_status: "retired",
        retired_at: "2026-02-01T00:00:00Z",
        retirement_reason: "EOL",
        allow_installation_when_retired: true,
      }),
    );

    renderWithProviders(<RevisionEditorPage />, { ...routeOptions, user: POWER_MANAGER_TEST_USER });
    await screen.findByRole("heading", { name: "Revision 1" });

    expect(await screen.findByRole("button", { name: "Instantiate PDU" })).toBeInTheDocument();
  });

  it("shows the PDU spec read-only to a viewer on a pdu draft", async () => {
    vi.mocked(api.getCatalogModel).mockResolvedValue(makeModel({ category: "pdu" }));
    vi.mocked(api.getPduDraft).mockResolvedValue(makePduDraft());

    renderWithProviders(<RevisionEditorPage />, { ...routeOptions, user: VIEWER_TEST_USER });
    await screen.findByRole("heading", { name: "Revision 1" });

    expect(await screen.findByRole("heading", { name: "PDU Electrical Specification" })).toBeInTheDocument();
    expect(screen.getByLabelText("Rated input voltage (V)")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save PDU specification" })).not.toBeInTheDocument();
  });
});
