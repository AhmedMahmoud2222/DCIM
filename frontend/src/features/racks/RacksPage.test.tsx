import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

describe("RacksPage Idempotency-Key (issue #338)", () => {
  const KEY_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const creator: CurrentUser = { ...ADMINISTRATOR_TEST_USER, permission_codes: ["rack:read", "rack:create"] };
  const created: Rack = {
    id: "rack-new", asset_tag: "RACK-9", lifecycle_status: "planned", model_revision_id: "rev-1",
    name: "Rack 9", owner: null, notes: null, version: 1, created_at: "2026-01-01T00:00:00Z", placement: null,
  };

  beforeEach(() => {
    vi.mocked(api.listRacks).mockResolvedValue(mockPage<Rack>([]));
    vi.mocked(api.listRooms).mockResolvedValue(mockPage<Room>([]));
    vi.mocked(api.createRackModel).mockResolvedValue({ id: "model-1" } as never);
    vi.mocked(api.createRackModelRevision).mockResolvedValue({ id: "rev-1" } as never);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  async function submit(user: ReturnType<typeof userEvent.setup>) {
    if (!screen.queryByPlaceholderText("Display name")) {
      await user.click(await screen.findByRole("button", { name: "New Rack" }));
    }
    for (const [placeholder, value] of [
      ["Asset tag (e.g. RACK-A01)", "RACK-9"],
      ["Display name", "Rack 9"],
      ["Manufacturer", "Acme"],
      ["Model name", "R1"],
    ]) {
      await user.clear(screen.getByPlaceholderText(placeholder));
      await user.type(screen.getByPlaceholderText(placeholder), value);
    }
    await user.click(screen.getByRole("button", { name: "Create Rack" }));
  }

  it("reuses the key across a retry and rotates it after success", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createRack)
      .mockRejectedValueOnce(new Error("Temporary failure."))
      .mockResolvedValueOnce(created)
      .mockResolvedValueOnce(created);
    renderWithProviders(<RacksPage />, { user: creator });

    await submit(user);
    await screen.findByText("Temporary failure.");
    await user.click(screen.getByRole("button", { name: "Create Rack" }));
    await waitFor(() => expect(api.createRack).toHaveBeenCalledTimes(2));
    const calls = vi.mocked(api.createRack).mock.calls;
    expect(calls[1][1]).toBe(calls[0][1]);

    await submit(user);
    await waitFor(() => expect(api.createRack).toHaveBeenCalledTimes(3));
    expect(vi.mocked(api.createRack).mock.calls[2][1]).not.toBe(calls[0][1]);
  });

  it("falls back to getRandomValues when crypto.randomUUID is missing", async () => {
    const user = userEvent.setup();
    const realGetRandomValues = globalThis.crypto.getRandomValues.bind(globalThis.crypto);
    vi.stubGlobal("crypto", { getRandomValues: realGetRandomValues });
    vi.mocked(api.createRack).mockResolvedValue(created);
    renderWithProviders(<RacksPage />, { user: creator });

    await submit(user);
    await waitFor(() => expect(api.createRack).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.createRack).mock.calls[0][1]).toMatch(KEY_PATTERN);
  });

  it("shows the actionable message and makes zero API calls without secure randomness", async () => {
    const user = userEvent.setup();
    vi.stubGlobal("crypto", {});
    renderWithProviders(<RacksPage />, { user: creator });

    await submit(user);
    expect(await screen.findByText(/cannot generate a secure request key/i)).toBeInTheDocument();
    expect(api.createRackModel).not.toHaveBeenCalled();
    expect(api.createRackModelRevision).not.toHaveBeenCalled();
    expect(api.createRack).not.toHaveBeenCalled();
  });
});
