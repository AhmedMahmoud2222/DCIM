import { act, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { BulkImportPanel } from "@/features/bulk-import/BulkImportPanel";
import * as api from "@/features/bulk-import/api";
import { renderWithProviders } from "@/test/renderWithProviders";
import { BulkImportJob, BulkImportRow, Page } from "@/types";

vi.mock("@/features/bulk-import/api");

function makeJob(overrides: Partial<BulkImportJob> = {}): BulkImportJob {
  return {
    id: "job-1",
    import_type: "rack",
    mode: "create_only",
    status: "uploaded",
    original_filename: "racks.xlsx",
    file_size_bytes: 1024,
    row_count: 0,
    valid_row_count: 0,
    error_row_count: 0,
    warning_row_count: 0,
    committed_row_count: 0,
    failed_row_count: 0,
    rejection_reason: null,
    created_at: "2026-01-01T00:00:00Z",
    validated_at: null,
    committed_at: null,
    report_available: false,
    ...overrides,
  };
}

function makeRow(overrides: Partial<BulkImportRow> = {}): BulkImportRow {
  return {
    id: "row-1",
    row_number: 2,
    sheet_name: "Racks",
    status: "valid",
    action: "create",
    raw_data: {},
    errors: [],
    warnings: [],
    target_managed_asset_id: null,
    target_catalog_model_id: null,
    target_catalog_revision_id: null,
    ...overrides,
  };
}

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 25, offset: 0 };
}

async function uploadAFile(user: ReturnType<typeof userEvent.setup>) {
  const file = new File(["fake-bytes"], "racks.xlsx", {
    type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  });
  const fileInput = screen.getByLabelText("Import file");
  await user.upload(fileInput, file);
  await user.click(screen.getByRole("button", { name: "Upload" }));
}

describe("BulkImportPanel", () => {
  beforeEach(() => {
    // jsdom has no createObjectURL/revokeObjectURL implementation.
    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:mock-url"), revokeObjectURL: vi.fn() });
  });

  it("renders the idle upload form", () => {
    renderWithProviders(<BulkImportPanel resource="rack" resourceLabel="Rack" onClose={vi.fn()} />);

    expect(screen.getByRole("dialog", { name: "Bulk import Rack" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download template" })).toBeInTheDocument();
    expect(screen.getByLabelText("Import file")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Create only" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Update existing" })).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Upload" })).toBeDisabled();
  });

  it("downloads the template as a blob", async () => {
    const user = userEvent.setup();
    vi.mocked(api.downloadImportTemplate).mockResolvedValue(new Blob(["fake-xlsx"], { type: "application/octet-stream" }));

    renderWithProviders(<BulkImportPanel resource="rack" resourceLabel="Rack" onClose={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Download template" }));

    await waitFor(() => expect(api.downloadImportTemplate).toHaveBeenCalledWith("rack"));
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(URL.revokeObjectURL).toHaveBeenCalled();
  });

  it("uploads a file and moves from parsing to the validated preview state", async () => {
    const user = userEvent.setup();
    vi.mocked(api.uploadImportJob).mockResolvedValue(makeJob({ status: "uploaded" }));
    vi.mocked(api.getImportJob).mockResolvedValue(makeJob({ status: "parsing" }));
    vi.mocked(api.listImportJobRows).mockResolvedValue(mockPage([]));

    const { queryClient } = renderWithProviders(<BulkImportPanel resource="rack" resourceLabel="Rack" onClose={vi.fn()} />);
    await uploadAFile(user);

    await waitFor(() => expect(api.uploadImportJob).toHaveBeenCalledWith("rack", expect.any(File), "create_only"));
    await screen.findByText("Processing… this view refreshes automatically.");
    expect(screen.queryByText(/Rows/)).not.toBeInTheDocument();

    // The poll (refetchInterval) landing a 'validated' job is simulated directly on the
    // query cache, rather than waiting out the real 1.5s interval in this test.
    act(() => {
      queryClient.setQueryData(
        ["bulk-import-job", "job-1"],
        makeJob({ status: "validated", row_count: 2, valid_row_count: 2 }),
      );
    });

    expect(await screen.findByTestId("bulk-import-job-status")).toHaveTextContent("validated");
    expect(screen.getByText("Rows")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Commit import" })).toBeEnabled();
  });

  it("shows the rejection reason for a failed_parse job with no preview table", async () => {
    const user = userEvent.setup();
    vi.mocked(api.uploadImportJob).mockResolvedValue(makeJob({ status: "uploaded" }));
    vi.mocked(api.getImportJob).mockResolvedValue(
      makeJob({ status: "failed_parse", rejection_reason: "The uploaded file is missing required column(s)." }),
    );

    renderWithProviders(<BulkImportPanel resource="rack" resourceLabel="Rack" onClose={vi.fn()} />);
    await uploadAFile(user);

    expect(await screen.findByRole("alert")).toHaveTextContent("The uploaded file is missing required column(s).");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Commit import" })).not.toBeInTheDocument();
  });

  it("renders row errors and warnings in the preview table", async () => {
    const user = userEvent.setup();
    vi.mocked(api.uploadImportJob).mockResolvedValue(makeJob({ status: "uploaded" }));
    vi.mocked(api.getImportJob).mockResolvedValue(makeJob({ status: "validated", row_count: 2, valid_row_count: 1, error_row_count: 1 }));
    vi.mocked(api.listImportJobRows).mockResolvedValue(
      mockPage([
        makeRow({ id: "row-1", row_number: 2, status: "valid" }),
        makeRow({
          id: "row-2",
          row_number: 3,
          status: "invalid",
          action: null,
          errors: [{ field: "asset_tag", message: "asset_tag is required." }],
          warnings: [{ field: "notes", message: "notes will be truncated." }],
        }),
      ]),
    );

    renderWithProviders(<BulkImportPanel resource="rack" resourceLabel="Rack" onClose={vi.fn()} />);
    await uploadAFile(user);

    await screen.findByRole("table");
    expect(screen.getByText("asset_tag: asset_tag is required.")).toBeInTheDocument();
    expect(screen.getByText("notes: notes will be truncated.")).toBeInTheDocument();
  });

  it("disables the commit button until the job is validated", async () => {
    const user = userEvent.setup();
    vi.mocked(api.uploadImportJob).mockResolvedValue(makeJob({ status: "uploaded" }));
    vi.mocked(api.getImportJob).mockResolvedValue(makeJob({ status: "committing", row_count: 2, valid_row_count: 2 }));
    vi.mocked(api.listImportJobRows).mockResolvedValue(mockPage([makeRow()]));

    renderWithProviders(<BulkImportPanel resource="rack" resourceLabel="Rack" onClose={vi.fn()} />);
    await uploadAFile(user);

    await screen.findByRole("table");
    expect(screen.getByRole("button", { name: "Commit import" })).toBeDisabled();
  });

  it("shows a download-report button once committed, and downloads it as a blob", async () => {
    const user = userEvent.setup();
    vi.mocked(api.uploadImportJob).mockResolvedValue(makeJob({ status: "uploaded" }));
    vi.mocked(api.getImportJob).mockResolvedValue(
      makeJob({ status: "committed", row_count: 1, valid_row_count: 1, committed_row_count: 1, report_available: true }),
    );
    vi.mocked(api.listImportJobRows).mockResolvedValue(mockPage([makeRow({ status: "committed" })]));
    vi.mocked(api.downloadImportReport).mockResolvedValue(new Blob(["fake-report"], { type: "application/octet-stream" }));

    const onCommitted = vi.fn();
    renderWithProviders(<BulkImportPanel resource="rack" resourceLabel="Rack" onClose={vi.fn()} onCommitted={onCommitted} />);
    await uploadAFile(user);

    const downloadButton = await screen.findByRole("button", { name: "Download results report" });
    await waitFor(() => expect(onCommitted).toHaveBeenCalled());

    await user.click(downloadButton);
    await waitFor(() => expect(api.downloadImportReport).toHaveBeenCalledWith("job-1"));
    expect(URL.createObjectURL).toHaveBeenCalled();
  });

  it("calls commitImportJob when Commit import is clicked", async () => {
    const user = userEvent.setup();
    vi.mocked(api.uploadImportJob).mockResolvedValue(makeJob({ status: "uploaded" }));
    vi.mocked(api.getImportJob).mockResolvedValue(makeJob({ status: "validated", row_count: 1, valid_row_count: 1 }));
    vi.mocked(api.listImportJobRows).mockResolvedValue(mockPage([makeRow()]));
    vi.mocked(api.commitImportJob).mockResolvedValue(undefined);

    renderWithProviders(<BulkImportPanel resource="rack" resourceLabel="Rack" onClose={vi.fn()} />);
    await uploadAFile(user);

    const commitButton = await screen.findByRole("button", { name: "Commit import" });
    await user.click(commitButton);

    await waitFor(() => expect(api.commitImportJob).toHaveBeenCalledWith("job-1"));
  });
});
