import { screen, waitFor } from "@testing-library/react";
import { useState } from "react";
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
    expect(screen.getByRole("status")).toHaveTextContent("Installation status saved.");
    expect(screen.getByRole("status")).toHaveFocus();
  });

  it("hides the transition from users without lifecycle permission", () => {
    renderWithProviders(<LifecycleTransitionAction assetId="rack-1" assetName="Rack 01" status="planned" assetLabel="rack" />, { user: VIEWER_TEST_USER });
    expect(screen.queryByRole("button", { name: "Mark installed: Rack 01" })).not.toBeInTheDocument();
  });

  it("hides the transition for non-planned assets", () => {
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

  it("clears a failed transition message on cancel and restores focus", async () => {
    vi.mocked(api.transitionManagedAssetLifecycle).mockRejectedValue(new Error("Transition conflict"));
    renderWithProviders(
      <LifecycleTransitionAction assetId="equipment-1" assetName="server-01" status="planned" assetLabel="equipment" />,
      { user: LIFECYCLE_MANAGER },
    );
    const trigger = screen.getByRole("button", { name: "Mark installed: server-01" });
    await userEvent.click(trigger);
    const confirm = screen.getByRole("button", { name: "Confirm install server-01" });
    expect(confirm).toHaveFocus();
    await userEvent.click(confirm);
    expect(await screen.findByRole("alert")).toHaveTextContent("Transition conflict");
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mark installed: server-01" })).toHaveFocus();
  });

  it("keeps and focuses a conflict alert after refreshed status is no longer planned", async () => {
    vi.mocked(api.transitionManagedAssetLifecycle).mockRejectedValue(new Error("Asset was already installed"));
    function StatusHarness() {
      const [status, setStatus] = useState("planned");
      return (
        <>
          <LifecycleTransitionAction assetId="equipment-1" assetName="server-01" status={status} assetLabel="equipment" />
          <button type="button" onClick={() => setStatus("installed")}>Simulate refreshed status</button>
        </>
      );
    }
    renderWithProviders(
      <StatusHarness />,
      { user: LIFECYCLE_MANAGER },
    );
    await userEvent.click(screen.getByRole("button", { name: "Mark installed: server-01" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirm install server-01" }));
    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Simulate refreshed status" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Asset was already installed");
    expect(screen.getByRole("alert")).toHaveFocus();
  });
});
