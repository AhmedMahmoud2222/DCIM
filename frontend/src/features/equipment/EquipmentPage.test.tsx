import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EquipmentPage } from "@/features/equipment/EquipmentPage";
import * as equipmentApi from "@/features/equipment/api";
import * as racksApi from "@/features/racks/api";
import { CurrentUser } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER, renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";
import { Equipment, Page, Rack } from "@/types";

vi.mock("@/features/equipment/api");
vi.mock("@/features/racks/api");

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
