import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AlarmRulesPage } from "@/features/telemetry/AlarmRulesPage";
import * as alarmRules from "@/features/telemetry/alarmRulesApi";
import * as integrations from "@/features/integrations/api";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/telemetry/alarmRulesApi");
vi.mock("@/features/integrations/api");

const MANAGER = {
  id: "u1", email: "m@example.com", full_name: "M", role_names: ["DCIM Manager"],
  permission_codes: ["alarm:read", "alarm:manage"],
};
// alarm:read without alarm:manage — may list rules but may not resolve or disable.
const VIEWER = { ...MANAGER, id: "u2", role_names: ["Viewer"], permission_codes: ["alarm:read"] };

const baseRule: alarmRules.AlarmRule = {
  id: "r-1", integration_id: "i-1", metric: "power_kw", rule_type: "threshold_high", threshold: 5,
  unit: null, registry_version: null, enabled: true, name: "PDU-A over-limit", severity: "warning", updated_at: "2026-10-08T10:00:00Z",
  unit_status: "unresolved", needs_attention: true, blocked_notified_at: "2026-10-08T09:00:00Z",
  unit_resolved_by: null, unit_resolved_at: null, unit_resolution_reason: null,
};
const resolvedRule: alarmRules.AlarmRule = { ...baseRule, id: "r-2", name: "Rack-B limit", unit: "kW", unit_status: "explicit", needs_attention: false, blocked_notified_at: null };

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(alarmRules.listAlarmRules).mockResolvedValue([baseRule]);
  vi.mocked(integrations.listIntegrations).mockResolvedValue([
    { id: "i-1", name: "Power monitoring", integration_type: "snmp", site_id: null, target_host: "host", target_port: null,
      config: {}, enabled: true, poll_interval_seconds: 60, last_poll_at: null, last_success_at: null, last_failure_at: null,
      consecutive_failures: 0, has_credential: false, assigned_collector_id: null, version: 1 },
  ]);
});

describe("AlarmRulesPage", () => {
  it("creates a threshold rule against a selected integration", async () => {
    vi.mocked(alarmRules.createAlarmRule).mockResolvedValue({ id: "new-rule" });
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: "Add alarm rule" }));
    await userEvent.type(await screen.findByLabelText("Rule name"), "Room temperature high");
    await userEvent.selectOptions(screen.getByLabelText("Integration"), "i-1");
    await userEvent.selectOptions(screen.getByLabelText("Metric"), "temperature_c");
    await userEvent.type(screen.getByLabelText("Threshold"), "30");
    await userEvent.click(screen.getByRole("button", { name: "Save rule" }));

    await waitFor(() => expect(alarmRules.createAlarmRule).toHaveBeenCalledWith({
      integration_id: "i-1", metric: "temperature_c", rule_type: "threshold_high", threshold: 30, name: "Room temperature high", severity: "warning",
    }));
  });

  it("sends the severity the operator selects and shows it in the rule table", async () => {
    vi.mocked(alarmRules.createAlarmRule).mockResolvedValue({ id: "new-rule" });
    vi.mocked(alarmRules.listAlarmRules).mockResolvedValue([{ ...baseRule, severity: "critical" }]);
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    expect(await screen.findByText("Critical")).toBeInTheDocument();
    await userEvent.click(await screen.findByRole("button", { name: "Add alarm rule" }));
    await userEvent.type(await screen.findByLabelText("Rule name"), "Inlet too hot");
    await userEvent.selectOptions(screen.getByLabelText("Metric"), "temperature_c");
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "critical");
    await userEvent.type(screen.getByLabelText("Threshold"), "32");
    await userEvent.click(screen.getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(alarmRules.createAlarmRule).toHaveBeenCalledWith(expect.objectContaining({ severity: "critical", name: "Inlet too hot" })));
  });

  it("prevents duplicate rule submissions while the create request is pending", async () => {
    let finishCreate!: (value: { id: string }) => void;
    vi.mocked(alarmRules.createAlarmRule).mockReturnValue(new Promise((resolve) => { finishCreate = resolve; }));
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: "Add alarm rule" }));
    await userEvent.type(await screen.findByLabelText("Rule name"), "Room temperature high");
    await userEvent.type(screen.getByLabelText("Threshold"), "30");
    const save = screen.getByRole("button", { name: "Save rule" });
    expect(screen.getByLabelText("Threshold")).toHaveAttribute("max", "9999999999.99999999");
    await userEvent.click(save);
    expect(save).toBeDisabled();
    await userEvent.click(save);
    expect(alarmRules.createAlarmRule).toHaveBeenCalledTimes(1);
    finishCreate({ id: "new-rule" });
  });

  it("resets availability to a threshold-compatible metric when the condition changes", async () => {
    vi.mocked(alarmRules.createAlarmRule).mockResolvedValue({ id: "new-rule" });
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: "Add alarm rule" }));
    await userEvent.type(await screen.findByLabelText("Rule name"), "Availability rule");
    await userEvent.selectOptions(screen.getByLabelText("Condition"), "availability_unavailable");
    expect(screen.getByLabelText("Metric")).toHaveValue("availability");
    await userEvent.selectOptions(screen.getByLabelText("Condition"), "threshold_high");
    expect(screen.getByLabelText("Metric")).toHaveValue("power_kw");
    await userEvent.type(screen.getByLabelText("Threshold"), "8");
    await userEvent.click(screen.getByRole("button", { name: "Save rule" }));
    await waitFor(() => expect(alarmRules.createAlarmRule).toHaveBeenCalledWith({
      integration_id: "i-1", metric: "power_kw", rule_type: "threshold_high", threshold: 8, name: "Availability rule", severity: "warning",
    }));
  });

  it("edits a rule's threshold with the current timestamp and reason", async () => {
    vi.mocked(alarmRules.listAlarmRules).mockResolvedValue([resolvedRule]);
    vi.mocked(alarmRules.editAlarmRule).mockResolvedValue({ ...resolvedRule, threshold: 9 });
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: "Edit rule" }));
    await userEvent.clear(screen.getByLabelText("Threshold"));
    await userEvent.type(screen.getByLabelText("Threshold"), "9");
    await userEvent.type(screen.getByLabelText("Reason for change"), "Revised operating limit");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(alarmRules.editAlarmRule).toHaveBeenCalledWith("r-2", {
      name: "Rack-B limit", threshold: 9, reason: "Revised operating limit", expected_updated_at: "2026-10-08T10:00:00Z",
    }));
  });

  it("explains that editing a disabled rule does not enable it", async () => {
    vi.mocked(alarmRules.listAlarmRules).mockResolvedValue([{ ...resolvedRule, enabled: false }]);
    renderWithProviders(<AlarmRulesPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: "Edit rule" }));
    expect(await screen.findByRole("status")).toHaveTextContent("This rule is disabled. Editing it will not re-enable it.");
  });

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
