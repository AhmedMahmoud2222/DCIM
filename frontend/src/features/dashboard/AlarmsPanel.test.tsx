import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AlarmsPanel } from "@/features/dashboard/AlarmsPanel";
import { AlarmOverview, getAlarmOverview } from "@/features/telemetry/api";
import type { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/telemetry/api");

const alarm = (id: string, extra: Record<string, unknown> = {}) => ({
  id, rule_id: "rule", integration_id: "integration", managed_asset_id: null, subject_key: `subject-${id}`, status: "ACTIVE", severity: "critical",
  opened_at: "2026-01-01T00:00:00Z", acknowledged_at: null, cleared_at: null, last_value: 1, unit: "1", presentation_value: 1, presentation_unit: "%", ...extra,
});

const userWith = (...permission_codes: string[]): CurrentUser => ({ id: "u", email: "u@example.test", full_name: "U", role_names: [], permission_codes });
const ASSET_READER = userWith("alarm:read", "equipment:read", "rack:read");

const COUNTS = { critical: 12, warning: 30, info: 4, total: 46 };
const overview = (items: unknown[], extra: Partial<AlarmOverview> = {}): AlarmOverview =>
  ({
    scope: { include_acknowledged: false, severity: null },
    counts: COUNTS,
    items,
    matching_total: items.length,
    limit: 7,
    truncated: false,
    as_of: "2026-11-18T10:00:00Z",
    ...extra,
  }) as AlarmOverview;

const seven = Array.from({ length: 7 }, (_, i) => alarm(`a${i}`));
const panel = () => document.querySelector('[data-section="alarms"]') as HTMLElement;
/** Counter buttons are named "<Severity> <count>". A name function, not a RegExp built from a string. */
const counter = (name: string) =>
  screen.getByRole("button", { name: (accessibleName) => accessibleName.startsWith(name) && /^\s*\d+$/.test(accessibleName.slice(name.length)) });
const lastCall = () => vi.mocked(getAlarmOverview).mock.lastCall?.[0];

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getAlarmOverview).mockResolvedValue(overview(seven, { matching_total: 46, truncated: true }));
});

describe("Active alarms panel", () => {
  it("shows the server's severity counts, not the length of the list, and the truncation from matching_total", async () => {
    renderWithProviders(<AlarmsPanel access="granted" />);

    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));
    expect(counter("Critical")).toHaveTextContent("12");
    expect(counter("Warning")).toHaveTextContent("30");
    expect(counter("Info")).toHaveTextContent("4");
    expect(screen.getByTestId("alarms-scope")).toHaveTextContent("46 active in total.");
    expect(within(panel()).getAllByRole("listitem")).toHaveLength(7);
    expect(screen.getByTestId("alarms-truncation")).toHaveTextContent("Showing latest 7 of 46.");
  });

  it("asks for all severities and active alarms only by default", async () => {
    renderWithProviders(<AlarmsPanel access="granted" />);

    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));
    expect(lastCall()).toEqual({ severity: null, includeAcknowledged: false });
    expect(screen.getByRole("radio", { name: "All" })).toBeChecked();
    expect(screen.getByRole("switch", { name: "Include acknowledged" })).not.toBeChecked();
    for (const name of ["Critical", "Warning", "Info"]) expect(counter(name)).toHaveAttribute("aria-pressed", "false");
  });

  it("treats a counter and the selector as one control: a counter selects its severity, pressing it again returns to All", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AlarmsPanel access="granted" />);
    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));

    await user.click(counter("Critical"));
    expect(counter("Critical")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("radio", { name: "Critical" })).toBeChecked();
    await waitFor(() => expect(lastCall()).toEqual({ severity: "critical", includeAcknowledged: false }));

    await user.click(counter("Critical"));
    expect(counter("Critical")).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("radio", { name: "All" })).toBeChecked();

    await user.click(screen.getByRole("radio", { name: "Warning" }));
    expect(counter("Warning")).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(lastCall()).toEqual({ severity: "warning", includeAcknowledged: false }));
  });

  it("operates the counters from the keyboard", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AlarmsPanel access="granted" />);
    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));

    counter("Info").focus();
    await user.keyboard("{Enter}");
    expect(counter("Info")).toHaveAttribute("aria-pressed", "true");
    await user.keyboard(" ");
    expect(counter("Info")).toHaveAttribute("aria-pressed", "false");
  });

  it("toggles acknowledged alarms independently of the severity", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AlarmsPanel access="granted" />);
    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));

    await user.click(counter("Warning"));
    await user.click(screen.getByRole("switch", { name: "Include acknowledged" }));
    expect(screen.getByRole("switch", { name: "Include acknowledged" })).toBeChecked();
    await waitFor(() => expect(lastCall()).toEqual({ severity: "warning", includeAcknowledged: true }));
    expect(counter("Warning")).toHaveAttribute("aria-pressed", "true");

    await user.click(counter("Warning"));
    await waitFor(() => expect(lastCall()).toEqual({ severity: null, includeAcknowledged: true }));
    expect(screen.getByRole("switch", { name: "Include acknowledged" })).toBeChecked();
  });

  it("keeps the previous counts on screen while a new filter loads, so the counters do not flash", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AlarmsPanel access="granted" />);
    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));

    vi.mocked(getAlarmOverview).mockReturnValue(new Promise(() => undefined));
    await user.click(counter("Critical"));
    expect(panel()).toHaveAttribute("data-state", "ready");
    expect(counter("Critical")).toHaveTextContent("12");
    expect(within(panel()).getByText(/Updating/)).toBeInTheDocument();
  });

  it("lists exactly seven without a truncation note when seven match", async () => {
    vi.mocked(getAlarmOverview).mockResolvedValue(overview(seven, { matching_total: 7 }));
    renderWithProviders(<AlarmsPanel access="granted" />);

    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));
    expect(within(panel()).getAllByRole("listitem")).toHaveLength(7);
    expect(screen.queryByTestId("alarms-truncation")).not.toBeInTheDocument();
  });

  it("lists fewer than seven when fewer match", async () => {
    vi.mocked(getAlarmOverview).mockResolvedValue(overview(seven.slice(0, 3)));
    renderWithProviders(<AlarmsPanel access="granted" />);

    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));
    expect(within(panel()).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.queryByTestId("alarms-truncation")).not.toBeInTheDocument();
  });

  it("shows severity and workflow status as separate badges, with value, opened time and acknowledgement", async () => {
    vi.mocked(getAlarmOverview).mockResolvedValue(
      overview([
        alarm("t", { subject_key: "rack-1.temperature", severity: "warning", status: "ACKNOWLEDGED", acknowledged_at: "2026-01-02T00:00:00Z", presentation_value: 25, presentation_unit: "degC", managed_asset_id: "eq-9", managed_asset_type: "equipment" }),
      ]),
    );
    renderWithProviders(<AlarmsPanel access="granted" />, { user: ASSET_READER });

    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));
    const [row] = within(panel()).getAllByRole("listitem");
    expect(within(row).getByText("Warning")).toBeInTheDocument();
    expect(within(row).getByText("ACKNOWLEDGED")).toBeInTheDocument();
    expect(within(row).getByText("25 degC")).toBeInTheDocument();
    expect(row).toHaveTextContent(/Opened .*Acknowledged /);
    expect(within(row).getByRole("link", { name: "Open rack-1.temperature in Events" })).toHaveAttribute("href", "/events");
    expect(within(row).getByRole("link", { name: "Open equipment for rack-1.temperature" })).toHaveAttribute("href", "/equipment/eq-9");
  });

  it("renders an unknown severity as a neutral Unrecognised badge and counts it only in the total", async () => {
    vi.mocked(getAlarmOverview).mockResolvedValue(
      overview([alarm("x", { severity: "fatal" })], { counts: { critical: 1, warning: 0, info: 0, total: 3 } }),
    );
    renderWithProviders(<AlarmsPanel access="granted" />);

    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));
    const [row] = within(panel()).getAllByRole("listitem");
    expect(within(row).getByText("Unrecognised severity")).toBeInTheDocument();
    expect(within(row).queryByText("Critical")).not.toBeInTheDocument();
    expect(screen.getByTestId("alarms-scope")).toHaveTextContent("3 active in total, including 2 with an unrecognised severity.");
    expect(counter("Critical")).toHaveTextContent("1");
  });

  it("keeps the controls when a filter matches nothing, so the filter can be changed back", async () => {
    const user = userEvent.setup();
    renderWithProviders(<AlarmsPanel access="granted" />);
    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));

    vi.mocked(getAlarmOverview).mockResolvedValue(overview([], { counts: { critical: 0, warning: 30, info: 4, total: 34 } }));
    await user.click(counter("Critical"));
    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "empty"));
    expect(screen.getByTestId("alarms-empty")).toHaveTextContent("No Critical active alarms.");
    await user.click(screen.getByRole("radio", { name: "All" }));
    expect(counter("Critical")).toHaveAttribute("aria-pressed", "false");
  });

  it("is restricted without a request when access is denied", () => {
    renderWithProviders(<AlarmsPanel access="denied" />);
    expect(panel()).toHaveAttribute("data-state", "restricted");
    expect(getAlarmOverview).not.toHaveBeenCalled();
  });
});

describe("AlarmsPanel managed asset reference", () => {
  const ID = "1a2b3c4d-0000-4000-8000-000000000000";
  async function rowFor(extra: Record<string, unknown>, user: CurrentUser = ASSET_READER) {
    vi.mocked(getAlarmOverview).mockResolvedValue(overview([alarm("m", { subject_key: "pdu-7.load", managed_asset_id: ID, ...extra })]));
    renderWithProviders(<AlarmsPanel access="granted" />, { user });
    await waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));
    return within(panel()).getAllByRole("listitem")[0];
  }

  it("links equipment to its equipment page", async () => {
    const row = await rowFor({ managed_asset_type: "equipment" });
    expect(within(row).getByRole("link", { name: "Open equipment for pdu-7.load" })).toHaveAttribute("href", `/equipment/${ID}`);
  });

  it("links a rack to its rack page, not to equipment", async () => {
    const row = await rowFor({ managed_asset_type: "rack" });
    expect(within(row).getByRole("link", { name: "Open rack for pdu-7.load" })).toHaveAttribute("href", `/racks/${ID}`);
    expect(within(row).queryByRole("link", { name: /Open equipment/ })).not.toBeInTheDocument();
  });

  it.each([
    ["pdu", "PDU asset 1a2b3c4d"],
    [null, "Asset 1a2b3c4d"],
    ["garbage", "Asset 1a2b3c4d"],
  ])("shows a non-link reference for type %s", async (type, label) => {
    const row = await rowFor({ managed_asset_type: type });
    expect(within(row).getByText(label)).toBeInTheDocument();
    expect(within(row).getAllByRole("link")).toHaveLength(1);
  });

  it("shows a non-link reference when the user lacks the page's permission", async () => {
    const row = await rowFor({ managed_asset_type: "rack" }, userWith("alarm:read", "equipment:read"));
    expect(within(row).getByText("Rack asset 1a2b3c4d")).toBeInTheDocument();
    expect(within(row).getAllByRole("link")).toHaveLength(1);
  });

  it("shows nothing for an alarm without a managed asset", async () => {
    const row = await rowFor({ managed_asset_id: null, managed_asset_type: null });
    expect(within(row).getAllByRole("link")).toHaveLength(1);
    expect(row).not.toHaveTextContent(/asset/i);
  });
});
