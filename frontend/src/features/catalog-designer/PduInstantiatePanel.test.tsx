import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { PduInstantiatePanel } from "@/features/catalog-designer/PduInstantiatePanel";
import * as api from "@/features/power/api";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";
import { PDUInstantiatedAsset } from "@/types";

vi.mock("@/features/power/api");

function makeInstantiated(overrides: Partial<PDUInstantiatedAsset> = {}): PDUInstantiatedAsset {
  return {
    id: "pdu-node-1",
    node_type: "pdu",
    managed_asset_id: "asset-1",
    owning_asset_id: null,
    label: "Rack PDU A",
    retired_at: null,
    created_at: "2026-01-01T00:00:00Z",
    catalog_model_revision_id: "rev-1",
    outlet_count: 2,
    outlets: [
      {
        id: "outlet-node-1",
        node_type: "pdu_outlet",
        managed_asset_id: "asset-1",
        owning_asset_id: "asset-1",
        label: "Outlet 1",
        retired_at: null,
        created_at: "2026-01-01T00:00:00Z",
      },
      {
        id: "outlet-node-2",
        node_type: "pdu_outlet",
        managed_asset_id: "asset-1",
        owning_asset_id: "asset-1",
        label: "Outlet 2",
        retired_at: null,
        created_at: "2026-01-01T00:00:00Z",
      },
    ],
    ...overrides,
  };
}

describe("PduInstantiatePanel", () => {
  beforeEach(() => {
    vi.mocked(api.instantiatePdu).mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function fillAndSubmit(user: ReturnType<typeof userEvent.setup>, assetTag = "PDU-001") {
    await user.type(screen.getByLabelText("Asset tag"), assetTag);
    await user.type(screen.getByLabelText("Name"), "Rack PDU A");
    await user.click(screen.getByRole("button", { name: "Instantiate PDU" }));
  }

  it("sends asset identity and the revision id — never electrical values — with an idempotency key", async () => {
    const user = userEvent.setup();
    vi.mocked(api.instantiatePdu).mockResolvedValue(makeInstantiated());

    renderWithProviders(<PduInstantiatePanel revisionId="rev-1" />);

    await user.type(screen.getByLabelText("Asset tag"), "PDU-001");
    await user.type(screen.getByLabelText("Name"), "Rack PDU A");
    await user.selectOptions(screen.getByLabelText("Management protocol"), "snmp");
    await user.click(screen.getByRole("button", { name: "Instantiate PDU" }));

    await waitFor(() => expect(api.instantiatePdu).toHaveBeenCalledTimes(1));
    const [body, idempotencyKey] = vi.mocked(api.instantiatePdu).mock.calls[0];
    expect(body).toEqual({
      asset_tag: "PDU-001",
      name: "Rack PDU A",
      catalog_model_revision_id: "rev-1",
      ip_address: null,
      protocol: "snmp",
    });
    expect(Object.keys(body)).not.toContain("outlet_count");
    expect(typeof idempotencyKey).toBe("string");
    expect(idempotencyKey).not.toBe("");
  });

  it("shows the created PDU node, its managed asset and every outlet power-node identity", async () => {
    const user = userEvent.setup();
    vi.mocked(api.instantiatePdu).mockResolvedValue(makeInstantiated());

    renderWithProviders(<PduInstantiatePanel revisionId="rev-1" />);
    await user.type(screen.getByLabelText("Asset tag"), "PDU-001");
    await user.type(screen.getByLabelText("Name"), "Rack PDU A");
    await user.click(screen.getByRole("button", { name: "Instantiate PDU" }));

    expect(await screen.findByText(/PDU instantiated/i)).toBeInTheDocument();
    expect(screen.getByText("pdu-node-1")).toBeInTheDocument();
    expect(screen.getByText("asset-1")).toBeInTheDocument();
    expect(screen.getByText("Outlet 1")).toBeInTheDocument();
    expect(screen.getByText("outlet-node-1")).toBeInTheDocument();
    expect(screen.getByText("outlet-node-2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open Power Topology/ })).toHaveAttribute("href", "/power");
  });

  it("surfaces the backend's rejection of a revision that cannot be instantiated", async () => {
    const user = userEvent.setup();
    vi.mocked(api.instantiatePdu).mockRejectedValue(
      new ApiError(409, "Conflict", "This revision is retired and does not allow new installations.", null),
    );

    renderWithProviders(<PduInstantiatePanel revisionId="rev-1" />);
    await user.type(screen.getByLabelText("Asset tag"), "PDU-001");
    await user.type(screen.getByLabelText("Name"), "Rack PDU A");
    await user.click(screen.getByRole("button", { name: "Instantiate PDU" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This revision is retired and does not allow new installations.");
  });

  it("reuses the same Idempotency-Key across a retry so a double submit cannot create two PDUs", async () => {
    const user = userEvent.setup();
    vi.mocked(api.instantiatePdu)
      .mockRejectedValueOnce(new ApiError(503, "Unavailable", "Temporary failure.", null))
      .mockResolvedValueOnce(makeInstantiated());

    renderWithProviders(<PduInstantiatePanel revisionId="rev-1" />);
    await user.type(screen.getByLabelText("Asset tag"), "PDU-001");
    await user.type(screen.getByLabelText("Name"), "Rack PDU A");

    await user.click(screen.getByRole("button", { name: "Instantiate PDU" }));
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Instantiate PDU" }));

    await waitFor(() => expect(api.instantiatePdu).toHaveBeenCalledTimes(2));
    const firstKey = vi.mocked(api.instantiatePdu).mock.calls[0][1];
    const secondKey = vi.mocked(api.instantiatePdu).mock.calls[1][1];
    expect(secondKey).toBe(firstKey);
  });

  it("rotates the Idempotency-Key after a success so the next PDU gets its own key", async () => {
    const user = userEvent.setup();
    vi.mocked(api.instantiatePdu).mockResolvedValue(makeInstantiated());

    renderWithProviders(<PduInstantiatePanel revisionId="rev-1" />);
    await fillAndSubmit(user, "PDU-001");
    await user.click(await screen.findByRole("button", { name: "Instantiate another" }));
    await fillAndSubmit(user, "PDU-002");

    await waitFor(() => expect(api.instantiatePdu).toHaveBeenCalledTimes(2));
    const firstKey = vi.mocked(api.instantiatePdu).mock.calls[0][1];
    const secondKey = vi.mocked(api.instantiatePdu).mock.calls[1][1];
    expect(secondKey).not.toBe(firstKey);
  });

  it("keeps retry reuse and never touches Math.random when crypto.randomUUID is unavailable (issue #328)", async () => {
    const user = userEvent.setup();
    const mathRandom = vi.spyOn(Math, "random");
    const realGetRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
    vi.stubGlobal("crypto", { getRandomValues: realGetRandomValues });
    vi.mocked(api.instantiatePdu)
      .mockRejectedValueOnce(new ApiError(503, "Unavailable", "Temporary failure.", null))
      .mockResolvedValueOnce(makeInstantiated());

    renderWithProviders(<PduInstantiatePanel revisionId="rev-1" />);
    await fillAndSubmit(user);
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "Instantiate PDU" }));

    await waitFor(() => expect(api.instantiatePdu).toHaveBeenCalledTimes(2));
    const firstKey = vi.mocked(api.instantiatePdu).mock.calls[0][1];
    const secondKey = vi.mocked(api.instantiatePdu).mock.calls[1][1];
    expect(firstKey).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(secondKey).toBe(firstKey);
    expect(mathRandom).not.toHaveBeenCalled();
  });

  it("sends nothing and tells the user why when the browser has no secure randomness (issue #328)", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("crypto", {});

    renderWithProviders(<PduInstantiatePanel revisionId="rev-1" />);
    await fillAndSubmit(user);

    expect(await screen.findByRole("alert")).toHaveTextContent(/cannot generate a secure request key/i);
    expect(api.instantiatePdu).not.toHaveBeenCalled();
  });
});
