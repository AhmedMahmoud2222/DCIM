import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as managedAssetApi from "@/features/managed-assets/api";
import * as powerApi from "@/features/power/api";
import * as racksApi from "@/features/racks/api";
import { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";
import { Rack } from "@/types";
import { RackDetailPage } from "@/features/racks/RackDetailPage";

vi.mock("@/features/managed-assets/api");
vi.mock("@/features/power/api");
vi.mock("@/features/racks/api");

const manager: CurrentUser = {
  id: "lifecycle-manager", email: "manager@example.com", full_name: "Lifecycle Manager", role_names: [],
  permission_codes: ["managed_asset:update_lifecycle"],
};

const rack: Rack = {
  id: "rack-1", asset_tag: "RACK-01", lifecycle_status: "planned", model_revision_id: "rev-1",
  name: "Rack 01", owner: null, notes: null, version: 1, created_at: "2026-01-01T00:00:00Z", placement: null,
};

describe("RackDetailPage lifecycle feedback", () => {
  beforeEach(() => {
    vi.mocked(racksApi.getRack).mockResolvedValue(rack);
    vi.mocked(racksApi.getRackElevation).mockResolvedValue({ rack_id: rack.id, rack_name: rack.name, height_u: 42, slots: [] });
    vi.mocked(racksApi.listRooms).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(powerApi.getEquipmentPowerSummary).mockResolvedValue({
      equipment_asset_id: "eq-1", feed_nodes: [], redundancy_classification: "no_power_modeled",
      effective_demand_kw: null, data_quality: "ok",
    });
  });

  it("shows and focuses a page-level notice after the rack is installed", async () => {
    vi.mocked(managedAssetApi.transitionManagedAssetLifecycle).mockResolvedValue({
      id: rack.id, asset_type: "rack", asset_tag: rack.asset_tag, serial_number: null,
      lifecycle_status: "installed", created_at: rack.created_at,
    });
    const user = userEvent.setup();
    renderWithProviders(<RackDetailPage />, { route: "/racks/rack-1", path: "/racks/:rackId", user: manager });

    await user.click(await screen.findByRole("button", { name: "Mark installed: Rack 01" }));
    await user.click(screen.getByRole("button", { name: "Confirm install Rack 01" }));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Rack 01 installation status saved.");
    expect(notice).toHaveFocus();
  });
});
