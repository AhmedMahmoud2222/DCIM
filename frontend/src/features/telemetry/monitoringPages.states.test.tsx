import { onlineManager } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AlarmRulesPage } from "@/features/telemetry/AlarmRulesPage";
import * as alarmRules from "@/features/telemetry/alarmRulesApi";
import * as api from "@/features/telemetry/api";
import type { Alarm } from "@/features/telemetry/api";
import { EventsPage } from "@/features/telemetry/EventsPage";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/telemetry/api");
vi.mock("@/features/telemetry/alarmRulesApi");

const never = () => new Promise<never>(() => undefined);
const alarm = (id: string, subject_key: string, status: Alarm["status"], opened_at: string): Alarm => ({
  id, rule_id: "r", integration_id: "i", managed_asset_id: "a", subject_key, status, opened_at,
  acknowledged_at: null, cleared_at: null, last_value: 1, unit: "%", presentation_value: 1, presentation_unit: "%",
});
const READER = { id: "u1", email: "r@example.test", full_name: "Reader", role_names: ["Viewer"], permission_codes: ["alarm:read"] };
const EMPTY_ACTIVE = "No active or acknowledged alarm condition is currently reported.";

afterEach(() => onlineManager.setOnline(true));

describe("Events list states", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.listAlarmHistory).mockResolvedValue({ items: [], next_cursor: null });
  });

  const answer = (active: unknown, acknowledged: unknown = []) =>
    vi.mocked(api.getOpenAlarms).mockImplementation((async (status?: string) => {
      const value = status === "ACTIVE" ? active : acknowledged;
      if (value instanceof Error) throw value;
      return value;
    }) as typeof api.getOpenAlarms);

  it("announces loading and shows neither a table nor the empty message", async () => {
    vi.mocked(api.getOpenAlarms).mockReturnValue(never());
    renderWithProviders(<EventsPage />);

    expect(await screen.findByRole("status")).toHaveTextContent("Loading events…");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
    expect(screen.queryByText(EMPTY_ACTIVE)).not.toBeInTheDocument();
  });

  it("shows a failure as an alert with a retry that recovers, never as 'no alarm condition'", async () => {
    answer(new Error("Upstream timed out"));
    const user = userEvent.setup();
    renderWithProviders(<EventsPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load events.");
    expect(alert).toHaveTextContent("Upstream timed out");
    expect(screen.queryByText(EMPTY_ACTIVE)).not.toBeInTheDocument();

    answer([alarm("a1", "rack-1.power", "ACTIVE", "2026-09-27T10:00:00Z")]);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(within(await screen.findByRole("table", { name: "Events" })).getByText("rack-1.power")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  });

  it("shows Access restricted naming alarm:read for a 403", async () => {
    answer(new ApiError(403, "Forbidden", "no", null));
    renderWithProviders(<EventsPage />);

    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText("alarm:read")).toBeInTheDocument();
    expect(screen.queryByText(EMPTY_ACTIVE)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("does not report 'no alarm condition' for a first request paused offline", async () => {
    onlineManager.setOnline(false);
    renderWithProviders(<EventsPage />);

    expect(await screen.findByRole("status")).toHaveTextContent("Loading events…");
    expect(screen.queryByText(EMPTY_ACTIVE)).not.toBeInTheDocument();
    expect(api.getOpenAlarms).not.toHaveBeenCalled();
  });

  it("says no condition is reported only after both successful responses are empty", async () => {
    answer([], []);
    renderWithProviders(<EventsPage />);

    expect(await screen.findByText(EMPTY_ACTIVE)).toBeInTheDocument();
  });

  it("names its table and scroll region, scopes its headers and keeps the workflow chips", async () => {
    answer([alarm("a1", "rack-1.power", "ACTIVE", "2026-09-27T10:00:00Z")], [alarm("a2", "rack-2.temp", "ACKNOWLEDGED", "2026-09-27T09:00:00Z")]);
    renderWithProviders(<EventsPage />);

    const table = await screen.findByRole("table", { name: "Events" });
    expect(screen.getByRole("region", { name: "Events table" })).toHaveAttribute("tabindex", "0");
    for (const header of within(table).getAllByRole("columnheader")) expect(header).toHaveAttribute("scope", "col");
    expect(within(table).getAllByText("ACTIVE").length).toBeGreaterThan(0);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("lets a keyboard user select a row with Enter or Space and marks it current", async () => {
    answer([alarm("a1", "rack-1.power", "ACTIVE", "2026-09-27T10:00:00Z"), alarm("a2", "rack-2.temp", "ACTIVE", "2026-09-27T09:00:00Z")]);
    const user = userEvent.setup();
    renderWithProviders(<EventsPage />);

    const table = await screen.findByRole("table", { name: "Events" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveAttribute("tabindex", "0");
    expect(rows[0]).toHaveAttribute("aria-current", "true"); // the first row is selected by default

    rows[1].focus();
    await user.keyboard("{Enter}");
    expect(rows[1]).toHaveAttribute("aria-current", "true");
    expect(rows[0]).not.toHaveAttribute("aria-current");

    rows[0].focus();
    await user.keyboard(" ");
    expect(rows[0]).toHaveAttribute("aria-current", "true");
    expect(rows[1]).not.toHaveAttribute("aria-current");
  });
});

describe("Alarm Rules list states", () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  const rule: alarmRules.AlarmRule = {
    id: "r-1", integration_id: "i-1", metric: "power_kw", rule_type: "threshold_high", threshold: 5, unit: null, registry_version: null,
    enabled: true, name: "PDU-A over-limit", updated_at: "2026-10-08T10:00:00Z", unit_status: "unresolved", needs_attention: true,
    blocked_notified_at: "2026-10-08T09:00:00Z", unit_resolved_by: null, unit_resolved_at: null, unit_resolution_reason: null,
  };

  it("announces loading and does not say there are no rules", async () => {
    vi.mocked(alarmRules.listAlarmRules).mockReturnValue(never());
    renderWithProviders(<AlarmRulesPage />, { user: READER });

    expect(await screen.findByRole("status")).toHaveTextContent("Loading alarm rules…");
    expect(screen.queryByText(/No alarm rules/)).not.toBeInTheDocument();
  });

  it("shows a failure as an alert and a 403 as Access restricted, never as 'No alarm rules'", async () => {
    vi.mocked(alarmRules.listAlarmRules).mockRejectedValue(new Error("boom"));
    const { unmount } = renderWithProviders(<AlarmRulesPage />, { user: READER });
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load alarm rules.");
    expect(screen.queryByText(/No alarm rules/)).not.toBeInTheDocument();
    unmount();

    vi.mocked(alarmRules.listAlarmRules).mockRejectedValue(new ApiError(403, "Forbidden", "no", null));
    renderWithProviders(<AlarmRulesPage />, { user: READER });
    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.queryByText(/No alarm rules/)).not.toBeInTheDocument();
  });

  it("does not say there are no rules for a first request paused offline", async () => {
    onlineManager.setOnline(false);
    renderWithProviders(<AlarmRulesPage />, { user: READER });

    expect(await screen.findByRole("status")).toHaveTextContent("Loading alarm rules…");
    expect(screen.queryByText(/No alarm rules/)).not.toBeInTheDocument();
    expect(alarmRules.listAlarmRules).not.toHaveBeenCalled();
  });

  it("says no rules need attention only after a successful empty response", async () => {
    vi.mocked(alarmRules.listAlarmRules).mockResolvedValue([]);
    renderWithProviders(<AlarmRulesPage />, { user: READER });

    expect(await screen.findByText("No alarm rules currently need attention.")).toBeInTheDocument();
  });

  it("names its table, scopes headers, and lets a keyboard user select a rule", async () => {
    vi.mocked(alarmRules.listAlarmRules).mockResolvedValue([rule, { ...rule, id: "r-2", name: "Rack-B limit" }]);
    const user = userEvent.setup();
    renderWithProviders(<AlarmRulesPage />, { user: READER });

    const table = await screen.findByRole("table", { name: "Alarm rules" });
    expect(screen.getByRole("region", { name: "Alarm rules table" })).toHaveAttribute("tabindex", "0");
    for (const header of within(table).getAllByRole("columnheader")) expect(header).toHaveAttribute("scope", "col");
    const rows = within(table).getAllByRole("row").slice(1);
    rows[1].focus();
    await user.keyboard("{Enter}");
    expect(rows[1]).toHaveAttribute("aria-current", "true");
    expect(rows[0]).not.toHaveAttribute("aria-current");
  });

  it("still gives a user without alarm:read the existing restricted view and makes no request", async () => {
    renderWithProviders(<AlarmRulesPage />, { user: { ...READER, permission_codes: [] } });

    await waitFor(() => expect(alarmRules.listAlarmRules).not.toHaveBeenCalled());
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
});
