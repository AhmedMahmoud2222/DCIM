import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { EventsPage } from "@/features/telemetry/EventsPage";
import * as api from "@/features/telemetry/api";
import type { Alarm } from "@/features/telemetry/api";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/telemetry/api");

const activeAlarm: Alarm = {
  id: "alarm-1",
  rule_id: "rule-1",
  integration_id: "integration-1",
  managed_asset_id: "asset-1",
  subject_key: "rack-3.power.utilization",
  status: "ACTIVE",
  opened_at: "2026-09-27T10:00:00Z",
  acknowledged_at: null,
  cleared_at: null,
  last_value: 92,
};

const acknowledgedAlarm: Alarm = {
  ...activeAlarm,
  id: "alarm-2",
  status: "ACKNOWLEDGED",
  acknowledged_at: "2026-09-27T10:05:00Z",
  subject_key: "rack-4.temperature",
};

describe("EventsPage", () => {
  beforeEach(() => {
    vi.mocked(api.getOpenAlarms).mockImplementation(async (status?: "ACTIVE" | "ACKNOWLEDGED") =>
      status === "ACTIVE" ? [activeAlarm] : status === "ACKNOWLEDGED" ? [acknowledgedAlarm] : [],
    );
    vi.mocked(api.listAlarmHistory).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(api.acknowledgeAlarm).mockResolvedValue({ ...activeAlarm, status: "ACKNOWLEDGED" });
  });

  it("renders active and acknowledged alarms in the active view by default", async () => {
    renderWithProviders(<EventsPage />);

    const table = await screen.findByRole("table");
    expect(within(table).getByText("rack-3.power.utilization")).toBeInTheDocument();
    expect(within(table).getByText("rack-4.temperature")).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Active/ })).toHaveAttribute("aria-selected", "true");
  });

  it("filters visible events by the search box", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EventsPage />);
    const table = await screen.findByRole("table");
    within(table).getByText("rack-3.power.utilization");

    await user.type(screen.getByLabelText("Search events"), "rack-4");

    await waitFor(() => expect(within(screen.getByRole("table")).queryByText("rack-3.power.utilization")).not.toBeInTheDocument());
    expect(within(screen.getByRole("table")).getByText("rack-4.temperature")).toBeInTheDocument();
  });

  it("switches to the history view and queries listAlarmHistory with the selected status filter", async () => {
    const user = userEvent.setup();
    vi.mocked(api.listAlarmHistory).mockResolvedValue({ items: [{ ...activeAlarm, status: "CLEARED", cleared_at: "2026-09-27T11:00:00Z" }], next_cursor: null });
    renderWithProviders(<EventsPage />);
    await screen.findByRole("table");

    await user.click(screen.getByRole("tab", { name: "History" }));
    await user.selectOptions(screen.getByLabelText("Alarm status"), "CLEARED");

    await waitFor(() => expect(api.listAlarmHistory).toHaveBeenCalledWith(expect.objectContaining({ status: "CLEARED", limit: 50 })));
  });

  it("acknowledges the selected active event", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EventsPage />);
    const table = await screen.findByRole("table");

    const row = within(table).getByText("rack-3.power.utilization").closest("tr")!;
    await user.click(row);
    const detail = screen.getByText("Event detail").closest("aside")!;
    await user.click(within(detail).getByRole("button", { name: "Acknowledge active event" }));

    await waitFor(() => expect(api.acknowledgeAlarm).toHaveBeenCalledWith("alarm-1", expect.anything()));
  });

  it("shows an empty state when there are no active events", async () => {
    vi.mocked(api.getOpenAlarms).mockResolvedValue([]);
    renderWithProviders(<EventsPage />);

    expect(await screen.findByText(/No active or acknowledged alarm condition/)).toBeInTheDocument();
  });
});
