import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/catalog-designer/api";
import { PduSpecPanel } from "@/features/catalog-designer/PduSpecPanel";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";
import { CatalogModelRevisionDetail, PDUDraft } from "@/types";

vi.mock("@/features/catalog-designer/api");

function makeDraft(overrides: Partial<PDUDraft> = {}): PDUDraft {
  return {
    revision_id: "rev-1",
    lifecycle_status: "draft",
    pdu_input_voltage_v: 230,
    pdu_input_current_a: 32,
    pdu_phase: "single",
    mounting_orientation: "vertical",
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
      {
        id: "outlet-2",
        stable_key: "c13-2",
        outlet_number: 2,
        label: "Outlet 2",
        outlet_type: "iec_c13",
        outlet_count: 1,
        phase: "single",
        rated_voltage_v: 230,
        rated_current_a: 10,
        position: null,
        side: "front",
        sort_order: 1,
      },
    ],
    version: 3,
    ...overrides,
  };
}

function makeRevision(overrides: Partial<CatalogModelRevisionDetail> = {}): CatalogModelRevisionDetail {
  return {
    id: "rev-1",
    catalog_model_id: "model-1",
    revision_number: 1,
    lifecycle_status: "draft",
    dimension_unit: "mm",
    width_value: 44,
    height_value: 1800,
    depth_value: 60,
    rack_unit_height: null,
    weight_unit: "kg",
    weight_value: 6,
    mounting_orientation: "vertical",
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
    version: 3,
    created_at: "2026-01-01T00:00:00Z",
    network_ports: [],
    power_supplies: [],
    monitoring_metrics: [],
    graphics: [],
    ...overrides,
  };
}

describe("PduSpecPanel", () => {
  beforeEach(() => {
    vi.mocked(api.updatePduDraft).mockReset();
  });

  it("renders the electrical specification, the mounting contract note and the outlet table", () => {
    renderWithProviders(<PduSpecPanel revision={makeRevision()} draft={makeDraft()} readOnly={false} onSaved={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "PDU Electrical Specification" })).toBeInTheDocument();
    expect(screen.getByLabelText("Rated input voltage (V)")).toHaveValue(230);
    expect(screen.getByLabelText("Rated input current (A)")).toHaveValue(32);
    expect(screen.getByLabelText("Phase")).toHaveValue("single");
    expect(screen.getByLabelText("Mounting orientation")).toHaveValue("vertical");
    // Requirement 5: mounting is catalog metadata; the note clarifies that placing an
    // instantiated PDU happens separately through the power PDU placement API (R5).
    expect(screen.getByText(/does not itself create a placement record/i)).toBeInTheDocument();
    expect(screen.getByText(/power PDU placement API/i)).toBeInTheDocument();
    expect(screen.getAllByTestId("pdu-outlet-row")).toHaveLength(2);
    expect(screen.getByLabelText("Outlet 1 label")).toHaveValue("Outlet 1");
  });

  it("saves the edited spec and the whole outlet table with the draft version as If-Match", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    vi.mocked(api.updatePduDraft).mockResolvedValue(makeDraft({ version: 4 }));

    renderWithProviders(<PduSpecPanel revision={makeRevision()} draft={makeDraft()} readOnly={false} onSaved={onSaved} />);

    const voltage = screen.getByLabelText("Rated input voltage (V)");
    await user.clear(voltage);
    await user.type(voltage, "400");
    await user.selectOptions(screen.getByLabelText("Phase"), "three");
    await user.click(screen.getByRole("button", { name: "Save PDU specification" }));

    await waitFor(() => expect(api.updatePduDraft).toHaveBeenCalledTimes(1));
    const [revisionId, body, ifMatch] = vi.mocked(api.updatePduDraft).mock.calls[0];
    expect(revisionId).toBe("rev-1");
    expect(ifMatch).toBe(3);
    expect(body.pdu_input_voltage_v).toBe(400);
    expect(body.pdu_phase).toBe("three");
    expect(body.mounting_orientation).toBe("vertical");
    expect(body.outlets).toHaveLength(2);
    expect(body.outlets?.[0]).toEqual(
      expect.objectContaining({ stable_key: "c13-1", outlet_number: 1, outlet_type: "iec_c13", side: "front" }),
    );
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
  });

  it("adds and removes outlet rows", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PduSpecPanel revision={makeRevision()} draft={makeDraft()} readOnly={false} onSaved={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "+ Add outlet" }));
    expect(screen.getAllByTestId("pdu-outlet-row")).toHaveLength(3);
    expect(screen.getByLabelText("Outlet 3 label")).toHaveValue("Outlet 3");
    expect(screen.getByLabelText("Outlet 3 stable key")).toHaveValue("outlet-3");

    await user.click(screen.getByRole("button", { name: "Remove outlet 3" }));
    expect(screen.getAllByTestId("pdu-outlet-row")).toHaveLength(2);
  });

  it("blocks saving with the server's duplicate-outlet-number rule before any request", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PduSpecPanel revision={makeRevision()} draft={makeDraft()} readOnly={false} onSaved={vi.fn()} />);

    const secondNumber = screen.getByLabelText("Outlet 2 number");
    await user.clear(secondNumber);
    await user.type(secondNumber, "1");
    await user.click(screen.getByRole("button", { name: "Save PDU specification" }));

    expect(await screen.findByText("Duplicate outlet number 1")).toBeInTheDocument();
    expect(api.updatePduDraft).not.toHaveBeenCalled();
  });

  it("surfaces the server's electrical validation messages (rated input current > 0)", async () => {
    const user = userEvent.setup();
    renderWithProviders(<PduSpecPanel revision={makeRevision()} draft={makeDraft()} readOnly={false} onSaved={vi.fn()} />);

    const current = screen.getByLabelText("Rated input current (A)");
    await user.clear(current);
    await user.type(current, "0");
    await user.click(screen.getByRole("button", { name: "Save PDU specification" }));

    expect(await screen.findByText("Input current must be > 0.")).toBeInTheDocument();
    expect(api.updatePduDraft).not.toHaveBeenCalled();
  });

  it("explains an optimistic-concurrency 409 instead of reporting a bare error", async () => {
    const user = userEvent.setup();
    vi.mocked(api.updatePduDraft).mockRejectedValue(new ApiError(409, "Conflict", "This revision is published and immutable.", null));

    renderWithProviders(<PduSpecPanel revision={makeRevision()} draft={makeDraft()} readOnly={false} onSaved={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Save PDU specification" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/optimistic-concurrency conflict/i);
  });

  it("decodes the backend's JSON validation payload from a 422", async () => {
    const user = userEvent.setup();
    const payload = JSON.stringify({
      valid: false,
      errors: [{ field: "pdu_input_voltage_v", code: "invalid_voltage", message: "Input voltage must be >= 0." }],
      warnings: [],
    });
    vi.mocked(api.updatePduDraft).mockRejectedValue(new ApiError(422, "Unprocessable", payload, null));

    renderWithProviders(<PduSpecPanel revision={makeRevision()} draft={makeDraft()} readOnly={false} onSaved={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: "Save PDU specification" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("pdu_input_voltage_v: Input voltage must be >= 0.");
  });

  it("renders read-only with no edit or save controls", () => {
    renderWithProviders(<PduSpecPanel revision={makeRevision()} draft={makeDraft()} readOnly onSaved={vi.fn()} />);

    expect(screen.getByText("Read-only")).toBeInTheDocument();
    expect(screen.getByLabelText("Rated input voltage (V)")).toBeDisabled();
    expect(screen.getByLabelText("Outlet 1 label")).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Save PDU specification" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "+ Add outlet" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove outlet 1" })).not.toBeInTheDocument();
  });

  it("tells the user an empty outlet table cannot satisfy publish", () => {
    renderWithProviders(
      <PduSpecPanel revision={makeRevision()} draft={makeDraft({ outlets: [] })} readOnly={false} onSaved={vi.fn()} />,
    );

    expect(screen.getByText(/a published PDU revision needs at least one/i)).toBeInTheDocument();
  });
});
