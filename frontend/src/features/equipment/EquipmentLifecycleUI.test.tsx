import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EquipmentDetailPage } from "@/features/equipment/EquipmentDetailPage";
import { EquipmentPage } from "@/features/equipment/EquipmentPage";
import * as equipmentApi from "@/features/equipment/api";
import * as managedAssetApi from "@/features/managed-assets/api";
import { DEFAULT_LIFECYCLE_SETTINGS } from "@/features/managed-assets/useLifecycleSettings";
import * as powerApi from "@/features/power/api";
import * as racksApi from "@/features/racks/api";
import * as telemetryApi from "@/features/telemetry/api";
import { CurrentUser } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER, renderWithProviders } from "@/test/renderWithProviders";
import { Equipment, Page, Rack } from "@/types";

vi.mock("@/features/equipment/api");
vi.mock("@/features/managed-assets/api");
vi.mock("@/features/power/api");
vi.mock("@/features/racks/api");
vi.mock("@/features/telemetry/api");

const FULL_EQUIPMENT_MANAGER: CurrentUser = {
  ...ADMINISTRATOR_TEST_USER,
  role_names: ["Administrator"],
  permission_codes: [
    "equipment:read",
    "equipment:manage",
    "managed_asset:read",
    "managed_asset:manage",
    "managed_asset:update_lifecycle",
  ],
};

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 200, offset: 0 };
}

const baseEquipment: Equipment = {
  id: "eq-active",
  asset_tag: "SRV-100",
  lifecycle_status: "active",
  model_revision_id: "rev-1",
  catalog_model_revision_id: null,
  hostname: "srv-prod-01",
  owner: "Platform Team",
  service: "Core Compute",
  environment: "production",
  notes: "Primary compute node",
  version: 3,
  created_at: "2026-01-01T00:00:00Z",
  placement: null,
};

describe("Equipment Lifecycle UI (Listing, Details, Editing, History, Legacy Reserved)", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(managedAssetApi.getLifecycleSettings).mockResolvedValue(DEFAULT_LIFECYCLE_SETTINGS);
    vi.mocked(racksApi.listRacks).mockResolvedValue(mockPage<Rack>([]));
    vi.mocked(racksApi.listRooms).mockResolvedValue({ items: [], total: 0, limit: 200, offset: 0 });
    vi.mocked(equipmentApi.getEquipmentPorts).mockResolvedValue({ ports: [], power_inlets: [] });
    vi.mocked(powerApi.getEquipmentPowerSummary).mockResolvedValue({
      equipment_asset_id: baseEquipment.id,
      feed_nodes: [],
      redundancy_classification: "no_power_modeled",
      effective_demand_kw: null,
      data_quality: "ok",
    });
    vi.mocked(telemetryApi.getLatestTelemetry).mockResolvedValue([]);
    vi.mocked(telemetryApi.getAlarmHistory).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(telemetryApi.getLatestPortStatusForEquipment).mockResolvedValue([]);
    vi.mocked(managedAssetApi.getManagedAssetLifecycleHistory).mockResolvedValue({
      items: [
        {
          id: "hist-2",
          asset_id: baseEquipment.id,
          from_status: "installed",
          to_status: "active",
          reason: "Commissioned into production",
          actor_user_id: "u-1",
          actor_name: "admin@dcim.local",
          timestamp: "2026-02-01T10:00:00Z",
        },
        {
          id: "hist-1",
          asset_id: baseEquipment.id,
          from_status: "planned",
          to_status: "installed",
          reason: null,
          actor_user_id: "u-1",
          actor_name: "admin@dcim.local",
          timestamp: "2026-01-15T09:00:00Z",
        },
      ],
    });
  });

  it("shows only permitted target states (excluding reserved) and enforces mandatory reason for decommissioning", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(mockPage([baseEquipment]));
    vi.mocked(managedAssetApi.transitionManagedAssetLifecycle).mockResolvedValue({
      id: baseEquipment.id,
      asset_type: "equipment",
      asset_tag: baseEquipment.asset_tag,
      serial_number: null,
      lifecycle_status: "decommissioned",
      created_at: baseEquipment.created_at,
    });

    const user = userEvent.setup();
    renderWithProviders(<EquipmentPage />, { user: FULL_EQUIPMENT_MANAGER });

    const changeBtn = await screen.findByRole("button", {
      name: "Change lifecycle state: srv-prod-01",
    });
    await user.click(changeBtn);

    const dialog = await screen.findByRole("dialog", {
      name: "Change lifecycle state for srv-prod-01",
    });
    const select = within(dialog).getByLabelText("Target lifecycle state") as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);

    // For 'active', only 'maintenance' and 'decommissioned' are permitted; 'reserved' is never selectable
    expect(optionValues).toEqual(["maintenance", "decommissioned"]);
    expect(optionValues).not.toContain("reserved");

    // Select 'decommissioned' -> reason becomes mandatory
    await user.selectOptions(select, "decommissioned");
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm lifecycle transition for srv-prod-01" }),
    );

    // Must fail client-side validation without calling API when reason is blank
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "A reason is required when transitioning to Decommissioned.",
    );
    expect(managedAssetApi.transitionManagedAssetLifecycle).not.toHaveBeenCalled();

    // Enter reason and confirm
    const reasonInput = within(dialog).getByLabelText("Reason for lifecycle transition");
    await user.type(reasonInput, "End of hardware support lifecycle");
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm lifecycle transition for srv-prod-01" }),
    );

    await waitFor(() =>
      expect(managedAssetApi.transitionManagedAssetLifecycle).toHaveBeenCalledWith(
        baseEquipment.id,
        "decommissioned",
        "End of hardware support lifecycle",
      ),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "srv-prod-01 lifecycle state changed to Decommissioned.",
    );
  });

  it("displays existing 'reserved' records as read-only legacy state and preserves status during equipment editing", async () => {
    const reservedEq: Equipment = {
      ...baseEquipment,
      id: "eq-reserved",
      asset_tag: "SRV-LEGACY-01",
      hostname: "srv-legacy-01",
      lifecycle_status: "reserved",
    };
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(mockPage([reservedEq]));
    vi.mocked(equipmentApi.updateEquipment).mockResolvedValue({
      ...reservedEq,
      hostname: "srv-legacy-01-updated",
      version: 4,
    });

    const user = userEvent.setup();
    renderWithProviders(<EquipmentPage />, { user: FULL_EQUIPMENT_MANAGER });

    // Read-only legacy notice rendered in row
    expect(await screen.findByTestId("legacy-reserved-explanation-eq-reserved")).toHaveTextContent(
      "read-only until a separate migration policy is approved",
    );
    expect(
      screen.queryByRole("button", { name: "Change lifecycle state: srv-legacy-01" }),
    ).not.toBeInTheDocument();

    // Open Edit Equipment panel
    await user.click(screen.getByRole("button", { name: "Edit equipment: srv-legacy-01" }));
    const editPanel = await screen.findByRole("region", { name: "Edit equipment srv-legacy-01" });
    expect(within(editPanel).getByTestId("edit-reserved-legacy-warning")).toHaveTextContent(
      "Saving equipment details will preserve the existing status without converting it.",
    );

    const hostnameInput = within(editPanel).getByLabelText("Edit hostname");
    await user.clear(hostnameInput);
    await user.type(hostnameInput, "srv-legacy-01-updated");
    await user.click(within(editPanel).getByRole("button", { name: "Save Equipment Changes" }));

    await waitFor(() =>
      expect(equipmentApi.updateEquipment).toHaveBeenCalledWith(
        reservedEq.id,
        expect.objectContaining({ hostname: "srv-legacy-01-updated" }),
        reservedEq.version,
      ),
    );
    // Payload must NOT contain lifecycle_status so reserved is never silently converted
    const patchPayload = vi.mocked(equipmentApi.updateEquipment).mock.calls[0][1];
    expect(patchPayload).not.toHaveProperty("lifecycle_status");
  });

  it("renders lifecycle transition history and edit panel on EquipmentDetailPage", async () => {
    vi.mocked(equipmentApi.getEquipment).mockResolvedValue(baseEquipment);

    const user = userEvent.setup();
    renderWithProviders(<EquipmentDetailPage />, {
      route: `/equipment/${baseEquipment.id}`,
      path: "/equipment/:equipmentId",
      user: FULL_EQUIPMENT_MANAGER,
    });

    const historySection = await screen.findByRole("region", { name: "Lifecycle transition history" });
    expect(within(historySection).getByText("Lifecycle Transition History")).toBeInTheDocument();
    expect(await within(historySection).findByText(/Commissioned into production/)).toBeInTheDocument();
    expect(within(historySection).getAllByText(/admin@dcim\.local/).length).toBeGreaterThanOrEqual(1);

    // Open Edit Equipment panel from details page
    await user.click(screen.getByRole("button", { name: /Edit equipment/i }));
    expect(
      await screen.findByRole("region", { name: `Edit equipment ${baseEquipment.hostname}` }),
    ).toBeInTheDocument();
  });

  it("offers active, maintenance and removed (not decommissioned) for installed equipment", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(
      mockPage([{ ...baseEquipment, lifecycle_status: "installed" }]),
    );
    const user = userEvent.setup();
    renderWithProviders(<EquipmentPage />, { user: FULL_EQUIPMENT_MANAGER });
    await user.click(
      await screen.findByRole("button", { name: "Change lifecycle state: srv-prod-01" }),
    );
    const dialog = await screen.findByRole("dialog", {
      name: "Change lifecycle state for srv-prod-01",
    });
    const select = within(dialog).getByLabelText("Target lifecycle state") as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value).sort()).toEqual([
      "active",
      "maintenance",
      "removed",
    ]);
  });

  it("does not offer transitions when lifecycle settings fail to load", async () => {
    vi.mocked(managedAssetApi.getLifecycleSettings).mockRejectedValue(new Error("boom"));
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(mockPage([baseEquipment]));
    renderWithProviders(<EquipmentPage />, { user: FULL_EQUIPMENT_MANAGER });
    await screen.findByText("srv-prod-01");
    expect(
      screen.queryByRole("button", { name: "Change lifecycle state: srv-prod-01" }),
    ).not.toBeInTheDocument();
  });

  it("shows an error, not an empty-history message, when history fails to load, and recovers on retry", async () => {
    vi.mocked(equipmentApi.getEquipment).mockResolvedValue(baseEquipment);
    vi.mocked(managedAssetApi.getManagedAssetLifecycleHistory)
      .mockRejectedValueOnce(new Error("500"))
      .mockResolvedValueOnce({ items: [] });
    const user = userEvent.setup();
    renderWithProviders(<EquipmentDetailPage />, {
      route: `/equipment/${baseEquipment.id}`,
      path: "/equipment/:equipmentId",
      user: FULL_EQUIPMENT_MANAGER,
    });
    const section = await screen.findByRole("region", { name: "Lifecycle transition history" });
    expect(await within(section).findByRole("alert")).toHaveTextContent(/could not be loaded/i);
    expect(within(section).queryByText(/No lifecycle transitions recorded/)).not.toBeInTheDocument();

    await user.click(within(section).getByRole("button", { name: "Retry" }));
    expect(await within(section).findByText("No lifecycle transitions recorded yet.")).toBeInTheDocument();
  });

  it.each([
    { fromStatus: "planned", expectedTargets: ["installed", "removed"] },
    { fromStatus: "installed", expectedTargets: ["active", "maintenance", "removed"] },
    { fromStatus: "active", expectedTargets: ["maintenance", "decommissioned"] },
    { fromStatus: "maintenance", expectedTargets: ["active", "decommissioned"] },
    { fromStatus: "decommissioned", expectedTargets: ["removed"] },
  ])(
    "offers exact permitted target states for source state '$fromStatus'",
    async ({ fromStatus, expectedTargets }) => {
      vi.mocked(equipmentApi.listEquipment).mockResolvedValue(
        mockPage([{ ...baseEquipment, lifecycle_status: fromStatus }]),
      );
      const user = userEvent.setup();
      renderWithProviders(<EquipmentPage />, { user: FULL_EQUIPMENT_MANAGER });
      await user.click(
        await screen.findByRole("button", { name: "Change lifecycle state: srv-prod-01" }),
      );
      const dialog = await screen.findByRole("dialog", {
        name: "Change lifecycle state for srv-prod-01",
      });
      const select = within(dialog).getByLabelText("Target lifecycle state") as HTMLSelectElement;
      expect(Array.from(select.options).map((o) => o.value)).toEqual(expectedTargets);
    },
  );

  it("hides transition actions for terminal 'removed' state", async () => {
    vi.mocked(equipmentApi.listEquipment).mockResolvedValue(
      mockPage([{ ...baseEquipment, lifecycle_status: "removed" }]),
    );
    renderWithProviders(<EquipmentPage />, { user: FULL_EQUIPMENT_MANAGER });
    await screen.findByText("srv-prod-01");
    expect(
      screen.queryByRole("button", { name: "Change lifecycle state: srv-prod-01" }),
    ).not.toBeInTheDocument();
  });

  it("preserves unsaved dirty metadata edits across equipment refetch, lifecycle transition, and query invalidation, and resets when switching equipment", async () => {
    let currentEq: Equipment = { ...baseEquipment };
    vi.mocked(equipmentApi.getEquipment).mockImplementation(async () => currentEq);
    vi.mocked(managedAssetApi.transitionManagedAssetLifecycle).mockImplementation(
      async (_id, toStatus) => {
        currentEq = {
          ...currentEq,
          lifecycle_status: toStatus,
          notes: "Server-side background note update",
          version: currentEq.version + 1,
        };
        return {
          id: currentEq.id,
          asset_type: "equipment",
          asset_tag: currentEq.asset_tag,
          serial_number: null,
          lifecycle_status: toStatus,
          created_at: currentEq.created_at,
        };
      },
    );

    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<EquipmentDetailPage />, {
      route: `/equipment/${baseEquipment.id}`,
      path: "/equipment/:equipmentId",
      user: FULL_EQUIPMENT_MANAGER,
    });

    await user.click(await screen.findByRole("button", { name: /Edit equipment/i }));
    const editPanel = await screen.findByRole("region", {
      name: `Edit equipment ${baseEquipment.hostname}`,
    });

    const hostnameInput = within(editPanel).getByLabelText("Edit hostname") as HTMLInputElement;
    const ownerInput = within(editPanel).getByLabelText("Edit owner") as HTMLInputElement;
    const notesInput = within(editPanel).getByLabelText("Edit notes") as HTMLTextAreaElement;

    // Make hostname and owner dirty while leaving notes clean
    await user.clear(hostnameInput);
    await user.type(hostnameInput, "srv-dirty-unsaved");
    await user.clear(ownerInput);
    await user.type(ownerInput, "Dirty Owner Team");

    // 1. Trigger a lifecycle transition inside the edit panel (which invalidates and refetches equipment)
    await user.click(
      await within(editPanel).findByRole("button", { name: "Change lifecycle state: srv-prod-01" }),
    );
    const dialog = await within(editPanel).findByRole("dialog", {
      name: "Change lifecycle state for srv-prod-01",
    });
    await user.selectOptions(within(dialog).getByLabelText("Target lifecycle state"), "maintenance");
    await user.click(
      within(dialog).getByRole("button", { name: "Confirm lifecycle transition for srv-prod-01" }),
    );

    await waitFor(() =>
      expect(managedAssetApi.transitionManagedAssetLifecycle).toHaveBeenCalledWith(
        baseEquipment.id,
        "maintenance",
        undefined,
      ),
    );
    // Clean field synced with server update, dirty fields preserved!
    await waitFor(() => expect(notesInput.value).toBe("Server-side background note update"));
    expect(hostnameInput.value).toBe("srv-dirty-unsaved");
    expect(ownerInput.value).toBe("Dirty Owner Team");

    // 2. Trigger an explicit background query invalidation & refetch with another server-side change to a clean field
    currentEq = { ...currentEq, service: "Updated Background Service", version: currentEq.version + 1 };
    await queryClient.invalidateQueries({ queryKey: ["equipment", baseEquipment.id] });
    const serviceInput = within(editPanel).getByLabelText("Edit service") as HTMLInputElement;
    await waitFor(() => expect(serviceInput.value).toBe("Updated Background Service"));
    expect(hostnameInput.value).toBe("srv-dirty-unsaved");
    expect(ownerInput.value).toBe("Dirty Owner Team");

    // 3. Verify form resets when switching to a different equipment item (different equipment.id)
    currentEq = {
      ...baseEquipment,
      id: "eq-second",
      asset_tag: "SRV-200",
      hostname: "srv-second-node",
      owner: "Storage Team",
      service: "Object Storage",
      environment: "staging",
      notes: "Second node notes",
    };
    await queryClient.invalidateQueries({ queryKey: ["equipment", baseEquipment.id] });
    await waitFor(() =>
      expect((screen.getByLabelText("Edit hostname") as HTMLInputElement).value).toBe("srv-second-node"),
    );
    expect((screen.getByLabelText("Edit owner") as HTMLInputElement).value).toBe("Storage Team");
  });

  it("keeps EquipmentEditPanel and dirty edits mounted during failed background refetch and recovers on retry", async () => {
    vi.mocked(equipmentApi.getEquipment)
      .mockResolvedValueOnce(baseEquipment)
      .mockRejectedValueOnce(new Error("Temporary API outage"))
      .mockResolvedValueOnce({ ...baseEquipment, notes: "Recovered note", version: 4 });

    const user = userEvent.setup();
    const { queryClient } = renderWithProviders(<EquipmentDetailPage />, {
      route: `/equipment/${baseEquipment.id}`,
      path: "/equipment/:equipmentId",
      user: FULL_EQUIPMENT_MANAGER,
    });

    await user.click(await screen.findByRole("button", { name: /Edit equipment/i }));
    const editPanel = await screen.findByRole("region", {
      name: `Edit equipment ${baseEquipment.hostname}`,
    });
    const hostnameInput = within(editPanel).getByLabelText("Edit hostname") as HTMLInputElement;
    await user.clear(hostnameInput);
    await user.type(hostnameInput, "srv-dirty-refetch");

    await queryClient.invalidateQueries({ queryKey: ["equipment", baseEquipment.id] });
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not refresh equipment details: Temporary API outage");
    expect(hostnameInput.value).toBe("srv-dirty-refetch");

    await user.click(within(alert).getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByText(/Could not refresh equipment details/)).not.toBeInTheDocument());
    await waitFor(() =>
      expect((within(editPanel).getByLabelText("Edit notes") as HTMLTextAreaElement).value).toBe("Recovered note"),
    );
    expect(hostnameInput.value).toBe("srv-dirty-refetch");
  });
});
