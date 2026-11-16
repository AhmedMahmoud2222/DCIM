import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { InstantiatePage } from "@/features/equipment/InstantiatePage";
import * as catalogApi from "@/features/catalog-designer/api";
import * as equipmentApi from "@/features/equipment/api";
import { useAuthStore } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER } from "@/test/renderWithProviders";

vi.mock("@/features/catalog-designer/api");
vi.mock("@/features/equipment/api");

describe("InstantiatePage catalog navigation", () => {
  beforeEach(() => {
    useAuthStore.getState().setSession("in-memory-token", ADMINISTRATOR_TEST_USER);
    vi.mocked(catalogApi.listCatalogModels).mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
  });

  it("opens the catalog designer with client-side navigation and preserves the session", async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/equipment/instantiate"]}>
          <Routes>
            <Route path="/equipment/instantiate" element={<InstantiatePage />} />
            <Route path="/admin/catalog" element={<h1>Catalog Designer destination</h1>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );

    const link = await screen.findByRole("link", { name: "Open the catalog designer →" });
    expect(link).toHaveAttribute("href", "/admin/catalog");
    await userEvent.click(link);

    expect(await screen.findByRole("heading", { name: "Catalog Designer destination" })).toBeInTheDocument();
    expect(useAuthStore.getState().accessToken).toBe("in-memory-token");
  });
});

describe("InstantiatePage Idempotency-Key (issue #338)", () => {
  const KEY_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const model = {
    id: "model-1", manufacturer_id: "m-1", category: "equipment", subtype: null, model_name: "Switch",
    model_number: null, description: null, tags: [], status: "active", created_at: "2026-01-01T00:00:00Z",
  };
  const result = { id: "eq-1" } as never;

  beforeEach(() => {
    useAuthStore.getState().setSession("in-memory-token", ADMINISTRATOR_TEST_USER);
    vi.mocked(catalogApi.listCatalogModels).mockResolvedValue({ items: [model], total: 1, limit: 100, offset: 0 });
    vi.mocked(catalogApi.getCatalogModel).mockResolvedValue({
      ...model,
      revisions: [{ id: "rev-1", revision_number: 1, lifecycle_status: "published", published_at: "2026-01-02T00:00:00Z" }],
    } as never);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  function renderPage() {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={["/equipment/instantiate"]}>
          <Routes>
            <Route path="/equipment/instantiate" element={<InstantiatePage />} />
            <Route path="/equipment/:id" element={<h1>Equipment detail</h1>} />
          </Routes>
        </MemoryRouter>
      </QueryClientProvider>,
    );
  }

  async function fillAndSubmit(user: ReturnType<typeof userEvent.setup>, tag: string) {
    const modelSelect = await screen.findByTestId("instantiate-model-select");
    await waitFor(() => expect(modelSelect.querySelectorAll("option").length).toBeGreaterThan(1));
    await user.selectOptions(modelSelect, "model-1");
    const revisionSelect = await screen.findByTestId("instantiate-revision-select");
    await waitFor(() => expect(revisionSelect.querySelectorAll("option").length).toBeGreaterThan(1));
    await user.selectOptions(revisionSelect, "rev-1");
    await user.clear(screen.getByTestId("instantiate-asset-tag"));
    await user.type(screen.getByTestId("instantiate-asset-tag"), tag);
    await user.click(screen.getByTestId("instantiate-submit"));
  }

  it("reuses the key across a retry of the same submission", async () => {
    const user = userEvent.setup();
    vi.mocked(equipmentApi.instantiateEquipment)
      .mockRejectedValueOnce(new Error("Temporary failure."))
      .mockResolvedValueOnce(result);
    renderPage();

    await fillAndSubmit(user, "SRV-1");
    await screen.findByText("Temporary failure.");
    await user.click(screen.getByTestId("instantiate-submit"));

    await waitFor(() => expect(equipmentApi.instantiateEquipment).toHaveBeenCalledTimes(2));
    const calls = vi.mocked(equipmentApi.instantiateEquipment).mock.calls;
    expect(calls[0][1]).toMatch(KEY_PATTERN);
    expect(calls[1][1]).toBe(calls[0][1]);
  });

  it("falls back to getRandomValues when crypto.randomUUID is missing", async () => {
    const user = userEvent.setup();
    const realGetRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
    vi.stubGlobal("crypto", { getRandomValues: realGetRandomValues });
    vi.mocked(equipmentApi.instantiateEquipment).mockResolvedValue(result);
    renderPage();

    await fillAndSubmit(user, "SRV-1");
    await waitFor(() => expect(equipmentApi.instantiateEquipment).toHaveBeenCalledTimes(1));
    expect(vi.mocked(equipmentApi.instantiateEquipment).mock.calls[0][1]).toMatch(KEY_PATTERN);
  });

  it("shows the actionable message and makes zero API calls without secure randomness", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("crypto", {});
    renderPage();

    await fillAndSubmit(user, "SRV-1");
    expect(await screen.findByText(/cannot generate a secure request key/i)).toBeInTheDocument();
    expect(equipmentApi.instantiateEquipment).not.toHaveBeenCalled();
  });
});
