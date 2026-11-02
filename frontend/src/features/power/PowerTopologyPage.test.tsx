import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/power/api";
import { PowerTopologyPage } from "@/features/power/PowerTopologyPage";
import { ApiError } from "@/lib/apiClient";
import { ADMINISTRATOR_TEST_USER, renderWithProviders } from "@/test/renderWithProviders";
import { Page, PowerNode } from "@/types";

vi.mock("@/features/power/api");

const POWER_READER_TEST_USER = { ...ADMINISTRATOR_TEST_USER, permission_codes: ["power:read"] };
const POWER_MANAGER_TEST_USER = { ...ADMINISTRATOR_TEST_USER, permission_codes: ["power:read", "power:manage"] };

function makeNode(overrides: Partial<PowerNode> = {}): PowerNode {
  return {
    id: "node-1",
    node_type: "pdu_outlet",
    managed_asset_id: "asset-1",
    owning_asset_id: "pdu-asset-1",
    label: "Outlet 1",
    retired_at: null,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function pageOf(items: PowerNode[]): Page<PowerNode> {
  return { items, total: items.length, limit: 200, offset: 0 };
}

const PDU_NODE = makeNode({ id: "pdu-node-1", node_type: "pdu", label: "Rack PDU A", owning_asset_id: null });
const OUTLET_1 = makeNode({ id: "outlet-node-1", label: "Outlet 1" });
const OUTLET_2 = makeNode({ id: "outlet-node-2", label: "Outlet 2" });
const EQUIPMENT_INPUT = makeNode({
  id: "equipment-input-1",
  node_type: "equipment_power_input",
  label: "Server A PSU 1",
  owning_asset_id: "equipment-asset-1",
});

describe("PowerTopologyPage PDU outlets panel", () => {
  beforeEach(() => {
    vi.mocked(api.listPowerNodes).mockImplementation((nodeType?: string) =>
      Promise.resolve(nodeType === "pdu_outlet" ? pageOf([OUTLET_1, OUTLET_2]) : pageOf([PDU_NODE, OUTLET_1, OUTLET_2, EQUIPMENT_INPUT])),
    );
  });

  it("lists instantiated PDU outlets and their power-node identities, filtered to pdu_outlet", async () => {
    renderWithProviders(<PowerTopologyPage />, { user: POWER_READER_TEST_USER });

    const rows = await screen.findAllByTestId("pdu-outlet-node-row");
    expect(rows).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Outlet 1" })).toBeInTheDocument();
    expect(screen.getByText("outlet-node-1")).toBeInTheDocument();
    expect(screen.getByText("outlet-node-2")).toBeInTheDocument();
    // The outlet's owning asset is another identity the panel must expose.
    expect(screen.getAllByText("pdu-asset-1")).toHaveLength(2);
    expect(api.listPowerNodes).toHaveBeenCalledWith("pdu_outlet");
  });

  it("stays read-only for a user without power:manage", async () => {
    renderWithProviders(<PowerTopologyPage />, { user: POWER_READER_TEST_USER });

    await screen.findAllByTestId("pdu-outlet-node-row");
    expect(screen.getByText("Read-only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Connect outlet/ })).not.toBeInTheDocument();
  });

  it("connects an outlet into the topology through POST /power/connections (outlet as source)", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createConnection).mockResolvedValue({
      id: "conn-1",
      source_node_id: "outlet-node-1",
      target_node_id: "equipment-input-1",
      connection_type: "power",
      feed_label: "single",
      phase: null,
      voltage: null,
      rated_current_a: null,
      status: "active",
      version: 1,
      effective_from: "2026-01-01T00:00:00Z",
      effective_to: null,
    });

    renderWithProviders(<PowerTopologyPage />, { user: POWER_MANAGER_TEST_USER });

    await user.click(await screen.findByRole("button", { name: "Connect outlet Outlet 1" }));
    await user.selectOptions(screen.getByLabelText("Target node"), "equipment-input-1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() =>
      expect(api.createConnection).toHaveBeenCalledWith({
        source_node_id: "outlet-node-1",
        target_node_id: "equipment-input-1",
        feed_label: "single",
      }),
    );
  });

  it("surfaces an API rejection when the connection is refused", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createConnection).mockRejectedValue(
      new ApiError(409, "Conflict", "A connection between these nodes already exists.", null),
    );

    renderWithProviders(<PowerTopologyPage />, { user: POWER_MANAGER_TEST_USER });

    await user.click(await screen.findByRole("button", { name: "Connect outlet Outlet 2" }));
    await user.selectOptions(screen.getByLabelText("Target node"), "equipment-input-1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("A connection between these nodes already exists.");
  });

  it("does not offer a retired outlet for connection", async () => {
    vi.mocked(api.listPowerNodes).mockImplementation((nodeType?: string) =>
      Promise.resolve(
        nodeType === "pdu_outlet" ? pageOf([OUTLET_1, makeNode({ id: "outlet-node-3", label: "Outlet 3", retired_at: "2026-03-01T00:00:00Z" })]) : pageOf([]),
      ),
    );

    renderWithProviders(<PowerTopologyPage />, { user: POWER_MANAGER_TEST_USER });

    expect(await screen.findByRole("button", { name: "Connect outlet Outlet 1" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect outlet Outlet 3" })).not.toBeInTheDocument();
    expect(screen.getByText("retired")).toBeInTheDocument();
  });
});

describe("PowerTopologyPage without power:read", () => {
  it("explains the restriction and requests nothing", () => {
    const user = { ...ADMINISTRATOR_TEST_USER, permission_codes: ["power:manage"] };
    renderWithProviders(<PowerTopologyPage />, { user });

    expect(screen.getByRole("heading", { name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText("power:read")).toBeInTheDocument();
    expect(screen.queryByText(/No power nodes/i)).not.toBeInTheDocument();
    expect(api.listPowerNodes).not.toHaveBeenCalled();
  });
});
