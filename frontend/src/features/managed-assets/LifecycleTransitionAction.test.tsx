import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LifecycleTransitionAction } from "@/features/managed-assets/LifecycleTransitionAction";
import * as api from "@/features/managed-assets/api";
import { CurrentUser } from "@/lib/authStore";
import { renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";

vi.mock("@/features/managed-assets/api");

const LIFECYCLE_MANAGER: CurrentUser = {
  ...VIEWER_TEST_USER,
  permission_codes: ["managed_asset:update_lifecycle"],
};

describe("LifecycleTransitionAction", () => {
  beforeEach(() => vi.resetAllMocks());

  it.each([
    { assetLabel: "rack" as const, assetId: "rack-1" },
    { assetLabel: "equipment" as const, assetId: "equipment-1" },
  ])("lets an authorized user install a planned $assetLabel", async ({ assetLabel, assetId }) => {
    vi.mocked(api.transitionManagedAssetLifecycle).mockResolvedValue({
      id: assetId, asset_type: assetLabel, asset_tag: assetId, serial_number: null,
      lifecycle_status: "installed", created_at: "2026-01-01T00:00:00Z",
    });
    const assetName = assetLabel === "rack" ? "Rack 01" : "server-01";
    const { queryClient } = renderWithProviders(<LifecycleTransitionAction assetId={assetId} assetName={assetName} status="planned" assetLabel={assetLabel} />, { user: LIFECYCLE_MANAGER });
    const queryKey = [assetLabel === "rack" ? "racks" : "equipment"];
    queryClient.setQueryData(queryKey, []);
    await userEvent.click(screen.getByRole("button", { name: `Mark installed: ${assetName}` }));
    expect(await screen.findByRole("status")).toHaveTextContent("cannot be changed back to planned");
    await userEvent.click(screen.getByRole("button", { name: `Confirm install ${assetName}` }));
    expect(api.transitionManagedAssetLifecycle).toHaveBeenCalledWith(assetId, "installed");
    await waitFor(() => expect(queryClient.getQueryState(queryKey)?.isInvalidated).toBe(true));
  });

  it("hides the transition from users without lifecycle permission", () => {
    renderWithProviders(<LifecycleTransitionAction assetId="rack-1" assetName="Rack 01" status="planned" assetLabel="rack" />, { user: VIEWER_TEST_USER });
    expect(screen.queryByRole("button", { name: "Mark installed: Rack 01" })).not.toBeInTheDocument();
  });

  it("offers only the next valid transition from planned", () => {
    renderWithProviders(<LifecycleTransitionAction assetId="rack-1" assetName="Rack 01" status="active" assetLabel="rack" />, { user: LIFECYCLE_MANAGER });
    expect(screen.queryByRole("button", { name: "Mark installed: Rack 01" })).not.toBeInTheDocument();
  });

  it("announces transition errors and refreshes stale status", async () => {
    vi.mocked(api.transitionManagedAssetLifecycle).mockRejectedValue(new Error("Cannot transition from 'installed' to 'installed'."));
    const { queryClient } = renderWithProviders(
      <LifecycleTransitionAction assetId="equipment-1" assetName="server-01" status="planned" assetLabel="equipment" />,
      { user: LIFECYCLE_MANAGER },
    );
    queryClient.setQueryData(["equipment"], []);
    await userEvent.click(screen.getByRole("button", { name: "Mark installed: server-01" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm install server-01" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cannot transition from 'installed' to 'installed'.");
    await waitFor(() => expect(queryClient.getQueryState(["equipment"])?.isInvalidated).toBe(true));
  });
});
