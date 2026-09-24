import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CatalogHomePage } from "@/features/catalog-designer/CatalogHomePage";
import * as api from "@/features/catalog-designer/api";
import { renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";
import { CatalogModel, Manufacturer, Page } from "@/types";

vi.mock("@/features/catalog-designer/api");

const manufacturer: Manufacturer = { id: "mfr-1", name: "Acme", status: "active", created_at: "2026-01-01T00:00:00Z" };
const model: CatalogModel = {
  id: "model-1",
  manufacturer_id: "mfr-1",
  category: "rack",
  subtype: null,
  model_name: "R4200",
  model_number: "R4200-A",
  description: null,
  tags: [],
  status: "active",
  created_at: "2026-01-01T00:00:00Z",
};

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 200, offset: 0 };
}

describe("CatalogHomePage", () => {
  beforeEach(() => {
    vi.mocked(api.listManufacturers).mockResolvedValue(mockPage([manufacturer]));
    vi.mocked(api.listCatalogModels).mockResolvedValue(mockPage([model]));
  });

  it("renders manufacturers and models", async () => {
    renderWithProviders(<CatalogHomePage />);

    expect(await screen.findByRole("link", { name: "Acme" })).toBeInTheDocument();
    expect(await screen.findByRole("link", { name: /R4200/ })).toBeInTheDocument();
  });

  it("creates a manufacturer and refreshes the list", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createManufacturer).mockResolvedValue({ ...manufacturer, id: "mfr-2", name: "Globex" });

    renderWithProviders(<CatalogHomePage />);
    await screen.findByRole("link", { name: "Acme" });

    await user.click(screen.getByRole("button", { name: "New Manufacturer" }));
    await user.type(screen.getByPlaceholderText("Manufacturer name"), "Globex");
    await user.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(api.createManufacturer).toHaveBeenCalledWith("Globex"));
  });

  it("filters models by search query", async () => {
    const user = userEvent.setup();
    renderWithProviders(<CatalogHomePage />);
    await screen.findByRole("link", { name: /R4200/ });

    await user.type(screen.getByPlaceholderText("Search models…"), "R42");

    await waitFor(() =>
      expect(api.listCatalogModels).toHaveBeenCalledWith(expect.objectContaining({ q: "R42" })),
    );
  });

  it("hides New Manufacturer and New Model controls for a non-administrator", async () => {
    renderWithProviders(<CatalogHomePage />, { user: VIEWER_TEST_USER });

    expect(await screen.findByRole("link", { name: "Acme" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New Manufacturer" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New Model" })).not.toBeInTheDocument();
  });
});
