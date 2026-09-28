import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CatalogHomePage } from "@/features/catalog-designer/CatalogHomePage";
import * as api from "@/features/catalog-designer/api";
import * as bulkImportApi from "@/features/bulk-import/api";
import { renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";
import { BulkImportJob, CatalogModel, Manufacturer, Page } from "@/types";

vi.mock("@/features/catalog-designer/api");
vi.mock("@/features/bulk-import/api");

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
    expect(screen.getByRole("textbox", { name: "Search manufacturers" })).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Search models" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Filter by category" })).toBeInTheDocument();
    expect(screen.getByRole("table", { name: "Catalog models" })).toBeInTheDocument();
  });

  it("creates a manufacturer and refreshes the list", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createManufacturer).mockResolvedValue({ ...manufacturer, id: "mfr-2", name: "Globex" });

    renderWithProviders(<CatalogHomePage />);
    await screen.findByRole("link", { name: "Acme" });

    await user.click(screen.getByRole("button", { name: "New Manufacturer" }));
    await user.type(screen.getByRole("textbox", { name: "Manufacturer name" }), "Globex");
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

  it("shows the Bulk Import button for a catalog administrator", async () => {
    renderWithProviders(<CatalogHomePage />);

    expect(await screen.findByRole("button", { name: "Bulk Import" })).toBeInTheDocument();
  });

  it("hides the Bulk Import button for a non-administrator", async () => {
    renderWithProviders(<CatalogHomePage />, { user: VIEWER_TEST_USER });

    await screen.findByRole("link", { name: "Acme" });
    expect(screen.queryByRole("button", { name: "Bulk Import" })).not.toBeInTheDocument();
  });

  it("opens the bulk-import panel and refreshes catalog lists once committed", async () => {
    const user = userEvent.setup();
    const committedJob: BulkImportJob = {
      id: "job-1",
      import_type: "catalog",
      mode: "create_only",
      status: "committed",
      original_filename: "catalog.xlsx",
      file_size_bytes: 10,
      row_count: 1,
      valid_row_count: 1,
      error_row_count: 0,
      warning_row_count: 0,
      committed_row_count: 1,
      failed_row_count: 0,
      rejection_reason: null,
      created_at: "2026-01-01T00:00:00Z",
      validated_at: "2026-01-01T00:00:01Z",
      committed_at: "2026-01-01T00:00:02Z",
      report_available: true,
    };
    vi.mocked(bulkImportApi.uploadImportJob).mockResolvedValue(committedJob);
    vi.mocked(bulkImportApi.getImportJob).mockResolvedValue(committedJob);
    vi.mocked(bulkImportApi.listImportJobRows).mockResolvedValue(mockPage([]));

    renderWithProviders(<CatalogHomePage />);
    await screen.findByRole("link", { name: "Acme" });

    await user.click(screen.getByRole("button", { name: "Bulk Import" }));
    expect(screen.getByRole("dialog", { name: "Bulk import Catalog Model" })).toBeInTheDocument();

    const file = new File(["fake"], "catalog.xlsx", {
      type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    });
    await user.upload(screen.getByLabelText("Import file"), file);
    await user.click(screen.getByRole("button", { name: "Upload" }));

    await waitFor(() => expect(bulkImportApi.uploadImportJob).toHaveBeenCalledWith("catalog", expect.any(File), "create_only"));
    await waitFor(() => expect(api.listCatalogModels).toHaveBeenCalledTimes(2));
  });
});
