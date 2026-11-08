import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/operations/api";
import { OperationsViews } from "@/features/operations/OperationsViews";
import { EventsPage } from "@/features/telemetry/EventsPage";
import * as telemetry from "@/features/telemetry/api";
import { ApiError } from "@/lib/apiClient";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/operations/api");
vi.mock("@/features/telemetry/api");

const MANAGER = {
  id: "u1", email: "m@example.com", full_name: "M", role_names: ["DCIM Manager"],
  permission_codes: ["alarm:read", "alarm:manage", "integration:read", "integration:manage", "collector:read"],
};
const VIEWER = { ...MANAGER, id: "u2", role_names: ["Viewer"], permission_codes: ["alarm:read", "integration:read", "collector:read"] };

const incident: api.Incident = {
  id: "inc-1", rule: "shared_power_cause", cause_type: "protection_device", cause_label: "BRK-1", confidence: "high",
  status: "open", site_id: "s1", opened_at: "2026-10-08T12:00:00Z", last_member_at: "2026-10-08T12:01:00Z", member_count: 2,
  ticket_count: 1, version: 3,
};
const detail: api.IncidentDetail = {
  ...incident, rationale: "2 alarm(s) downstream of BRK-1, which is tripped.", correlation_id: "incident:abc123",
  causation_id: "x", method_version: "1", resolved_at: null, all_sources_cleared: false,
  evidence: [
    { type: "protection_state", label: "BRK-1", state: "tripped" },
    { type: "alarm", subject_key: "srv-1.power" },
  ],
  members: [
    { member_type: "alarm", role: "symptom", source_time: "2026-10-08T12:00:30Z", alarm_id: "a1", alarm_status: "ACTIVE", alarm_subject: "srv-1.power", alarm_opened_at: "2026-10-08T12:00:30Z", alarm_cleared_at: null, transition_id: null, collector_name: null, transition_to_state: null },
    { member_type: "collector_transition", role: "cause", source_time: "2026-10-08T11:59:00Z", alarm_id: null, alarm_status: null, alarm_subject: null, alarm_opened_at: null, alarm_cleared_at: null, transition_id: "t1", collector_name: "edge-1", transition_to_state: "offline" },
  ],
  notifications: [
    { id: "d1", event_type: "incident.opened", status: "retry", attempts: 2, max_attempts: 5, next_attempt_at: "2026-10-08T12:05:00Z", failure_code: "HTTP_5XX", sent_at: null, channel_name: "ops-hook" },
    { id: "d2", event_type: "incident.opened", status: "failed", attempts: 5, max_attempts: 5, next_attempt_at: "2026-10-08T12:05:00Z", failure_code: "ATTEMPTS_EXHAUSTED", sent_at: null, channel_name: "pager" },
    { id: "d3", event_type: "incident.updated", status: "sent", attempts: 1, max_attempts: 5, next_attempt_at: "2026-10-08T12:05:00Z", failure_code: null, sent_at: "2026-10-08T12:02:00Z", channel_name: "chat" },
  ],
  tickets: [
    { id: "tk1", incident_id: "inc-1", connection_id: "c1", connection_name: "snow-prod", status: "synced", external_number: "INC0010042", external_state: "in_progress", attempts: 1, next_attempt_at: "2026-10-08T12:00:00Z", failure_code: null, last_synced_at: "2026-10-08T12:00:40Z" },
    { id: "tk2", incident_id: "inc-1", connection_id: "c2", connection_name: "snow-dr", status: "failed", external_number: null, external_state: "unknown", attempts: 5, next_attempt_at: "2026-10-08T12:00:00Z", failure_code: "AUTH_FAILED", last_synced_at: null },
  ],
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listIncidents).mockResolvedValue({ items: [incident], total: 1, limit: 100, offset: 0 });
  vi.mocked(api.getIncident).mockResolvedValue(detail);
  vi.mocked(api.listItsmConnections).mockResolvedValue([
    { id: "c3", name: "snow-new", provider: "servicenow", base_url: "https://x.service-now.com", username: "svc", has_password: true, enabled: true, auto_create: false, min_confidence: null, version: 1 },
  ]);
});

async function openIncident(user = MANAGER) {
  renderWithProviders(<OperationsViews view="incidents" />, { user });
  await userEvent.click(await screen.findByRole("button", { name: "BRK-1" }));
  return screen.findByRole("region", { name: "Incident detail" });
}

describe("incidents", () => {
  it("shows probable cause, evidence, source events, delivery state and the ITSM reference", async () => {
    const panel = await openIncident();
    expect(within(panel).getByTestId("rationale")).toHaveTextContent("downstream of BRK-1, which is tripped");
    expect(within(panel).getByText(/protection state: BRK-1 \(tripped\)/)).toBeInTheDocument();
    const sources = within(panel).getByRole("table", { name: "Source events" });
    expect(within(sources).getByText("srv-1.power")).toBeInTheDocument();
    expect(within(sources).getByText("Collector edge-1")).toBeInTheDocument();
    expect(within(panel).getByText(/Resolving the incident never clears or changes the source alarms/)).toBeInTheDocument();
    expect(within(panel).getByText("Provider unavailable (HTTP_5XX): retrying, attempt 2 of 5")).toBeInTheDocument();
    expect(within(panel).getByText("Failed (ATTEMPTS_EXHAUSTED): retries stopped")).toBeInTheDocument();
    expect(within(panel).getByText("Delivered")).toBeInTheDocument();
    const tickets = within(panel).getByRole("table", { name: "ITSM tickets" });
    expect(within(tickets).getByText("INC0010042")).toBeInTheDocument();
    expect(within(tickets).getByText("in progress")).toBeInTheDocument();
    expect(within(tickets).getByText("Failed (AUTH_FAILED): retries stopped")).toBeInTheDocument();
  });

  it("acknowledges and resolves with the incident version and retries failed work", async () => {
    vi.mocked(api.acknowledgeIncident).mockResolvedValue(incident);
    vi.mocked(api.resolveIncident).mockResolvedValue({ ...incident, status: "resolved" });
    vi.mocked(api.retryDelivery).mockResolvedValue({} as api.Delivery);
    vi.mocked(api.retryTicket).mockResolvedValue({} as api.Ticket);
    vi.mocked(api.createTicket).mockResolvedValue({} as api.Ticket);
    const panel = await openIncident();
    await userEvent.click(within(panel).getByRole("button", { name: "Acknowledge incident" }));
    await waitFor(() => expect(api.acknowledgeIncident).toHaveBeenCalledWith("inc-1", 3));
    await userEvent.click(within(panel).getByRole("button", { name: "Resolve incident" }));
    await waitFor(() => expect(api.resolveIncident).toHaveBeenCalledWith("inc-1", 3));
    await userEvent.click(within(panel).getByRole("button", { name: "Retry delivery" }));
    await waitFor(() => expect(api.retryDelivery).toHaveBeenCalledWith("d2"));
    await userEvent.click(within(panel).getByRole("button", { name: "Retry ticket sync" }));
    await waitFor(() => expect(api.retryTicket).toHaveBeenCalledWith("tk2"));
    await userEvent.selectOptions(within(panel).getByLabelText("ITSM connection"), "c3");
    await userEvent.click(within(panel).getByRole("button", { name: "Create ticket" }));
    await waitFor(() => expect(api.createTicket).toHaveBeenCalledWith("inc-1", "c3"));
  });

  it("offers no mutation controls to a read-only user", async () => {
    const panel = await openIncident(VIEWER);
    expect(within(panel).queryByRole("button", { name: /Acknowledge|Resolve|Retry|Create ticket/ })).not.toBeInTheDocument();
    expect(within(panel).getByText("INC0010042")).toBeInTheDocument();
  });

  it("surfaces a version conflict as an alert", async () => {
    vi.mocked(api.acknowledgeIncident).mockRejectedValue(new ApiError(409, "Conflict", "Resource has been modified by another request", null));
    const panel = await openIncident();
    await userEvent.click(within(panel).getByRole("button", { name: "Acknowledge incident" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("modified by another request");
  });

  it("shows empty and error states", async () => {
    vi.mocked(api.listIncidents).mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 });
    const first = renderWithProviders(<OperationsViews view="incidents" />, { user: MANAGER });
    expect(await screen.findByText(/No correlated incidents/)).toBeInTheDocument();
    first.unmount();
    vi.mocked(api.listIncidents).mockRejectedValue(new ApiError(403, "Forbidden", "Missing required permission: alarm:read", null));
    renderWithProviders(<OperationsViews view="incidents" />, { user: MANAGER });
    expect(await screen.findByRole("alert")).toHaveTextContent("Missing required permission");
  });
});

describe("collector health", () => {
  it("shows current state and the transition history", async () => {
    vi.mocked(api.listCollectorStates).mockResolvedValue([
      { collector_id: "c1", name: "edge-1", site_id: "s1", state: "offline", since: "2026-10-08T12:00:00Z", generation: 2, last_heartbeat_at: "2026-10-08T11:50:00Z", health: "offline" },
    ]);
    vi.mocked(api.listTransitions).mockResolvedValue({
      items: [{ id: "t1", collector_id: "c1", collector_name: "edge-1", site_id: "s1", generation: 2, from_state: "online", to_state: "offline", detected_at: "2026-10-08T12:00:00Z", heartbeat_at: "2026-10-08T11:50:00Z", never_heartbeat: false }],
      total: 1, limit: 50, offset: 0,
    });
    renderWithProviders(<OperationsViews view="collectors" />, { user: VIEWER });
    expect(await screen.findByTestId("collector-state-edge-1")).toHaveTextContent("Offline");
    const history = screen.getByRole("table", { name: "Transition history" });
    expect(within(history).getByText("online to offline")).toBeInTheDocument();
  });
});

describe("notification history", () => {
  it("lists deliveries and retries a failed one", async () => {
    const d: api.Delivery = { id: "d9", policy_id: "p", channel_id: "c", incident_id: null, event_type: "collector.offline", status: "failed", attempts: 5, max_attempts: 5, next_attempt_at: "2026-10-08T12:00:00Z", last_http_status: 503, failure_code: "ATTEMPTS_EXHAUSTED", created_at: "2026-10-08T11:00:00Z", sent_at: null };
    vi.mocked(api.listDeliveries).mockResolvedValue({ items: [d], total: 1, limit: 50, offset: 0 });
    vi.mocked(api.retryDelivery).mockResolvedValue(d);
    renderWithProviders(<OperationsViews view="notifications" />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: "Retry delivery" }));
    await waitFor(() => expect(api.retryDelivery).toHaveBeenCalledWith("d9"));
    await userEvent.selectOptions(screen.getByLabelText("State"), "failed");
    await waitFor(() => expect(api.listDeliveries).toHaveBeenCalledWith("failed"));
  });
});

describe("integration settings", () => {
  beforeEach(() => {
    vi.mocked(api.listChannels).mockResolvedValue([{ id: "ch1", name: "ops-hook", kind: "webhook", url_display: "https://hooks.example.com", has_signing_secret: true, enabled: true, version: 1 }]);
    vi.mocked(api.listPolicies).mockResolvedValue([]);
  });

  it("renders secrets only as presence flags and never keeps what was typed", async () => {
    vi.mocked(api.createChannel).mockResolvedValue({} as api.Channel);
    renderWithProviders(<OperationsViews view="integrations" />, { user: MANAGER });
    expect(await screen.findByText(/ops-hook: https:\/\/hooks.example.com, signed, enabled/)).toBeInTheDocument();
    const url = screen.getByLabelText("Webhook URL") as HTMLInputElement;
    const secret = screen.getByLabelText("Signing secret") as HTMLInputElement;
    expect(url.type).toBe("password");
    expect(secret.type).toBe("password");
    await userEvent.type(screen.getByLabelText("Channel name"), "pager");
    await userEvent.type(url, "https://hooks.example.com/h/TOKEN");
    await userEvent.type(secret, "s3cret");
    await userEvent.click(screen.getByRole("button", { name: "Add channel" }));
    await waitFor(() => expect(api.createChannel).toHaveBeenCalledWith({ name: "pager", url: "https://hooks.example.com/h/TOKEN", signing_secret: "s3cret" }));
    await waitFor(() => expect(url.value).toBe(""));
    expect(secret.value).toBe("");
    expect(document.body.textContent).not.toContain("TOKEN");
  });

  it("hides every settings form from a read-only user and shows the ITSM password field as a password input", async () => {
    const first = renderWithProviders(<OperationsViews view="integrations" />, { user: VIEWER });
    await screen.findByText(/ops-hook/);
    expect(screen.queryByRole("button", { name: /Add (channel|policy|connection)/ })).not.toBeInTheDocument();
    first.unmount();
    renderWithProviders(<OperationsViews view="integrations" />, { user: MANAGER });
    expect(((await screen.findByLabelText("Password")) as HTMLInputElement).type).toBe("password");
  });

  it("reports a rejected unsafe URL as an alert", async () => {
    vi.mocked(api.createChannel).mockRejectedValue(new ApiError(422, "Unprocessable Entity", "URL must use https.", null));
    renderWithProviders(<OperationsViews view="integrations" />, { user: MANAGER });
    await userEvent.type(await screen.findByLabelText("Channel name"), "x");
    await userEvent.type(screen.getByLabelText("Webhook URL"), "http://insecure.example.com");
    await userEvent.click(screen.getByRole("button", { name: "Add channel" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("URL must use https.");
  });
});

describe("events page integration", () => {
  it("keeps the alarm view by default and adds the operations tabs next to it", async () => {
    vi.mocked(telemetry.getOpenAlarms).mockResolvedValue([]);
    vi.mocked(telemetry.listAlarmHistory).mockResolvedValue({ items: [], next_cursor: null });
    renderWithProviders(<EventsPage />, { user: MANAGER });
    const tabs = await screen.findAllByRole("tab");
    expect(tabs.map((t) => t.textContent?.replace(/\d+$/, "").trim())).toEqual(["Active", "History", "Incidents", "Collector health", "Notifications", "Integrations"]);
    await userEvent.click(screen.getByRole("tab", { name: "Incidents" }));
    expect(await screen.findByRole("button", { name: "BRK-1" })).toBeInTheDocument();
    expect(screen.queryByText(/No active or acknowledged alarm/)).not.toBeInTheDocument();
  });
});
