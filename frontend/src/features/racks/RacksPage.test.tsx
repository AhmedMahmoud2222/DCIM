import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/racks/api";
import * as managedAssetApi from "@/features/managed-assets/api";
import { RacksPage } from "@/features/racks/RacksPage";
import { ADMINISTRATOR_TEST_USER, renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";
import { CurrentUser } from "@/lib/authStore";
import { Page, Rack, Room } from "@/types";

vi.mock("@/features/racks/api");
vi.mock("@/features/managed-assets/api");

// RacksPage.tsx's "New Rack" toggle is gated only by an authenticated session (any
// permission), so exercising the "Bulk Import" gate needs its own users carrying/lacking
// rack:import specifically, rather than reusing ADMINISTRATOR_TEST_USER/VIEWER_TEST_USER
// as-is (catalog-designer permissions, per src/test/renderWithProviders.tsx).
const RACK_IMPORTER_USER: CurrentUser = { ...ADMINISTRATOR_TEST_USER, permission_codes: ["rack:read", "rack:import"] };
const RACK_VIEWER_USER: CurrentUser = { ...VIEWER_TEST_USER, permission_codes: ["rack:read"] };

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 200, offset: 0 };
}

describe("RacksPage bulk-import gating", () => {
  beforeEach(() => {
    vi.mocked(api.listRacks).mockResolvedValue(mockPage<Rack>([]));
    vi.mocked(api.listRooms).mockResolvedValue(mockPage<Room>([]));
  });

  it("shows the Bulk Import button for a user with rack:import", async () => {
    renderWithProviders(<RacksPage />, { user: RACK_IMPORTER_USER });

    expect(await screen.findByRole("button", { name: "Bulk Import" })).toBeInTheDocument();
  });

  it("hides the Bulk Import button for a user without rack:import", async () => {
    renderWithProviders(<RacksPage />, { user: RACK_VIEWER_USER });

    await screen.findByRole("heading", { name: "Racks" });
    expect(screen.queryByRole("button", { name: "Bulk Import" })).not.toBeInTheDocument();
  });
});

describe("RacksPage lifecycle feedback", () => {
  const manager: CurrentUser = {
    ...ADMINISTRATOR_TEST_USER,
    permission_codes: ["rack:read", "managed_asset:update_lifecycle"],
  };

  it("shows and focuses a page-level notice after a rack row installs", async () => {
    const rack: Rack = {
      id: "rack-planned", asset_tag: "RACK-01", lifecycle_status: "planned", model_revision_id: "rev-1",
      name: "Rack 01", owner: null, notes: null, version: 1, created_at: "2026-01-01T00:00:00Z", placement: null,
    };
    vi.mocked(api.listRacks).mockResolvedValue(mockPage([rack]));
    vi.mocked(managedAssetApi.transitionManagedAssetLifecycle).mockResolvedValue({
      id: rack.id, asset_type: "rack", asset_tag: rack.asset_tag, serial_number: null, lifecycle_status: "installed",
      created_at: rack.created_at,
    });
    const user = userEvent.setup();
    renderWithProviders(<RacksPage />, { user: manager });

    await user.click(await screen.findByRole("button", { name: "Mark installed: Rack 01" }));
    await user.click(screen.getByRole("button", { name: "Confirm install Rack 01" }));
    const notice = await screen.findByRole("status", { name: "" });
    expect(notice).toHaveTextContent("Rack 01 installation status saved.");
    expect(notice).toHaveFocus();
  });
});
