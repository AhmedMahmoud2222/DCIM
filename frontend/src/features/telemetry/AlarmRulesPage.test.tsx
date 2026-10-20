import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AlarmRulesPage } from "@/features/telemetry/AlarmRulesPage";
import * as alarmRules from "@/features/telemetry/alarmRulesApi";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/telemetry/alarmRulesApi");

const MANAGER = {
  id: "u1", email: "m@example.com", full_name: "M", role_names: ["DCIM Manager"],
  permission_codes: ["alarm:read", "alarm:manage"],
};
// alarm:read without alarm:manage — may list rules but may not resolve or disable.
const VIEWER = { ...MANAGER, id: "u2", role_names: ["Viewer"], permission_codes: ["alarm:read"] };

const baseRule: alarmRules.AlarmRule = {
  id: "r-1", integration_id: "i-1", metric: "power_kw", rule_type: "threshold_high", threshold: 5,
  unit: null, registry_version: null, enabled: true, name: "PDU-A over-limit", updated_at: "2026-10-08T10:00:00Z",
  unit_status: "unresolved", needs_attention: true, blocked_notified_at: "2026-10-08T09:00:00Z",
  unit_resolved_by: null, unit_resolved_at: null, unit_resolution_reason: null,
};
const resolvedRule: alarmRules.AlarmRule = { ...baseRule, id: "r-2", name: "Rack-B limit", unit: "kW", unit_status: "explicit", needs_attention: false, blocked_notified_at: null };

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(alarmRules.listAlarmRules).mockResolvedValue([baseRule]);
});

describe("AlarmRulesPage", () => {
  it("lists rules needing attention and shows the blocked reason", async () => {
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    // The name renders in both the list row and the detail heading — assert on the list row.
    expect((await screen.findAllByText("PDU-A over-limit")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Needs unit").length).toBeGreaterThan(0);
    expect(alarmRules.listAlarmRules).toHaveBeenCalledWith({ needsAttention: true });
    // Stored unit is absent, so evaluation is explicitly blocked — never silently inferred.
    expect(screen.getByText("Stored unit")).toBeInTheDocument();
  });

  it("resolves a rule's unit, sending expected_updated_at for optimistic concurrency", async () => {
    vi.mocked(alarmRules.resolveAlarmRuleUnit).mockResolvedValue({
      ...baseRule, unit: "kW", unit_status: "explicit", needs_attention: false,
      retained_blocked_window_readings: 3, blocked_window_count_complete: false, retroactive_evaluation: false,
    });
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: "Resolve threshold unit" }));
    await userEvent.type(screen.getByLabelText("Threshold unit"), "kW");
    await userEvent.type(screen.getByLabelText("Reason for resolution"), "Contract unit is kW");
    await userEvent.click(screen.getByRole("button", { name: "Save unit" }));

    await waitFor(() =>
      expect(alarmRules.resolveAlarmRuleUnit).toHaveBeenCalledWith("r-1", {
        unit: "kW", reason: "Contract unit is kW", expected_updated_at: "2026-10-08T10:00:00Z",
      }),
    );
    expect(alarmRules.resolveAlarmRuleUnit).toHaveBeenCalledTimes(1);
    // The resolve flow must not fall back to a broad list reload; it re-queries only the
    // needs_attention rules with the operator's current filter.
    expect(alarmRules.listAlarmRules).toHaveBeenCalledWith({ needsAttention: true });
  });

  it("disables a rule with a required reason", async () => {
    vi.mocked(alarmRules.listAlarmRules).mockResolvedValue([resolvedRule, baseRule]);
    vi.mocked(alarmRules.disableAlarmRule).mockResolvedValue({ ...baseRule, enabled: false, needs_attention: false });
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    // Select the unresolved rule (not the first/resolved one) then disable it.
    await userEvent.click(await screen.findByText("PDU-A over-limit"));
    await userEvent.click(screen.getByRole("button", { name: "Disable rule" }));
    await userEvent.type(screen.getByLabelText("Reason for disabling"), "Superseded by a versioned rule");
    await userEvent.click(screen.getByRole("button", { name: "Disable rule" }));

    await waitFor(() =>
      expect(alarmRules.disableAlarmRule).toHaveBeenCalledWith("r-1", {
        reason: "Superseded by a versioned rule", expected_updated_at: "2026-10-08T10:00:00Z",
      }),
    );
  });

  it("surfaces a backend conflict instead of silently retrying", async () => {
    vi.mocked(alarmRules.disableAlarmRule).mockRejectedValue(new ApiError(409, "Conflict", "Rule was modified concurrently.", null));
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    await screen.findAllByText("PDU-A over-limit");
    await userEvent.click(screen.getByRole("button", { name: "Disable rule" }));
    await userEvent.type(screen.getByLabelText("Reason for disabling"), "test");
    await userEvent.click(screen.getByRole("button", { name: "Disable rule" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Conflict: Rule was modified concurrently.");
  });

  it("hides resolve/disable actions from a read-only operator", async () => {
    renderWithProviders(<AlarmRulesPage />, { user: VIEWER });
    await screen.findAllByText("PDU-A over-limit");
    // The rule still renders in the detail panel, but mutation actions are absent.
    expect(screen.getByText("Stored unit")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Resolve threshold unit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable rule" })).not.toBeInTheDocument();
  });

  it("blocks the view entirely without alarm:read", async () => {
    renderWithProviders(<AlarmRulesPage />, { user: { ...VIEWER, permission_codes: [] } });
    expect(await screen.findByText(/do not have permission to view alarm rules/i)).toBeInTheDocument();
    expect(alarmRules.listAlarmRules).not.toHaveBeenCalled();
  });
});
