import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { EquipmentPage } from "./EquipmentPage";
import type { EquipmentModel, EquipmentModelRevision } from "@/types";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof api>("./api");
  return {
    ...actual,
    listEquipment: vi.fn(),
    listEquipmentModels: vi.fn(),
    listEquipmentModelRevisions: vi.fn(),
    createEquipment: vi.fn(),
  };
});
vi.mock("@/features/racks/api", () => ({ listRacks: vi.fn() }));

const mockedApi = vi.mocked(api);

const models: EquipmentModel[] = [
  { id: "model-1", manufacturer: "Acme", model_name: "SW-48", created_at: "2026-01-01T00:00:00Z" },
];
const revisions: EquipmentModelRevision[] = [
  { id: "rev-1", equipment_model_id: "model-1", height_u: 1, width_mm: 440, depth_mm: 300, weight_kg: 5, created_at: "2026-01-01T00:00:00Z" },
];

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  return render(<EquipmentPage />, { wrapper });
}

beforeEach(async () => {
  mockedApi.listEquipment.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  mockedApi.listEquipmentModels.mockResolvedValue({ items: models, total: 1, limit: 200, offset: 0 });
  mockedApi.listEquipmentModelRevisions.mockResolvedValue({ items: revisions, total: 1, limit: 200, offset: 0 });
  const racksApi = await import("@/features/racks/api");
  vi.mocked(racksApi.listRacks).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("EquipmentPage catalog model picker (Phase 10A PR-2)", () => {
  it("never renders the old inline manufacturer/model-name mint fields", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Equipment" }));

    expect(screen.queryByPlaceholderText("Manufacturer")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Model name")).not.toBeInTheDocument();
  });

  it("renders a model picker populated from the existing GET /equipment-models endpoint", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Equipment" }));

    const modelSelect = await screen.findByLabelText("Equipment model");
    expect(within(modelSelect).getByRole("option", { name: "Acme SW-48" })).toBeInTheDocument();
    expect(mockedApi.listEquipmentModels).toHaveBeenCalled();
  });

  it("keeps the revision picker disabled until a model is chosen, then populates it from GET /equipment-models/{id}/revisions", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Equipment" }));

    const revisionSelect = screen.getByLabelText("Equipment model revision");
    expect(revisionSelect).toBeDisabled();

    await user.selectOptions(await screen.findByLabelText("Equipment model"), "model-1");

    await waitFor(() => expect(revisionSelect).toBeEnabled());
    expect(mockedApi.listEquipmentModelRevisions).toHaveBeenCalledWith("model-1");
    expect(within(revisionSelect).getByRole("option", { name: "1U · 440×300mm" })).toBeInTheDocument();
  });

  it("creates equipment with the selected model_revision_id, not newly-minted identity fields", async () => {
    mockedApi.createEquipment.mockResolvedValue({
      id: "eq-1",
      asset_tag: "SRV-0142",
      hostname: "srv-0142",
      model_revision_id: "rev-1",
      lifecycle_status: "planned",
      placement: null,
    } as never);
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Equipment" }));

    await user.type(screen.getByPlaceholderText("Asset tag (e.g. SRV-0142)"), "SRV-0142");
    await user.selectOptions(await screen.findByLabelText("Equipment model"), "model-1");
    await waitFor(() => expect(screen.getByLabelText("Equipment model revision")).toBeEnabled());
    await user.selectOptions(screen.getByLabelText("Equipment model revision"), "rev-1");
    await user.click(screen.getByRole("button", { name: "Create Equipment" }));

    await waitFor(() =>
      expect(mockedApi.createEquipment).toHaveBeenCalledWith(
        expect.objectContaining({ asset_tag: "SRV-0142", model_revision_id: "rev-1" }),
        expect.any(String),
      ),
    );
  });

  it("clears the selected revision when the model changes", async () => {
    mockedApi.listEquipmentModels.mockResolvedValue({
      items: [...models, { id: "model-2", manufacturer: "Zeta", model_name: "Z-1", created_at: "2026-01-01T00:00:00Z" }],
      total: 2,
      limit: 200,
      offset: 0,
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Equipment" }));

    await user.selectOptions(await screen.findByLabelText("Equipment model"), "model-1");
    await waitFor(() => expect(screen.getByLabelText("Equipment model revision")).toBeEnabled());
    await user.selectOptions(screen.getByLabelText("Equipment model revision"), "rev-1");
    expect(screen.getByLabelText("Equipment model revision")).toHaveValue("rev-1");

    await user.selectOptions(screen.getByLabelText("Equipment model"), "model-2");
    expect(screen.getByLabelText("Equipment model revision")).toHaveValue("");
  });
});
