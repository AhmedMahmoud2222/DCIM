import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as managedAssetApi from "@/features/managed-assets/api";
import { DEFAULT_LIFECYCLE_SETTINGS } from "@/features/managed-assets/useLifecycleSettings";
import { LifecycleSettingsPage } from "@/features/settings/LifecycleSettingsPage";
import { CurrentUser } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER, renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";
import { LifecycleSettingsResponse } from "@/types";

vi.mock("@/features/managed-assets/api");

const LIFECYCLE_ADMIN_USER: CurrentUser = {
  ...ADMINISTRATOR_TEST_USER,
  role_names: ["Administrator"],
  permission_codes: ["managed_asset:read", "managed_asset:manage"],
};

function cloneDefaultSettings(): LifecycleSettingsResponse {
  return JSON.parse(JSON.stringify(DEFAULT_LIFECYCLE_SETTINGS)) as LifecycleSettingsResponse;
}

describe("LifecycleSettingsPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(managedAssetApi.getLifecycleSettings).mockResolvedValue(cloneDefaultSettings());
  });

  it("displays the six visible states, excludes reserved from configuration rows, and shows legacy reserved notice", async () => {
    renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    expect(await screen.findByRole("heading", { name: "Lifecycle Settings" })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Settings breadcrumb" })).toHaveTextContent(
      "Settings → Asset Management → Lifecycle Settings",
    );

    for (const status of ["planned", "installed", "active", "maintenance", "decommissioned", "removed"]) {
      expect(screen.getByTestId(`lifecycle-state-card-${status}`)).toBeInTheDocument();
    }
    expect(screen.queryByTestId("lifecycle-state-card-reserved")).not.toBeInTheDocument();

    const policyNotice = screen.getByTestId("lifecycle-policy-note");
    expect(policyNotice).toHaveTextContent("reserved");
    expect(policyNotice).toHaveTextContent("excluded from selection");
  });

  it("allows an administrator to customize display label, description, badge color, display order, and allowed transitions and persist them", async () => {
    const savedResponse = cloneDefaultSettings();
    savedResponse.states[2] = {
      ...savedResponse.states[2],
      label: "In Production",
      description: "Live production equipment",
      badge_color: "info",
      allowed_transitions: ["decommissioned"],
    };
    vi.mocked(managedAssetApi.updateLifecycleSettings).mockResolvedValue(savedResponse);

    const user = userEvent.setup();
    renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    await waitFor(() => expect(managedAssetApi.getLifecycleSettings).toHaveBeenCalledTimes(1));
    const activeCard = await screen.findByTestId("lifecycle-state-card-active");

    const labelInput = within(activeCard).getByLabelText("Display label for active");
    await user.clear(labelInput);
    await user.type(labelInput, "In Production");

    const descInput = within(activeCard).getByLabelText("Description for active");
    await user.clear(descInput);
    await user.type(descInput, "Live production equipment");

    const colorSelect = within(activeCard).getByLabelText("Badge color for active");
    await user.selectOptions(colorSelect, "info");

    // Toggle off active -> maintenance transition while keeping active -> decommissioned
    const maintenanceCheckbox = within(activeCard).getByRole("checkbox", {
      name: /Allow transition from active to maintenance/i,
    });
    expect(maintenanceCheckbox).toBeChecked();
    await user.click(maintenanceCheckbox);
    expect(maintenanceCheckbox).not.toBeChecked();

    // Move 'active' up in display order
    await user.click(within(activeCard).getByRole("button", { name: "Move active up" }));

    await user.click(screen.getByRole("button", { name: "Save Lifecycle Settings" }));

    await waitFor(() => expect(managedAssetApi.updateLifecycleSettings).toHaveBeenCalledTimes(1));
    const submittedStates = vi.mocked(managedAssetApi.updateLifecycleSettings).mock.calls[0][0];
    const submittedActive = submittedStates.find((s) => s.status === "active")!;
    expect(submittedActive.label).toBe("In Production");
    expect(submittedActive.description).toBe("Live production equipment");
    expect(submittedActive.badge_color).toBe("info");
    expect(submittedActive.allowed_transitions).toEqual(["decommissioned"]);
    expect(submittedActive.display_order).toBe(2);

    expect(await screen.findByRole("status")).toHaveTextContent("Lifecycle settings saved.");
  });

  it("allows an administrator to restore default settings", async () => {
    vi.mocked(managedAssetApi.restoreDefaultLifecycleSettings).mockResolvedValue(cloneDefaultSettings());
    const user = userEvent.setup();
    renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    await screen.findByTestId("lifecycle-state-card-planned");
    await user.click(screen.getByRole("button", { name: "Restore Default Settings" }));

    await waitFor(() => expect(managedAssetApi.restoreDefaultLifecycleSettings).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("status")).toHaveTextContent("Default lifecycle settings restored.");
  });

  it("restricts modification for non-administrators and hides save/restore buttons", async () => {
    renderWithProviders(<LifecycleSettingsPage />, {
      user: { ...VIEWER_TEST_USER, permission_codes: ["managed_asset:read"] },
    });

    expect(await screen.findByRole("heading", { name: "Access restricted" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save Lifecycle Settings" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Restore Default Settings" })).not.toBeInTheDocument();
  });
});
