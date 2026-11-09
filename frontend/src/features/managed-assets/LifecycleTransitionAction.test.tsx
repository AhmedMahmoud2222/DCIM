import { screen } from "@testing-library/react";
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
    renderWithProviders(<LifecycleTransitionAction assetId={assetId} status="planned" assetLabel={assetLabel} />, { user: LIFECYCLE_MANAGER });
    await userEvent.click(screen.getByRole("button", { name: `Mark ${assetLabel} installed` }));
    expect(api.transitionManagedAssetLifecycle).toHaveBeenCalledWith(assetId, "installed");
  });

  it("hides the transition from users without lifecycle permission", () => {
    renderWithProviders(<LifecycleTransitionAction assetId="rack-1" status="planned" assetLabel="rack" />, { user: VIEWER_TEST_USER });
    expect(screen.queryByRole("button", { name: "Mark rack installed" })).not.toBeInTheDocument();
  });

  it("offers only the next valid transition from planned", () => {
    renderWithProviders(<LifecycleTransitionAction assetId="rack-1" status="active" assetLabel="rack" />, { user: LIFECYCLE_MANAGER });
    expect(screen.queryByRole("button", { name: "Mark rack installed" })).not.toBeInTheDocument();
  });
});
