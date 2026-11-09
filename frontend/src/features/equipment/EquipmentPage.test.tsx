import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EquipmentPage } from "@/features/equipment/EquipmentPage";
import * as equipmentApi from "@/features/equipment/api";
import * as racksApi from "@/features/racks/api";
import * as managedAssetApi from "@/features/managed-assets/api";
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
