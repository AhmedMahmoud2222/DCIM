import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DatasheetPanel } from "@/features/catalog-designer/DatasheetPanel";
import * as api from "@/features/catalog-designer/extractionApi";
import { ApiError } from "@/lib/apiClient";
import { CatalogModelRevisionDetail } from "@/types";

vi.mock("@/features/auth/useAuthorization", () => ({ useHasPermission: () => true }));
vi.mock("@/features/catalog-designer/extractionApi", () => ({
  listDatasheets: vi.fn(), listModelDatasheets: vi.fn(), listExtractionJobs: vi.fn(), listCandidates: vi.fn(),
  listApplications: vi.fn(), reviewCandidate: vi.fn(), applyExtraction: vi.fn(), requestExtraction: vi.fn(),
}));
const candidate: api.ExtractionCandidate = {
  id: "candidate", field_key: "power_typical_w", value_numeric: 2, value_max: null, value_text: null, unit: "kW",
  raw_value: "2", raw_unit: "kW", source_text: "Typical power: 2 kW", page_number: 3, confidence: 0.9,
  flags: [], model_context: "CX-100", model_match: "target", conflict_group_key: null, review_status: "accepted",
  model_attribution_confirmed: false, review_note: "Verified",
};
const revision = { id: "revision", catalog_model_id: "model", version: 7, lifecycle_status: "draft" } as CatalogModelRevisionDetail;
function mount(readOnly = false, onChanged = vi.fn()) {
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
    <DatasheetPanel revision={revision} readOnly={readOnly} onChanged={onChanged} />
  </QueryClientProvider>);
  fireEvent.change(screen.getByLabelText("Attached datasheet"), { target: { value: "document" } });
  return onChanged;
}
beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listDatasheets).mockResolvedValue([{ id: "document", original_filename: "spec.pdf", version_number: 1, scan_status: "clean", sha256: "abc" }]);
  vi.mocked(api.listModelDatasheets).mockResolvedValue([]);
  vi.mocked(api.listApplications).mockResolvedValue([]);
  vi.mocked(api.listExtractionJobs).mockResolvedValue([{ id: "job", status: "completed", is_current: true,
    model_resolution: "single_model_matched", outcome: "complete", candidate_count: 1, error_message: null, warnings: [] }]);
  vi.mocked(api.listCandidates).mockResolvedValue([candidate]);
});
describe("datasheet review and apply", () => {
  it("shows evidence and applies only after explicit selection with the displayed version", async () => {
    mount();
    // Wait for the datasheet options before choosing it.
    await screen.findByRole("option", { name: /spec.pdf/ });
    fireEvent.change(screen.getByLabelText("Attached datasheet"), { target: { value: "document" } });
    await screen.findByText("Page 3: Typical power: 2 kW");
    expect(api.applyExtraction).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Select power_typical_w for apply"));
    vi.mocked(api.applyExtraction).mockResolvedValue({ id: "application", revision_version: 8, applied_at: "now", document_sha256: "abc", after_values: {}, candidates: [] });
    fireEvent.click(screen.getByRole("button", { name: "Apply selected accepted values to draft" }));
    await waitFor(() => expect(api.applyExtraction).toHaveBeenCalledWith("revision", "document", "job", ["candidate"], false, 7));
  });
  it("requires attribution confirmation for an unattributed value and never applies during review", async () => {
    vi.mocked(api.listCandidates).mockResolvedValue([{ ...candidate, review_status: "pending", model_match: "unattributed" }]);
    mount(); await screen.findByRole("option", { name: /spec.pdf/ });
    fireEvent.change(screen.getByLabelText("Attached datasheet"), { target: { value: "document" } });
    const accept = await screen.findByRole("button", { name: "Accept power_typical_w" });
    expect(accept).toBeDisabled();
    fireEvent.click(screen.getByLabelText("I confirm this value belongs to the target model."));
    fireEvent.click(accept);
    await waitFor(() => expect(api.reviewCandidate).toHaveBeenCalledWith("candidate", "accepted", "", true));
    expect(api.applyExtraction).not.toHaveBeenCalled();
  });
  it("shows stale conflicts and clears the selection", async () => {
    const changed = mount(); await screen.findByRole("option", { name: /spec.pdf/ });
    fireEvent.change(screen.getByLabelText("Attached datasheet"), { target: { value: "document" } });
    fireEvent.click(await screen.findByLabelText("Select power_typical_w for apply"));
    vi.mocked(api.applyExtraction).mockRejectedValue(new ApiError(409, "Conflict", "Draft changed", null));
    fireEvent.click(screen.getByRole("button", { name: "Apply selected accepted values to draft" }));
    await screen.findByRole("alert");
    expect(screen.getByLabelText("Select power_typical_w for apply")).not.toBeChecked();
    expect(changed).toHaveBeenCalled();
  });
  it("offers no write controls on a published revision", async () => {
    mount(true); await screen.findByRole("option", { name: /spec.pdf/ });
    fireEvent.change(screen.getByLabelText("Attached datasheet"), { target: { value: "document" } });
    const article = await screen.findByRole("article", { name: "power_typical_w candidate" });
    expect(within(article).queryByRole("checkbox")).toBeNull();
    expect(screen.queryByLabelText("Upload PDF datasheet")).toBeNull();
    expect(screen.queryByRole("button", { name: /Apply selected/ })).toBeNull();
  });
});
