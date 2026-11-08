import { apiFetch } from "@/lib/apiClient";
import { Page } from "@/types";

export type Confidence = "high" | "medium" | "low";
export type IncidentStatus = "open" | "acknowledged" | "resolved";

export interface Incident {
  id: string;
  rule: string;
  cause_type: string;
  cause_label: string;
  confidence: Confidence;
  status: IncidentStatus;
  site_id: string | null;
  opened_at: string;
  last_member_at: string;
  member_count: number;
  ticket_count: number;
  version: number;
}

export interface Member {
  member_type: "alarm" | "collector_transition";
  role: "cause" | "symptom";
  source_time: string;
  alarm_id: string | null;
  alarm_status: string | null;
  alarm_subject: string | null;
  alarm_opened_at: string | null;
  alarm_cleared_at: string | null;
  transition_id: string | null;
  collector_name: string | null;
  transition_to_state: string | null;
}

export interface DeliverySummary {
  id: string;
  event_type: string;
  status: "pending" | "sending" | "retry" | "sent" | "failed";
  attempts: number;
  max_attempts: number;
  next_attempt_at: string;
  failure_code: string | null;
  sent_at: string | null;
  channel_name: string;
}

export interface Ticket {
  id: string;
  incident_id: string;
  connection_id: string;
  connection_name: string;
  status: "pending" | "syncing" | "retry" | "synced" | "failed";
  external_number: string | null;
  external_state: string;
  attempts: number;
  next_attempt_at: string;
  failure_code: string | null;
  last_synced_at: string | null;
}

export interface IncidentDetail extends Incident {
  rationale: string;
  evidence: Record<string, unknown>[];
  correlation_id: string;
  causation_id: string | null;
  method_version: string;
  resolved_at: string | null;
  all_sources_cleared: boolean;
  members: Member[];
  notifications: DeliverySummary[];
  tickets: Ticket[];
}

export interface CollectorState {
  collector_id: string;
  name: string;
  site_id: string | null;
  state: "online" | "offline";
  since: string;
  generation: number;
  last_heartbeat_at: string | null;
  health: string;
}

export interface Transition {
  id: string;
  collector_id: string;
  collector_name: string;
  site_id: string | null;
  generation: number;
  from_state: string;
  to_state: string;
  detected_at: string;
  heartbeat_at: string | null;
  never_heartbeat: boolean;
}

export interface Channel {
  id: string;
  name: string;
  kind: string;
  url_display: string;
  has_signing_secret: boolean;
  enabled: boolean;
  version: number;
}

export interface Policy {
  id: string;
  name: string;
  channel_id: string;
  event_types: string[];
  site_id: string | null;
  min_confidence: Confidence | null;
  enabled: boolean;
  version: number;
}

export interface Delivery {
  id: string;
  policy_id: string;
  channel_id: string;
  incident_id: string | null;
  event_type: string;
  status: DeliverySummary["status"];
  attempts: number;
  max_attempts: number;
  next_attempt_at: string;
  last_http_status: number | null;
  failure_code: string | null;
  created_at: string;
  sent_at: string | null;
}

export interface ItsmConnection {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  username: string;
  has_password: boolean;
  enabled: boolean;
  auto_create: boolean;
  min_confidence: Confidence | null;
  version: number;
}

const O = "/operations";
export const listIncidents = (status?: IncidentStatus) =>
  apiFetch<Page<Incident>>(`${O}/incidents?limit=100${status ? `&status=${status}` : ""}`);
export const getIncident = (id: string) => apiFetch<IncidentDetail>(`${O}/incidents/${id}`);
export const acknowledgeIncident = (id: string, version: number) =>
  apiFetch<Incident>(`${O}/incidents/${id}/acknowledge`, { method: "POST", ifMatch: version });
export const resolveIncident = (id: string, version: number) =>
  apiFetch<Incident>(`${O}/incidents/${id}/resolve`, { method: "POST", ifMatch: version });
export const createTicket = (incidentId: string, connectionId: string) =>
  apiFetch<Ticket>(`${O}/incidents/${incidentId}/tickets`, { method: "POST", body: JSON.stringify({ connection_id: connectionId }) });
export const retryTicket = (id: string) => apiFetch<Ticket>(`${O}/itsm-tickets/${id}/retry`, { method: "POST" });

export const listCollectorStates = () => apiFetch<CollectorState[]>(`${O}/collector-states`);
export const listTransitions = () => apiFetch<Page<Transition>>(`${O}/collector-transitions?limit=50`);

export const listDeliveries = (status?: string) =>
  apiFetch<Page<Delivery>>(`${O}/notification-deliveries?limit=50${status ? `&status=${status}` : ""}`);
export const retryDelivery = (id: string) => apiFetch<Delivery>(`${O}/notification-deliveries/${id}/retry`, { method: "POST" });

export const listChannels = () => apiFetch<Channel[]>(`${O}/notification-channels`);
export const createChannel = (body: { name: string; url: string; signing_secret?: string }) =>
  apiFetch<Channel>(`${O}/notification-channels`, { method: "POST", body: JSON.stringify(body) });
export const listPolicies = () => apiFetch<Policy[]>(`${O}/notification-policies`);
export const createPolicy = (body: { name: string; channel_id: string; event_types: string[]; min_confidence?: Confidence }) =>
  apiFetch<Policy>(`${O}/notification-policies`, { method: "POST", body: JSON.stringify(body) });
export const listItsmConnections = () => apiFetch<ItsmConnection[]>(`${O}/itsm-connections`);
export const createItsmConnection = (body: {
  name: string;
  base_url: string;
  username: string;
  password: string;
  auto_create: boolean;
}) => apiFetch<ItsmConnection>(`${O}/itsm-connections`, { method: "POST", body: JSON.stringify(body) });
