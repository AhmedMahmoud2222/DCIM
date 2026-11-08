import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { InstantiatePage } from "@/features/equipment/InstantiatePage";
import * as catalogApi from "@/features/catalog-designer/api";
import { useAuthStore } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER } from "@/test/renderWithProviders";

vi.mock("@/features/catalog-designer/api");

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
