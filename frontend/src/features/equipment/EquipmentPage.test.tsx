import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { EquipmentPage } from "@/features/equipment/EquipmentPage";
import * as equipmentApi from "@/features/equipment/api";
import * as racksApi from "@/features/racks/api";
import * as managedAssetApi from "@/features/managed-assets/api";
import { DEFAULT_LIFECYCLE_SETTINGS } from "@/features/managed-assets/useLifecycleSettings";
import { CurrentUser } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER, renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";
import { Equipment, Page, Rack } from "@/types";

vi.mock("@/features/equipment/api");
vi.mock("@/features/racks/api");
vi.mock("@/features/managed-assets/api");

const EQUIPMENT_IMPORTER_USER: CurrentUser = {
  ...ADMINISTRATOR_TEST_USER,
  permission_codes: ["equipment:read", "equipment:import"],
};
const EQUIPMENT_VIEWER_USER: CurrentUser = { ...VIEWER_TEST_USER, permission_codes: ["equipment:read"] };

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 200, offset: 0 };
}

describe("EquipmentPage bulk-import gating", () => {
  beforeEach(() => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(mockPage<Equipment>([]));
    vi.mocked(racksApi.listRacks).mockResolvedValue(mockPage<Rack>([]));
  });

  it("shows the Bulk Import button for a user with equipment:import", async () => {
    renderWithProviders(<EquipmentPage />, { user: EQUIPMENT_IMPORTER_USER });

    expect(await screen.findByRole("button", { name: "Bulk Import" })).toBeInTheDocument();
  });

  it("hides the Bulk Import button for a user without equipment:import", async () => {
    renderWithProviders(<EquipmentPage />, { user: EQUIPMENT_VIEWER_USER });

    await screen.findByRole("heading", { name: "Equipment" });
    expect(screen.queryByRole("button", { name: "Bulk Import" })).not.toBeInTheDocument();
  });
});

describe("EquipmentPage lifecycle feedback", () => {
  const manager: CurrentUser = {
    ...ADMINISTRATOR_TEST_USER,
    permission_codes: ["equipment:read", "managed_asset:update_lifecycle"],
  };

  it("shows and focuses a page-level notice after an equipment row installs", async () => {
    const eq: Equipment = {
      id: "eq-planned", asset_tag: "SRV-001", lifecycle_status: "planned", model_revision_id: "rev-1",
      catalog_model_revision_id: null, hostname: "srv-001", owner: null, service: null, environment: null,
      notes: null, version: 1, created_at: "2026-01-01T00:00:00Z", placement: null,
    };
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(mockPage([eq]));
    vi.mocked(managedAssetApi.getLifecycleSettings).mockResolvedValue(DEFAULT_LIFECYCLE_SETTINGS);
    vi.mocked(managedAssetApi.transitionManagedAssetLifecycle).mockResolvedValue({
      id: eq.id, asset_type: "equipment", asset_tag: eq.asset_tag, serial_number: null, lifecycle_status: "installed",
      created_at: eq.created_at,
    });
    const user = userEvent.setup();
    renderWithProviders(<EquipmentPage />, { user: manager });

    await user.click(await screen.findByRole("button", { name: "Mark installed: srv-001" }));
    await user.click(screen.getByRole("button", { name: "Confirm install srv-001" }));
    const notice = await screen.findByRole("status", { name: "" });
    expect(notice).toHaveTextContent("srv-001 installation status saved.");
    expect(notice).toHaveFocus();
  });
});

describe("EquipmentPage Idempotency-Key (issue #338)", () => {
  const KEY_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const creator: CurrentUser = { ...ADMINISTRATOR_TEST_USER, permission_codes: ["equipment:read", "equipment:create"] };
  const created: Equipment = {
    id: "eq-new", asset_tag: "SRV-9", lifecycle_status: "planned", model_revision_id: "rev-1",
    catalog_model_revision_id: null, hostname: null, owner: null, service: null, environment: null,
    notes: null, version: 1, created_at: "2026-01-01T00:00:00Z", placement: null,
  };

  beforeEach(() => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(mockPage<Equipment>([]));
    vi.mocked(racksApi.listRacks).mockResolvedValue(mockPage<Rack>([]));
    vi.mocked(equipmentApi.createEquipmentModel).mockResolvedValue({ id: "model-1" } as never);
    vi.mocked(equipmentApi.createEquipmentModelRevision).mockResolvedValue({ id: "rev-1" } as never);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  async function submit(user: ReturnType<typeof userEvent.setup>) {
    if (!screen.queryByPlaceholderText("Asset tag (e.g. SRV-0142)")) {
      await user.click(await screen.findByRole("button", { name: "New Equipment" }));
    }
    for (const [placeholder, value] of [
      ["Asset tag (e.g. SRV-0142)", "SRV-9"],
      ["Manufacturer", "Acme"],
      ["Model name", "X1"],
    ]) {
      await user.clear(screen.getByPlaceholderText(placeholder));
      await user.type(screen.getByPlaceholderText(placeholder), value);
    }
    await user.click(screen.getByRole("button", { name: "Create Equipment" }));
  }

  it("reuses the key across a retry and rotates it after success", async () => {
    const user = userEvent.setup();
    vi.mocked(equipmentApi.createEquipment)
      .mockRejectedValueOnce(new Error("Temporary failure."))
      .mockResolvedValueOnce(created)
      .mockResolvedValueOnce(created);
    renderWithProviders(<EquipmentPage />, { user: creator });

    await submit(user);
    await screen.findByText("Temporary failure.");
    await user.click(screen.getByRole("button", { name: "Create Equipment" }));
    await waitFor(() => expect(equipmentApi.createEquipment).toHaveBeenCalledTimes(2));
    const calls = vi.mocked(equipmentApi.createEquipment).mock.calls;
    expect(calls[1][1]).toBe(calls[0][1]);

    await submit(user);
    await waitFor(() => expect(equipmentApi.createEquipment).toHaveBeenCalledTimes(3));
    expect(vi.mocked(equipmentApi.createEquipment).mock.calls[2][1]).not.toBe(calls[0][1]);
  });

  it("falls back to getRandomValues when crypto.randomUUID is missing", async () => {
    const user = userEvent.setup();
    const realGetRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
    vi.stubGlobal("crypto", { getRandomValues: realGetRandomValues });
    vi.mocked(equipmentApi.createEquipment).mockResolvedValue(created);
    renderWithProviders(<EquipmentPage />, { user: creator });

    await submit(user);
    await waitFor(() => expect(equipmentApi.createEquipment).toHaveBeenCalledTimes(1));
    expect(vi.mocked(equipmentApi.createEquipment).mock.calls[0][1]).toMatch(KEY_PATTERN);
  });

  it("shows the actionable message and makes zero API calls without secure randomness", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("crypto", {});
    renderWithProviders(<EquipmentPage />, { user: creator });

    await submit(user);
    expect(await screen.findByText(/cannot generate a secure request key/i)).toBeInTheDocument();
    expect(equipmentApi.createEquipmentModel).not.toHaveBeenCalled();
    expect(equipmentApi.createEquipmentModelRevision).not.toHaveBeenCalled();
    expect(equipmentApi.createEquipment).not.toHaveBeenCalled();
  });
});
