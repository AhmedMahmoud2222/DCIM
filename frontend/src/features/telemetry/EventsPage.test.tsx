import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useLocation } from "react-router-dom";

import { getDashboardExceptions } from "@/features/power/api";
import { EventsPage } from "@/features/telemetry/EventsPage";
import * as api from "@/features/telemetry/api";
import type { Alarm } from "@/features/telemetry/api";
import type { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { DashboardExceptionItem } from "@/types";

vi.mock("@/features/telemetry/api");
vi.mock("@/features/power/api");

const activeAlarm: Alarm = {
  id: "alarm-1",
  rule_id: "rule-1",
  integration_id: "integration-1",
  managed_asset_id: "asset-1",
  subject_key: "rack-3.power.utilization",
  status: "ACTIVE",
  severity: "warning",
  opened_at: "2026-09-27T10:00:00Z",
  acknowledged_at: null,
  cleared_at: null,
  last_value: 92,
  unit: "%",
  presentation_value: 92,
  presentation_unit: "%",
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

  it("shows each alarm's workflow state as text with an icon, and ACTIVE never looks like Critical", async () => {
    renderWithProviders(<EventsPage />);

    const active = (await screen.findAllByText("ACTIVE"))[0].parentElement!;
    expect(active.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(active.className).not.toMatch(/danger|red-/);
    expect(active).toHaveAttribute("title", expect.stringMatching(/does not indicate severity/i));
    expect(screen.getAllByText("ACKNOWLEDGED").length).toBeGreaterThan(0);
  });

  it("shows backend severity as Critical / Warning / Info text with a distinct icon, apart from workflow state", async () => {
    const critical: Alarm = { ...activeAlarm, id: "alarm-c", severity: "critical", subject_key: "rack-9.inlet" };
    const info: Alarm = { ...acknowledgedAlarm, id: "alarm-i", severity: "info", subject_key: "rack-8.humidity" };
    vi.mocked(api.getOpenAlarms).mockImplementation(async (status?: "ACTIVE" | "ACKNOWLEDGED") =>
      status === "ACTIVE" ? [critical, activeAlarm] : status === "ACKNOWLEDGED" ? [info] : [],
    );
    renderWithProviders(<EventsPage />);
    const table = await screen.findByRole("table");
    await within(table).findByText("rack-9.inlet");
    expect(screen.getByRole("columnheader", { name: "Severity" })).toBeInTheDocument();
    const shapes = ["Critical", "Warning", "Info"].map((label) => {
      const chip = within(table).getByText(label).parentElement!;
      expect(chip.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
      return chip.querySelector("svg")!.innerHTML;
    });
    expect(new Set(shapes).size).toBe(3);
    // The ACTIVE alarms in rows 1 and 2 carry different severities: workflow state does not decide severity.
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("Critical")).toBeInTheDocument();
    expect(within(rows[0]).getByText("ACTIVE")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Warning")).toBeInTheDocument();
    expect(within(rows[1]).getByText("ACTIVE")).toBeInTheDocument();
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
  it("renders canonical availability as 100% and converted temperature in its presentation unit", async () => {
    vi.mocked(api.getOpenAlarms).mockImplementation(async (status) => status === "ACTIVE" ? [
      { ...activeAlarm, last_value: 1, unit: "1", presentation_value: 100, presentation_unit: "%" },
      { ...acknowledgedAlarm, last_value: 77, unit: "degF", presentation_value: 25, presentation_unit: "degC" },
    ] : []);
    renderWithProviders(<EventsPage />);
    const table = await screen.findByRole("table");
    expect(within(table).getByText("100%")).toBeInTheDocument();
    expect(within(table).getByText("25 degC")).toBeInTheDocument();
    expect(within(table).queryByText("77")).not.toBeInTheDocument();
    expect(screen.getByText("last value 100%")).toBeInTheDocument();
  });

  it("exports presented value and unit alongside retained storage fields", async () => {
    vi.mocked(api.getOpenAlarms).mockImplementation(async (status) => status === "ACTIVE" ? [
      { ...activeAlarm, last_value: 1, unit: "1", presentation_value: 100, presentation_unit: "%" },
      { ...acknowledgedAlarm, last_value: 77, unit: "degF", presentation_value: 25, presentation_unit: "degC" },
    ] : []);
    const user = userEvent.setup();
    renderWithProviders(<EventsPage />);
    await screen.findByRole("table");
    let csv = "";
    const OriginalBlob = globalThis.Blob;
    vi.stubGlobal("Blob", class extends OriginalBlob {
      constructor(parts: BlobPart[], options?: BlobPropertyBag) {
        super(parts, options);
        csv = parts.join("");
      }
    });
    const originalUrl = globalThis.URL;
    vi.stubGlobal("URL", class extends originalUrl {
      static createObjectURL = vi.fn(() => "blob:test");
      static revokeObjectURL = vi.fn();
    });
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    try {
      await user.click(screen.getByRole("button", { name: "Export current results CSV" }));
      expect(csv.split("\n")[0]).toContain("presentation_value,presentation_unit,last_value,unit");
      expect(csv).toContain('"100","%","1","1"');
      expect(csv).toContain('"25","degC","77","degF"');
      expect(click).toHaveBeenCalledOnce();
    } finally {
      click.mockRestore();
      vi.unstubAllGlobals();
    }
  });

});

describe("EventsPage Infrastructure tab", () => {
  const INFRA_USER: CurrentUser = { id: "u", email: "u@example.test", full_name: "U", role_names: [], permission_codes: ["alarm:read", "dashboard:read"] };
  const exceptions: DashboardExceptionItem[] = [
    { code: "POWER_PATH_MISSING", severity: "info", object_type: "equipment", object_id: "eq-3", message: "No power path" },
    { code: "NODE_OVERLOADED", severity: "critical", object_type: "power_node", object_id: "pn-1", message: "PDU A over capacity" },
    { code: "REDUNDANCY_DEGRADED", severity: "warning", object_type: "equipment", object_id: "eq-9", message: "Single feed" },
  ];

  function LocationProbe() {
    const location = useLocation();
    return <output data-testid="location">{`${location.pathname}${location.search}`}</output>;
  }

  beforeEach(() => {
    vi.mocked(api.getOpenAlarms).mockImplementation(async (status?: "ACTIVE" | "ACKNOWLEDGED") => (status === "ACTIVE" ? [activeAlarm] : []));
    vi.mocked(api.listAlarmHistory).mockResolvedValue({ items: [], next_cursor: null });
    vi.mocked(getDashboardExceptions).mockResolvedValue(exceptions);
  });

  it("opens from ?view=infrastructure and lists every exception, most severe first, with its drill-down", async () => {
    renderWithProviders(<EventsPage />, { route: "/events?view=infrastructure", user: INFRA_USER });

    expect(screen.getByRole("tab", { name: "Infrastructure" })).toHaveAttribute("aria-selected", "true");
    const table = await screen.findByRole("table", { name: "Infrastructure exceptions" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[1].textContent)).toEqual(["NODE_OVERLOADED", "REDUNDANCY_DEGRADED", "POWER_PATH_MISSING"]);
    expect(within(rows[0]).getByText("Critical")).toBeInTheDocument();
    expect(within(rows[0]).getByText("Power node")).toBeInTheDocument();
    expect(within(rows[0]).getByText("PDU A over capacity")).toBeInTheDocument();
    expect(within(rows[0]).getByRole("link", { name: /Inspect NODE_OVERLOADED/ })).toHaveAttribute("href", "/power");
    expect(within(rows[1]).getByRole("link", { name: /Inspect REDUNDANCY_DEGRADED/ })).toHaveAttribute("href", "/equipment/eq-9");
    // Exceptions are not alarms: no workflow status, and the alarm export is off on this tab.
    expect(table).not.toHaveTextContent(/ACTIVE|ACKNOWLEDGED/);
    expect(screen.getByRole("button", { name: "Export current results CSV" })).toBeDisabled();
    expect(screen.queryByLabelText("Search events")).not.toBeVisible();
  });

  it("filters by severity with counts from the full list", async () => {
    const user = userEvent.setup();
    renderWithProviders(<EventsPage />, { route: "/events?view=infrastructure", user: INFRA_USER });
    const table = await screen.findByRole("table", { name: "Infrastructure exceptions" });

    expect(screen.getByRole("radio", { name: "All (3)" })).toBeChecked();
    await user.click(screen.getByRole("radio", { name: "Warning (1)" }));
    expect(within(table).getAllByRole("row").slice(1)).toHaveLength(1);
    expect(within(table).getByText("REDUNDANCY_DEGRADED")).toBeInTheDocument();
  });

  it("writes the tab into the URL and keeps the existing tabs working", async () => {
    const user = userEvent.setup();
    renderWithProviders(
      <>
        <EventsPage />
        <LocationProbe />
      </>,
      { route: "/events", user: INFRA_USER },
    );
    await screen.findByRole("table", { name: "Events" });
    await user.click(screen.getByRole("tab", { name: "Infrastructure" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/events?view=infrastructure");
    await screen.findByRole("table", { name: "Infrastructure exceptions" });

    await user.click(screen.getByRole("tab", { name: /Active/ }));
    expect(screen.getByTestId("location")).toHaveTextContent(/^\/events$/);
    expect(await screen.findByRole("table", { name: "Events" })).toBeInTheDocument();
    expect(screen.queryByTestId("infrastructure-view")).not.toBeInTheDocument();
  });

  it("is Access restricted without a request when dashboard:read is missing", async () => {
    renderWithProviders(<EventsPage />, { route: "/events?view=infrastructure", user: { ...INFRA_USER, permission_codes: ["alarm:read"] } });

    expect(await screen.findByRole("heading", { name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText("dashboard:read")).toBeInTheDocument();
    expect(getDashboardExceptions).not.toHaveBeenCalled();
  });

  it("shows an error with a retry, and an empty message only after a successful empty response", async () => {
    vi.mocked(getDashboardExceptions).mockRejectedValue(new Error("Upstream timed out"));
    const user = userEvent.setup();
    renderWithProviders(<EventsPage />, { route: "/events?view=infrastructure", user: INFRA_USER });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Upstream timed out");
    expect(screen.queryByTestId("infrastructure-empty")).not.toBeInTheDocument();

    vi.mocked(getDashboardExceptions).mockResolvedValue([]);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByTestId("infrastructure-empty")).toHaveTextContent("No capacity or topology exceptions detected.");
  });

  it("shows a loading status while the request is in flight", async () => {
    vi.mocked(getDashboardExceptions).mockReturnValue(new Promise(() => undefined));
    renderWithProviders(<EventsPage />, { route: "/events?view=infrastructure", user: INFRA_USER });

    await waitFor(() => expect(screen.getByTestId("infrastructure-view").querySelector("[aria-busy='true'], [role='status']")).not.toBeNull());
    expect(screen.queryByTestId("infrastructure-empty")).not.toBeInTheDocument();
  });
});

describe("EventsPage detail aside: managed asset reference", () => {
  const ID = "9f8e7d6c-0000-4000-8000-000000000000";
  const ASSET_READER: CurrentUser = { id: "u", email: "u@example.test", full_name: "U", role_names: [], permission_codes: ["alarm:read", "equipment:read", "rack:read"] };
  const aside = () => screen.getByText("Event detail").closest("aside") as HTMLElement;

  async function renderWith(extra: Partial<Alarm>, user: CurrentUser = ASSET_READER) {
    vi.mocked(api.getOpenAlarms).mockImplementation(async (status?: "ACTIVE" | "ACKNOWLEDGED") =>
      status === "ACTIVE" ? [{ ...activeAlarm, managed_asset_id: ID, ...extra }] : [],
    );
    vi.mocked(api.listAlarmHistory).mockResolvedValue({ items: [], next_cursor: null });
    renderWithProviders(<EventsPage />, { user });
    await screen.findByText("Event detail");
  }

  it("links equipment to its equipment page", async () => {
    await renderWith({ managed_asset_type: "equipment" });
    expect(within(aside()).getByRole("link", { name: "Open equipment for rack-3.power.utilization" })).toHaveAttribute("href", `/equipment/${ID}`);
  });

  it("links a rack to its rack page", async () => {
    await renderWith({ managed_asset_type: "rack" });
    expect(within(aside()).getByRole("link", { name: "Open rack for rack-3.power.utilization" })).toHaveAttribute("href", `/racks/${ID}`);
    expect(within(aside()).queryByRole("link", { name: /equipment/i })).not.toBeInTheDocument();
  });

  it.each([
    ["pdu", "PDU asset 9f8e7d6c"],
    [null, "Asset 9f8e7d6c"],
    ["cable", "Cable asset 9f8e7d6c"],
  ])("shows a non-link reference for type %s", async (type, label) => {
    await renderWith({ managed_asset_type: type });
    expect(within(aside()).getByText(`Affected asset: ${label}`)).toBeInTheDocument();
    expect(within(aside()).queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows a non-link reference when the user lacks the page's permission", async () => {
    await renderWith({ managed_asset_type: "equipment" }, { ...ASSET_READER, permission_codes: ["alarm:read", "rack:read"] });
    expect(within(aside()).getByText("Affected asset: Equipment asset 9f8e7d6c")).toBeInTheDocument();
    expect(within(aside()).queryByRole("link")).not.toBeInTheDocument();
  });

  it("shows nothing for an alarm without a managed asset", async () => {
    await renderWith({ managed_asset_id: null, managed_asset_type: null });
    expect(within(aside()).queryByRole("link")).not.toBeInTheDocument();
    expect(within(aside()).queryByText(/Affected asset/)).not.toBeInTheDocument();
  });
});
