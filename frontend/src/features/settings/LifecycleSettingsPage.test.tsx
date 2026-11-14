import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as managedAssetApi from "@/features/managed-assets/api";
import {
  DEFAULT_LIFECYCLE_SETTINGS,
  useLifecycleSettings,
} from "@/features/managed-assets/useLifecycleSettings";
import { LifecycleSettingsPage } from "@/features/settings/LifecycleSettingsPage";
import { ApiError } from "@/lib/apiClient";
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
  return { ...(JSON.parse(JSON.stringify(DEFAULT_LIFECYCLE_SETTINGS)) as LifecycleSettingsResponse), version: 1 };
}

describe("LifecycleSettingsPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(managedAssetApi.getLifecycleSettings).mockResolvedValue(cloneDefaultSettings());
  });

  it("displays the six visible states, excludes reserved from configuration rows, and shows legacy reserved notice", async () => {
    renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    // The loading view also renders the heading, so wait for authoritative content before asserting it.
    await screen.findByTestId("lifecycle-state-card-planned");
    expect(screen.getByRole("heading", { name: "Lifecycle Settings" })).toBeInTheDocument();
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

  it("passes the configuration version and exposes a clear conflict alert on HTTP 409 stale submission", async () => {
    const initial = { ...cloneDefaultSettings(), version: 4 };
    vi.mocked(managedAssetApi.getLifecycleSettings).mockResolvedValue(initial);
    vi.mocked(managedAssetApi.updateLifecycleSettings).mockRejectedValue(
      new ApiError(409, "Conflict", "LifecycleSettings 1 was modified concurrently (current version 5).", null),
    );

    const user = userEvent.setup();
    renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    await screen.findByTestId("lifecycle-state-card-planned");
    await user.click(screen.getByRole("button", { name: "Save Lifecycle Settings" }));

    await waitFor(() =>
      expect(managedAssetApi.updateLifecycleSettings).toHaveBeenCalledWith(expect.any(Array), 4),
    );
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Lifecycle settings were modified by another administrator — reload the latest configuration and try again.",
    );
  });

  it("offers a reload control after a 409 that refetches the settings and clears the conflict", async () => {
    const stale = { ...cloneDefaultSettings(), version: 4 };
    const latest = { ...cloneDefaultSettings(), version: 5 };
    vi.mocked(managedAssetApi.getLifecycleSettings).mockResolvedValueOnce(stale).mockResolvedValue(latest);
    vi.mocked(managedAssetApi.updateLifecycleSettings).mockRejectedValueOnce(
      new ApiError(409, "Conflict", "modified concurrently", null),
    );
    vi.mocked(managedAssetApi.updateLifecycleSettings).mockResolvedValueOnce({ ...latest, version: 6 });

    const user = userEvent.setup();
    renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    await screen.findByTestId("lifecycle-state-card-planned");
    await user.click(screen.getByRole("button", { name: "Save Lifecycle Settings" }));
    await user.click(await screen.findByRole("button", { name: "Reload latest configuration" }));

    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: "Save Lifecycle Settings" }));
    await waitFor(() =>
      expect(managedAssetApi.updateLifecycleSettings).toHaveBeenLastCalledWith(expect.any(Array), 5),
    );
  });

  it("keeps unsaved edits when the server copy changes and requires an explicit discard to reload", async () => {
    const first = { ...cloneDefaultSettings(), version: 1 };
    const changed = { ...cloneDefaultSettings(), version: 2 };
    changed.states[0] = { ...changed.states[0], label: "Other Admin Label" };
    vi.mocked(managedAssetApi.getLifecycleSettings).mockResolvedValueOnce(first).mockResolvedValue(changed);

    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    const card = await screen.findByTestId("lifecycle-state-card-planned");
    const labelInput = within(card).getByLabelText("Display label for planned");
    await user.clear(labelInput);
    await user.type(labelInput, "My Draft");

    await queryClient.invalidateQueries({ queryKey: ["lifecycle-settings"] });

    expect(await screen.findByRole("button", { name: "Discard my edits and reload" })).toBeInTheDocument();
    expect(within(screen.getByTestId("lifecycle-state-card-planned")).getByLabelText("Display label for planned")).toHaveValue(
      "My Draft",
    );
  });

  it("renders no editable form until settings load, then bases the draft on the loaded version", async () => {
    let resolve!: (v: LifecycleSettingsResponse) => void;
    vi.mocked(managedAssetApi.getLifecycleSettings).mockReturnValue(
      new Promise<LifecycleSettingsResponse>((r) => {
        resolve = r;
      }),
    );
    vi.mocked(managedAssetApi.updateLifecycleSettings).mockResolvedValue({ ...cloneDefaultSettings(), version: 8 });

    const user = userEvent.setup();
    renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    expect(await screen.findByText("Loading lifecycle settings…")).toBeInTheDocument();
    expect(screen.queryByTestId("lifecycle-state-card-planned")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save Lifecycle Settings" })).not.toBeInTheDocument();

    resolve({ ...cloneDefaultSettings(), version: 7 });
    await screen.findByTestId("lifecycle-state-card-planned");
    await user.click(screen.getByRole("button", { name: "Save Lifecycle Settings" }));
    await waitFor(() => expect(managedAssetApi.updateLifecycleSettings).toHaveBeenCalledWith(expect.any(Array), 7));
  });

  it("disables saving after a concurrent server change so a stale draft is never submitted", async () => {
    const first = { ...cloneDefaultSettings(), version: 1 };
    const changed = { ...cloneDefaultSettings(), version: 2 };
    vi.mocked(managedAssetApi.getLifecycleSettings).mockResolvedValueOnce(first).mockResolvedValue(changed);

    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    const card = await screen.findByTestId("lifecycle-state-card-planned");
    const labelInput = within(card).getByLabelText("Display label for planned");
    await user.clear(labelInput);
    await user.type(labelInput, "My Draft");
    await queryClient.invalidateQueries({ queryKey: ["lifecycle-settings"] });

    await screen.findByRole("button", { name: "Discard my edits and reload" });
    expect(screen.getByRole("button", { name: "Save Lifecycle Settings" })).toBeDisabled();
    expect(managedAssetApi.updateLifecycleSettings).not.toHaveBeenCalled();
  });

  it("never exposes an editable form from display-only fallback settings cached by another view", async () => {
    vi.mocked(managedAssetApi.getLifecycleSettings).mockRejectedValue(new Error("network down"));

    function EquipmentViewProbe() {
      const { isAuthoritative } = useLifecycleSettings();
      return <span data-testid="probe">{isAuthoritative ? "authoritative" : "fallback"}</span>;
    }

    renderWithProviders(
      <>
        <EquipmentViewProbe />
        <LifecycleSettingsPage />
      </>,
      { user: LIFECYCLE_ADMIN_USER },
    );

    expect(await screen.findByText("Could not load lifecycle settings.")).toBeInTheDocument();
    expect(screen.getByTestId("probe")).toHaveTextContent("fallback");
    expect(screen.queryByTestId("lifecycle-state-card-planned")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save Lifecycle Settings" })).not.toBeInTheDocument();
  });

  it("keeps the dirty draft and conflict state when a reload fails", async () => {
    const first = { ...cloneDefaultSettings(), version: 1 };
    const changed = { ...cloneDefaultSettings(), version: 2 };
    vi.mocked(managedAssetApi.getLifecycleSettings)
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(changed)
      .mockRejectedValue(new Error("network down"));

    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<LifecycleSettingsPage />, { user: LIFECYCLE_ADMIN_USER });

    const card = await screen.findByTestId("lifecycle-state-card-planned");
    const labelInput = within(card).getByLabelText("Display label for planned");
    await user.clear(labelInput);
    await user.type(labelInput, "My Draft");
    await queryClient.invalidateQueries({ queryKey: ["lifecycle-settings"] });

    await user.click(await screen.findByRole("button", { name: "Discard my edits and reload" }));

    expect(await screen.findByText(/Could not reload the latest lifecycle settings/)).toBeInTheDocument();
    expect(
      within(screen.getByTestId("lifecycle-state-card-planned")).getByLabelText("Display label for planned"),
    ).toHaveValue("My Draft");
    expect(screen.getByRole("button", { name: "Save Lifecycle Settings" })).toBeDisabled();
  });
});
