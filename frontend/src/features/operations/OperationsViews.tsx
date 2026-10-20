import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import {
  Channel,
  Confidence,
  Delivery,
  DeliverySummary,
  IncidentDetail,
  Ticket,
  acknowledgeIncident,
  createChannel,
  createItsmConnection,
  createPolicy,
  createTicket,
  getIncident,
  listChannels,
  listCollectorStates,
  listDeliveries,
  listIncidents,
  listItsmConnections,
  listPolicies,
  listTransitions,
  resolveIncident,
  retryDelivery,
  retryTicket,
} from "@/features/operations/api";
import { ApiError } from "@/lib/apiClient";

export type OpsView = "incidents" | "collectors" | "notifications" | "integrations";

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "n/a");
const errorText = (e: unknown) => (e instanceof ApiError ? e.detail : e instanceof Error ? e.message : "Request failed");

function ErrorLine({ error }: { error: unknown }) {
  return (
    <p role="alert" className="text-sm text-red-400">
      {errorText(error)}
    </p>
  );
}

function Loading({ what }: { what: string }) {
  return (
    <p role="status" className="text-sm text-slate-400">
      Loading {what}.
    </p>
  );
}

const CONFIDENCE_TEXT: Record<Confidence, string> = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" };
const RULE_TEXT: Record<string, string> = {
  collector_offline: "Collector offline",
  shared_power_cause: "Shared power cause",
  same_device: "Same device",
  same_integration: "Same integration",
};

/** Plain-language state for a delivery or ticket, including the provider-unavailable case. */
function providerState(status: string, attempts: number, max: number | null, code: string | null): string {
  if (status === "sent") return "Delivered";
  if (status === "synced") return "Synced";
  if (status === "retry") return `Provider unavailable (${code ?? "error"}): retrying, attempt ${attempts}${max ? ` of ${max}` : ""}`;
  if (status === "failed") return `Failed (${code ?? "error"}): retries stopped`;
  if (status === "sending" || status === "syncing") return "Sending";
  return "Queued";
}

function DeliveryTable({ rows, canManage, onRetry }: { rows: DeliverySummary[]; canManage: boolean; onRetry: (id: string) => void }) {
  if (rows.length === 0) return <p className="text-sm text-slate-500">No notifications were queued for this incident.</p>;
  return (
    <table className="w-full text-left text-sm">
      <caption className="mb-1 text-left text-sm font-semibold">Notifications</caption>
      <thead className="text-slate-400">
        <tr>
          <th scope="col">Channel</th>
          <th scope="col">Event</th>
          <th scope="col">State</th>
          <th scope="col">Next attempt</th>
          <th scope="col">Action</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((d) => (
          <tr key={d.id} className="border-t border-slate-800">
            <th scope="row" className="font-normal">
              {d.channel_name}
            </th>
            <td>{d.event_type}</td>
            <td>{providerState(d.status, d.attempts, d.max_attempts, d.failure_code)}</td>
            <td>{d.status === "retry" || d.status === "pending" ? when(d.next_attempt_at) : "n/a"}</td>
            <td>
              {d.status === "failed" && canManage ? (
                <button type="button" onClick={() => onRetry(d.id)} className="rounded-sm bg-slate-800 px-2 py-0.5 text-xs hover:bg-slate-700">
                  Retry delivery
                </button>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function TicketTable({ rows, canManage, onRetry }: { rows: Ticket[]; canManage: boolean; onRetry: (id: string) => void }) {
  if (rows.length === 0) return <p className="text-sm text-slate-500">No ITSM ticket is linked to this incident.</p>;
  return (
    <table className="w-full text-left text-sm">
      <caption className="mb-1 text-left text-sm font-semibold">ITSM tickets</caption>
      <thead className="text-slate-400">
        <tr>
          <th scope="col">Connection</th>
          <th scope="col">Ticket</th>
          <th scope="col">Remote state</th>
          <th scope="col">Sync</th>
          <th scope="col">Action</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((t) => (
          <tr key={t.id} className="border-t border-slate-800">
            <th scope="row" className="font-normal">
              {t.connection_name}
            </th>
            <td>{t.external_number ?? "pending"}</td>
            <td>{t.external_state.replace(/_/g, " ")}</td>
            <td>{providerState(t.status, t.attempts, null, t.failure_code)}</td>
            <td>
              {t.status === "failed" && canManage ? (
                <button type="button" onClick={() => onRetry(t.id)} className="rounded-sm bg-slate-800 px-2 py-0.5 text-xs hover:bg-slate-700">
                  Retry ticket sync
                </button>
              ) : null}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function IncidentPanel({ id }: { id: string }) {
  const qc = useQueryClient();
  const canAlarm = useHasPermission("alarm:manage");
  const canIntegrate = useHasPermission("integration:manage");
  const q = useQuery({
    queryKey: ["ops", "incident", id],
    queryFn: () => getIncident(id),
    staleTime: 0,
    // Delivery and ticket sync run in the background, so keep refreshing while any of them is unfinished.
    refetchInterval: (query) => {
      const d = query.state.data;
      const open = (s: string) => ["pending", "sending", "retry", "syncing"].includes(s);
      return d && (d.notifications.some((n) => open(n.status)) || d.tickets.some((t) => open(t.status))) ? 3000 : false;
    },
  });
  const connections = useQuery({ queryKey: ["ops", "itsm"], queryFn: listItsmConnections });
  const [connectionId, setConnectionId] = useState("");
  const refresh = () => qc.invalidateQueries({ queryKey: ["ops"] });
  const act = useMutation({
    mutationFn: (v: { kind: "ack" | "resolve"; version: number }) =>
      v.kind === "ack" ? acknowledgeIncident(id, v.version) : resolveIncident(id, v.version),
    onSuccess: refresh,
  });
  const ticket = useMutation({ mutationFn: () => createTicket(id, connectionId), onSuccess: refresh });
  const retryD = useMutation({ mutationFn: (id: string) => retryDelivery(id), onSuccess: refresh });
  const retryT = useMutation({ mutationFn: (id: string) => retryTicket(id), onSuccess: refresh });
  if (q.isPending) return <Loading what="incident" />;
  if (q.isError) return <ErrorLine error={q.error} />;
  const d: IncidentDetail = q.data;
  const enabled = (connections.data ?? []).filter((c) => c.enabled);
  return (
    <section aria-label="Incident detail" className="space-y-4 rounded-sm border border-slate-800 bg-slate-900 p-4">
      <header>
        <h2 className="text-base font-semibold">{d.cause_label}</h2>
        <p className="text-sm text-slate-400">
          {RULE_TEXT[d.rule] ?? d.rule}, {CONFIDENCE_TEXT[d.confidence]}, {d.status}
        </p>
      </header>
      <div>
        <h3 className="text-sm font-semibold">Probable cause and evidence</h3>
        <p className="text-sm" data-testid="rationale">
          {d.rationale}
        </p>
        <ul className="mt-1 list-disc pl-5 text-sm text-slate-300">
          {d.evidence
            .filter((e) => e.type !== "alarm")
            .map((e, i) => (
              <li key={i}>
                {String(e.type).replace(/_/g, " ")}
                {e.label ? `: ${String(e.label)}` : e.collector_name ? `: ${String(e.collector_name)}` : ""}
                {e.state ? ` (${String(e.state)})` : ""}
              </li>
            ))}
        </ul>
        <p className="mt-1 text-xs text-slate-500">
          Correlation {d.correlation_id}. Resolving the incident never clears or changes the source alarms below.
          {d.all_sources_cleared ? " All source alarms have cleared." : ""}
        </p>
      </div>
      <table className="w-full text-left text-sm">
        <caption className="mb-1 text-left text-sm font-semibold">Source events</caption>
        <thead className="text-slate-400">
          <tr>
            <th scope="col">Role</th>
            <th scope="col">Source</th>
            <th scope="col">State</th>
            <th scope="col">Time</th>
          </tr>
        </thead>
        <tbody>
          {d.members.map((m, i) => (
            <tr key={i} className="border-t border-slate-800">
              <th scope="row" className="font-normal">
                {m.role}
              </th>
              <td>{m.member_type === "alarm" ? m.alarm_subject : `Collector ${m.collector_name ?? ""}`}</td>
              <td>{m.member_type === "alarm" ? m.alarm_status : m.transition_to_state}</td>
              <td>{when(m.source_time)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <DeliveryTable rows={d.notifications} canManage={canIntegrate} onRetry={(i) => retryD.mutate(i)} />
      <TicketTable rows={d.tickets} canManage={canIntegrate} onRetry={(i) => retryT.mutate(i)} />
      {(act.isError || ticket.isError || retryD.isError || retryT.isError) && (
        <ErrorLine error={act.error ?? ticket.error ?? retryD.error ?? retryT.error} />
      )}
      {canAlarm && d.status !== "resolved" && (
        <div className="flex flex-wrap items-end gap-2">
          {d.status === "open" && (
            <button type="button" disabled={act.isPending} onClick={() => act.mutate({ kind: "ack", version: d.version })} className="rounded-sm bg-yellow-800 px-3 py-1 text-sm text-yellow-100 disabled:opacity-50">
              Acknowledge incident
            </button>
          )}
          <button type="button" disabled={act.isPending} onClick={() => act.mutate({ kind: "resolve", version: d.version })} className="rounded-sm bg-green-800 px-3 py-1 text-sm text-green-100 disabled:opacity-50">
            Resolve incident
          </button>
        </div>
      )}
      {canAlarm && enabled.length > 0 && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (connectionId) ticket.mutate();
          }}
        >
          <label className="text-sm">
            <span className="mr-2 text-slate-400">ITSM connection</span>
            <select value={connectionId} onChange={(e) => setConnectionId(e.target.value)} className="rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm">
              <option value="">Select a connection</option>
              {enabled.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={!connectionId || ticket.isPending} className="rounded-sm bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50">
            Create ticket
          </button>
        </form>
      )}
    </section>
  );
}

function IncidentsView() {
  const q = useQuery({ queryKey: ["ops", "incidents"], queryFn: () => listIncidents() });
  const [selected, setSelected] = useState<string | null>(null);
  if (q.isPending) return <Loading what="incidents" />;
  if (q.isError) return <ErrorLine error={q.error} />;
  if (q.data.items.length === 0) return <p className="text-sm text-slate-500">No correlated incidents. Unrelated alarms stay separate.</p>;
  return (
    <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <table className="w-full text-left text-sm">
        <caption className="mb-1 text-left text-sm font-semibold">Correlated incidents</caption>
        <thead className="text-slate-400">
          <tr>
            <th scope="col">Probable cause</th>
            <th scope="col">Rule</th>
            <th scope="col">Confidence</th>
            <th scope="col">Status</th>
            <th scope="col">Sources</th>
            <th scope="col">Opened</th>
          </tr>
        </thead>
        <tbody>
          {q.data.items.map((i) => (
            <tr key={i.id} className={`border-t border-slate-800 ${selected === i.id ? "bg-slate-800/70" : ""}`}>
              <th scope="row" className="font-normal">
                <button type="button" onClick={() => setSelected(i.id)} aria-pressed={selected === i.id} className="text-left text-sky-300 hover:underline">
                  {i.cause_label}
                </button>
              </th>
              <td>{RULE_TEXT[i.rule] ?? i.rule}</td>
              <td>{CONFIDENCE_TEXT[i.confidence]}</td>
              <td>{i.status}</td>
              <td>{i.member_count}</td>
              <td>{when(i.opened_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {selected ? <IncidentPanel id={selected} /> : <p className="text-sm text-slate-500">Select an incident to see its evidence and source events.</p>}
    </div>
  );
}

function CollectorsView() {
  const states = useQuery({ queryKey: ["ops", "collector-states"], queryFn: listCollectorStates });
  const transitions = useQuery({ queryKey: ["ops", "transitions"], queryFn: listTransitions });
  if (states.isPending || transitions.isPending) return <Loading what="collector health" />;
  if (states.isError) return <ErrorLine error={states.error} />;
  if (transitions.isError) return <ErrorLine error={transitions.error} />;
  return (
    <div className="space-y-6">
      <table className="w-full text-left text-sm">
        <caption className="mb-1 text-left text-sm font-semibold">Collector state</caption>
        <thead className="text-slate-400">
          <tr>
            <th scope="col">Collector</th>
            <th scope="col">State</th>
            <th scope="col">Since</th>
            <th scope="col">Last heartbeat</th>
            <th scope="col">Live health</th>
          </tr>
        </thead>
        <tbody>
          {states.data.map((s) => (
            <tr key={s.collector_id} className="border-t border-slate-800">
              <th scope="row" className="font-normal">
                {s.name}
              </th>
              <td data-testid={`collector-state-${s.name}`}>{s.state === "offline" ? "Offline" : "Online"}</td>
              <td>{when(s.since)}</td>
              <td>{when(s.last_heartbeat_at)}</td>
              <td>{s.health}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {states.data.length === 0 && <p className="text-sm text-slate-500">No collector state has been recorded yet.</p>}
      <table className="w-full text-left text-sm">
        <caption className="mb-1 text-left text-sm font-semibold">Transition history</caption>
        <thead className="text-slate-400">
          <tr>
            <th scope="col">Detected</th>
            <th scope="col">Collector</th>
            <th scope="col">Change</th>
            <th scope="col">Last heartbeat</th>
          </tr>
        </thead>
        <tbody>
          {transitions.data.items.map((t) => (
            <tr key={t.id} className="border-t border-slate-800">
              <th scope="row" className="font-normal">
                {when(t.detected_at)}
              </th>
              <td>{t.collector_name}</td>
              <td>
                {t.from_state} to {t.to_state}
              </td>
              <td>{t.never_heartbeat ? "never seen" : when(t.heartbeat_at)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {transitions.data.items.length === 0 && <p className="text-sm text-slate-500">No transitions yet.</p>}
    </div>
  );
}

function NotificationsView() {
  const qc = useQueryClient();
  const canManage = useHasPermission("integration:manage");
  const [status, setStatus] = useState("");
  const q = useQuery({ queryKey: ["ops", "deliveries", status], queryFn: () => listDeliveries(status || undefined) });
  const retry = useMutation({ mutationFn: (id: string) => retryDelivery(id), onSuccess: () => qc.invalidateQueries({ queryKey: ["ops"] }) });
  return (
    <div className="space-y-3">
      <label className="block text-sm">
        <span className="mr-2 text-slate-400">State</span>
        <select value={status} onChange={(e) => setStatus(e.target.value)} className="rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm">
          <option value="">All</option>
          {["pending", "sending", "retry", "sent", "failed"].map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>
      {retry.isError && <ErrorLine error={retry.error} />}
      {q.isPending ? (
        <Loading what="deliveries" />
      ) : q.isError ? (
        <ErrorLine error={q.error} />
      ) : q.data.items.length === 0 ? (
        <p className="text-sm text-slate-500">No notification deliveries.</p>
      ) : (
        <table className="w-full text-left text-sm">
          <caption className="mb-1 text-left text-sm font-semibold">Delivery history</caption>
          <thead className="text-slate-400">
            <tr>
              <th scope="col">Created</th>
              <th scope="col">Event</th>
              <th scope="col">State</th>
              <th scope="col">Action</th>
            </tr>
          </thead>
          <tbody>
            {q.data.items.map((d: Delivery) => (
              <tr key={d.id} className="border-t border-slate-800">
                <th scope="row" className="font-normal">
                  {when(d.created_at)}
                </th>
                <td>{d.event_type}</td>
                <td>{providerState(d.status, d.attempts, d.max_attempts, d.failure_code)}</td>
                <td>
                  {d.status === "failed" && canManage ? (
                    <button type="button" onClick={() => retry.mutate(d.id)} className="rounded-sm bg-slate-800 px-2 py-0.5 text-xs hover:bg-slate-700">
                      Retry delivery
                    </button>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}

function IntegrationsView() {
  const qc = useQueryClient();
  const canManage = useHasPermission("integration:manage");
  const channels = useQuery({ queryKey: ["ops", "channels"], queryFn: listChannels });
  const policies = useQuery({ queryKey: ["ops", "policies"], queryFn: listPolicies });
  const itsm = useQuery({ queryKey: ["ops", "itsm"], queryFn: listItsmConnections });
  const [ch, setCh] = useState({ name: "", url: "", secret: "" });
  const [pol, setPol] = useState({ name: "", channel: "", events: ["incident.opened"] as string[] });
  const [sn, setSn] = useState({ name: "", url: "", user: "", password: "", auto: false });
  const refresh = () => qc.invalidateQueries({ queryKey: ["ops"] });
  const addChannel = useMutation({
    mutationFn: () => createChannel({ name: ch.name, url: ch.url, signing_secret: ch.secret || undefined }),
    onSuccess: () => {
      setCh({ name: "", url: "", secret: "" }); // the secret never stays in the page after saving
      refresh();
    },
  });
  const addPolicy = useMutation({
    mutationFn: () => createPolicy({ name: pol.name, channel_id: pol.channel, event_types: pol.events }),
    onSuccess: () => {
      setPol({ name: "", channel: "", events: ["incident.opened"] });
      refresh();
    },
  });
  const addItsm = useMutation({
    mutationFn: () => createItsmConnection({ name: sn.name, base_url: sn.url, username: sn.user, password: sn.password, auto_create: sn.auto }),
    onSuccess: () => {
      setSn({ name: "", url: "", user: "", password: "", auto: false });
      refresh();
    },
  });
  const field = "rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-sm";
  const submit = (fn: () => void) => (e: FormEvent) => {
    e.preventDefault();
    fn();
  };
  return (
    <div className="space-y-8">
      <section aria-label="Notification channels" className="space-y-2">
        <h2 className="text-sm font-semibold">Notification channels</h2>
        {channels.isPending ? (
          <Loading what="channels" />
        ) : channels.isError ? (
          <ErrorLine error={channels.error} />
        ) : (
          <ul className="text-sm">
            {channels.data.map((c: Channel) => (
              <li key={c.id}>
                {c.name}: {c.url_display}, {c.has_signing_secret ? "signed" : "unsigned"}, {c.enabled ? "enabled" : "disabled"}
              </li>
            ))}
            {channels.data.length === 0 && <li className="text-slate-500">No channels.</li>}
          </ul>
        )}
        {canManage && (
          <form className="flex flex-wrap items-end gap-2" onSubmit={submit(() => addChannel.mutate())}>
            <label className="text-sm">
              <span className="block text-slate-400">Channel name</span>
              <input required value={ch.name} onChange={(e) => setCh({ ...ch, name: e.target.value })} className={field} />
            </label>
            <label className="text-sm">
              <span className="block text-slate-400">Webhook URL</span>
              <input required type="password" autoComplete="off" value={ch.url} onChange={(e) => setCh({ ...ch, url: e.target.value })} className={field} />
            </label>
            <label className="text-sm">
              <span className="block text-slate-400">Signing secret</span>
              <input type="password" autoComplete="off" value={ch.secret} onChange={(e) => setCh({ ...ch, secret: e.target.value })} className={field} />
            </label>
            <button type="submit" disabled={addChannel.isPending} className="rounded-sm bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50">
              Add channel
            </button>
          </form>
        )}
        {addChannel.isError && <ErrorLine error={addChannel.error} />}
      </section>

      <section aria-label="Notification policies" className="space-y-2">
        <h2 className="text-sm font-semibold">Notification policies</h2>
        <ul className="text-sm">
          {(policies.data ?? []).map((p) => (
            <li key={p.id}>
              {p.name}: {p.event_types.join(", ")} ({p.enabled ? "enabled" : "disabled"})
            </li>
          ))}
          {policies.data?.length === 0 && <li className="text-slate-500">No policies.</li>}
        </ul>
        {canManage && (
          <form className="flex flex-wrap items-end gap-2" onSubmit={submit(() => addPolicy.mutate())}>
            <label className="text-sm">
              <span className="block text-slate-400">Policy name</span>
              <input required value={pol.name} onChange={(e) => setPol({ ...pol, name: e.target.value })} className={field} />
            </label>
            <label className="text-sm">
              <span className="block text-slate-400">Channel</span>
              <select required value={pol.channel} onChange={(e) => setPol({ ...pol, channel: e.target.value })} className={field}>
                <option value="">Select a channel</option>
                {(channels.data ?? []).map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.name}
                  </option>
                ))}
              </select>
            </label>
            <fieldset className="text-sm">
              <legend className="text-slate-400">Events</legend>
              {["incident.opened", "collector.offline", "collector.online"].map((ev) => (
                <label key={ev} className="mr-3">
                  <input
                    type="checkbox"
                    checked={pol.events.includes(ev)}
                    onChange={(e) => setPol({ ...pol, events: e.target.checked ? [...pol.events, ev] : pol.events.filter((x) => x !== ev) })}
                  />{" "}
                  {ev}
                </label>
              ))}
            </fieldset>
            <button type="submit" disabled={addPolicy.isPending || pol.events.length === 0} className="rounded-sm bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50">
              Add policy
            </button>
          </form>
        )}
        {addPolicy.isError && <ErrorLine error={addPolicy.error} />}
      </section>

      <section aria-label="ITSM connections" className="space-y-2">
        <h2 className="text-sm font-semibold">ITSM connections (ServiceNow)</h2>
        <ul className="text-sm">
          {(itsm.data ?? []).map((c) => (
            <li key={c.id}>
              {c.name}: {c.base_url} as {c.username}, {c.enabled ? "enabled" : "disabled"}, {c.auto_create ? "creates tickets automatically" : "manual tickets"}
            </li>
          ))}
          {itsm.data?.length === 0 && <li className="text-slate-500">No ITSM connection.</li>}
        </ul>
        {canManage && (
          <form className="flex flex-wrap items-end gap-2" onSubmit={submit(() => addItsm.mutate())}>
            <label className="text-sm">
              <span className="block text-slate-400">Connection name</span>
              <input required value={sn.name} onChange={(e) => setSn({ ...sn, name: e.target.value })} className={field} />
            </label>
            <label className="text-sm">
              <span className="block text-slate-400">Instance URL</span>
              <input required value={sn.url} onChange={(e) => setSn({ ...sn, url: e.target.value })} className={field} />
            </label>
            <label className="text-sm">
              <span className="block text-slate-400">Integration user</span>
              <input required autoComplete="off" value={sn.user} onChange={(e) => setSn({ ...sn, user: e.target.value })} className={field} />
            </label>
            <label className="text-sm">
              <span className="block text-slate-400">Password</span>
              <input required type="password" autoComplete="new-password" value={sn.password} onChange={(e) => setSn({ ...sn, password: e.target.value })} className={field} />
            </label>
            <label className="text-sm">
              <input type="checkbox" checked={sn.auto} onChange={(e) => setSn({ ...sn, auto: e.target.checked })} /> Create tickets automatically
            </label>
            <button type="submit" disabled={addItsm.isPending} className="rounded-sm bg-blue-600 px-3 py-1 text-sm text-white disabled:opacity-50">
              Add connection
            </button>
          </form>
        )}
        {addItsm.isError && <ErrorLine error={addItsm.error} />}
      </section>
    </div>
  );
}

export function OperationsViews({ view }: { view: OpsView }) {
  return (
    <div className="mt-4 rounded-sm border border-slate-800 bg-slate-900 p-4" role="tabpanel" aria-label={view}>
      {view === "incidents" && <IncidentsView />}
      {view === "collectors" && <CollectorsView />}
      {view === "notifications" && <NotificationsView />}
      {view === "integrations" && <IntegrationsView />}
    </div>
  );
}
