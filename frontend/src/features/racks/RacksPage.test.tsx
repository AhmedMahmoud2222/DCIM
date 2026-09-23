import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "./api";
import { RacksPage } from "./RacksPage";
import type { RackModel, RackModelRevision } from "@/types";

vi.mock("./api", async () => {
  const actual = await vi.importActual<typeof api>("./api");
  return {
    ...actual,
    listRacks: vi.fn(),
    listRooms: vi.fn(),
    listRackModels: vi.fn(),
    listRackModelRevisions: vi.fn(),
    createRack: vi.fn(),
  };
});

const mockedApi = vi.mocked(api);

const models: RackModel[] = [
  { id: "model-1", manufacturer: "Acme", model_name: "AC-42", created_at: "2026-01-01T00:00:00Z" },
];
const revisions: RackModelRevision[] = [
  { id: "rev-1", rack_model_id: "model-1", height_u: 42, width_mm: 600, depth_mm: 1000, weight_capacity_kg: null, created_at: "2026-01-01T00:00:00Z" },
];

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
  return render(<RacksPage />, { wrapper });
}

beforeEach(() => {
  mockedApi.listRacks.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  mockedApi.listRooms.mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
  mockedApi.listRackModels.mockResolvedValue({ items: models, total: 1, limit: 200, offset: 0 });
  mockedApi.listRackModelRevisions.mockResolvedValue({ items: revisions, total: 1, limit: 200, offset: 0 });
});

afterEach(() => {
  vi.clearAllMocks();
});

describe("RacksPage catalog model picker (Phase 10A PR-2)", () => {
  it("never renders the old inline manufacturer/model-name/dimension mint fields", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Rack" }));

    expect(screen.queryByPlaceholderText("Manufacturer")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Model name")).not.toBeInTheDocument();
    expect(screen.queryByText("Height (U)")).not.toBeInTheDocument();
    expect(screen.queryByText("Width (mm)")).not.toBeInTheDocument();
    expect(screen.queryByText("Depth (mm)")).not.toBeInTheDocument();
  });

  it("renders a model picker populated from the existing GET /rack-models endpoint", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Rack" }));

    const modelSelect = await screen.findByLabelText("Rack model");
    expect(within(modelSelect).getByRole("option", { name: "Acme AC-42" })).toBeInTheDocument();
    expect(mockedApi.listRackModels).toHaveBeenCalled();
  });

  it("keeps the revision picker disabled until a model is chosen, then populates it from GET /rack-models/{id}/revisions", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Rack" }));

    const revisionSelect = screen.getByLabelText("Rack model revision");
    expect(revisionSelect).toBeDisabled();

    await user.selectOptions(await screen.findByLabelText("Rack model"), "model-1");

    await waitFor(() => expect(revisionSelect).toBeEnabled());
    expect(mockedApi.listRackModelRevisions).toHaveBeenCalledWith("model-1");
    expect(within(revisionSelect).getByRole("option", { name: "42U · 600×1000mm" })).toBeInTheDocument();
  });

  it("creates a rack with the selected model_revision_id, not newly-minted identity fields", async () => {
    mockedApi.createRack.mockResolvedValue({
      id: "rack-1",
      asset_tag: "RACK-A01",
      name: "Test Rack",
      model_revision_id: "rev-1",
      lifecycle_status: "planned",
      version: 1,
      placement: null,
    } as never);
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Rack" }));

    await user.type(screen.getByPlaceholderText("Asset tag (e.g. RACK-A01)"), "RACK-A01");
    await user.type(screen.getByPlaceholderText("Display name"), "Test Rack");
    await user.selectOptions(await screen.findByLabelText("Rack model"), "model-1");
    await waitFor(() => expect(screen.getByLabelText("Rack model revision")).toBeEnabled());
    await user.selectOptions(screen.getByLabelText("Rack model revision"), "rev-1");
    await user.click(screen.getByRole("button", { name: "Create Rack" }));

    await waitFor(() =>
      expect(mockedApi.createRack).toHaveBeenCalledWith(
        expect.objectContaining({ asset_tag: "RACK-A01", name: "Test Rack", model_revision_id: "rev-1" }),
        expect.any(String),
      ),
    );
  });

  it("clears the selected revision when the model changes, so a stale revision from the previous model can never be submitted", async () => {
    mockedApi.listRackModels.mockResolvedValue({
      items: [...models, { id: "model-2", manufacturer: "Zeta", model_name: "Z-1", created_at: "2026-01-01T00:00:00Z" }],
      total: 2,
      limit: 200,
      offset: 0,
    });
    const user = userEvent.setup();
    renderPage();
    await user.click(screen.getByRole("button", { name: "New Rack" }));

    await user.selectOptions(await screen.findByLabelText("Rack model"), "model-1");
    await waitFor(() => expect(screen.getByLabelText("Rack model revision")).toBeEnabled());
    await user.selectOptions(screen.getByLabelText("Rack model revision"), "rev-1");
    expect(screen.getByLabelText("Rack model revision")).toHaveValue("rev-1");

    await user.selectOptions(screen.getByLabelText("Rack model"), "model-2");
    expect(screen.getByLabelText("Rack model revision")).toHaveValue("");
  });
});
