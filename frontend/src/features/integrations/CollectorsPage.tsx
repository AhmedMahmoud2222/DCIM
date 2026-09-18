import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import {
  assignIntegration,
  declareCollectorCapabilities,
  listCollectorCapabilities,
  listCollectors,
  listIntegrations,
  registerCollector,
  triggerPollNow,
} from "@/features/integrations/api";
import { ApiError } from "@/lib/apiClient";

const HEALTH_COLORS: Record<string, string> = {
  healthy: "bg-green-900 text-green-200",
  stale: "bg-amber-900 text-amber-200",
  offline: "bg-red-900 text-red-200",
};

function HealthBadge({ health }: { health: string }) {
  return <span className={`rounded px-1.5 py-0.5 text-[10px] ${HEALTH_COLORS[health] ?? "bg-slate-700"}`}>{health}</span>;
}

function fmtSeconds(s: number | null): string {
  if (s === null) return "never";
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}

export function CollectorsPage() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showRegisterForm, setShowRegisterForm] = useState(false);
  const [name, setName] = useState("");
  const [collectorType, setCollectorType] = useState<"central" | "edge">("central");
  const [siteId, setSiteId] = useState("");
  const [lastSecret, setLastSecret] = useState<string | null>(null);
  const [capabilityInput, setCapabilityInput] = useState("icmp");
  const [assignIntegrationId, setAssignIntegrationId] = useState("");

  const collectorsQuery = useQuery({ queryKey: ["collectors"], queryFn: listCollectors });
  const integrationsQuery = useQuery({ queryKey: ["integrations"], queryFn: listIntegrations });
  const capabilitiesQuery = useQuery({
    queryKey: ["collectors", selectedId, "capabilities"],
    queryFn: () => listCollectorCapabilities(selectedId!),
    enabled: !!selectedId,
  });

  const registerMutation = useMutation({
    mutationFn: () => registerCollector({ name, collector_type: collectorType, site_id: collectorType === "edge" ? siteId : null }),
    onSuccess: (collector) => {
      setLastSecret(collector.secret);
      setShowRegisterForm(false);
      setName("");
      setSiteId("");
      queryClient.invalidateQueries({ queryKey: ["collectors"] });
    },
  });

  const declareMutation = useMutation({
    mutationFn: () => declareCollectorCapabilities(selectedId!, [capabilityInput]),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["collectors", selectedId, "capabilities"] }),
  });

  const assignMutation = useMutation({
    mutationFn: () => assignIntegration(selectedId!, assignIntegrationId),
    onSuccess: () => {
      setAssignIntegrationId("");
      queryClient.invalidateQueries({ queryKey: ["integrations"] });
    },
  });

  const pollMutation = useMutation({ mutationFn: () => triggerPollNow(selectedId!) });

  const selected = collectorsQuery.data?.find((c) => c.id === selectedId);
  const assignedIntegrations = integrationsQuery.data?.filter((i) => i.assigned_collector_id === selectedId) ?? [];

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Collectors</h1>
      <p className="mb-6 max-w-2xl text-sm text-slate-400">
        Central and edge collectors acquire telemetry via SNMP/ICMP/REST and forward it to this server. A collector
        never holds the authoritative inventory itself (Phase 8).
      </p>

      <div className="grid grid-cols-3 gap-6">
        <div className="rounded border border-slate-800 bg-slate-900 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-300">Registered Collectors</h2>
            <button
              onClick={() => setShowRegisterForm((v) => !v)}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700"
            >
              + Register
            </button>
          </div>

          {showRegisterForm && (
            <form
              onSubmit={(e: FormEvent) => {
                e.preventDefault();
                registerMutation.mutate();
              }}
              className="mb-3 space-y-2 rounded border border-slate-700 bg-slate-800/50 p-2"
            >
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Collector name"
                required
                className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
              />
              <select
                value={collectorType}
                onChange={(e) => setCollectorType(e.target.value as "central" | "edge")}
                className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
              >
                <option value="central">central</option>
                <option value="edge">edge</option>
              </select>
              {collectorType === "edge" && (
                <input
                  value={siteId}
                  onChange={(e) => setSiteId(e.target.value)}
                  placeholder="Site ID (required for edge)"
                  required
                  className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
                />
              )}
              <button
                type="submit"
                disabled={registerMutation.isPending}
                className="w-full rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                Register
              </button>
              {registerMutation.isError && (
                <p className="text-xs text-red-400">
                  {registerMutation.error instanceof ApiError ? registerMutation.error.detail : (registerMutation.error as Error).message}
                </p>
              )}
            </form>
          )}

          {lastSecret && (
            <div className="mb-3 rounded border border-amber-700 bg-amber-950/50 p-2 text-xs text-amber-200">
              <p className="mb-1 font-semibold">Collector secret (shown once — copy it now):</p>
              <code className="break-all">{lastSecret}</code>
              <button onClick={() => setLastSecret(null)} className="mt-1 block text-amber-400 hover:text-amber-300">
                Dismiss
              </button>
            </div>
          )}

          <div className="max-h-[420px] space-y-1 overflow-y-auto">
            {collectorsQuery.data?.map((c) => (
              <button
                key={c.id}
                onClick={() => setSelectedId(c.id)}
                className={`flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs hover:bg-slate-800 ${
                  selectedId === c.id ? "bg-slate-800" : ""
                }`}
              >
                <span className="truncate">
                  {c.name} <span className="text-slate-500">({c.collector_type})</span>
                </span>
                <HealthBadge health={c.health} />
              </button>
            ))}
            {collectorsQuery.data?.length === 0 && <p className="text-xs italic text-slate-500">No collectors registered yet.</p>}
          </div>
        </div>

        <div className="col-span-2 space-y-4">
          {!selected && (
            <div className="rounded border border-slate-800 bg-slate-900 p-4 text-sm text-slate-500">
              Select a collector from the list to inspect it.
            </div>
          )}

          {selected && (
            <>
              <div className="rounded border border-slate-800 bg-slate-900 p-4">
                <div className="mb-3 flex items-center justify-between">
                  <div>
                    <h2 className="text-sm font-semibold text-slate-200">{selected.name}</h2>
                    <div className="mt-1 flex gap-2">
                      <HealthBadge health={selected.health} />
                      <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">{selected.status}</span>
                    </div>
                  </div>
                  <button
                    onClick={() => pollMutation.mutate()}
                    disabled={pollMutation.isPending}
                    className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
                  >
                    Poll now
                  </button>
                </div>

                <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
                  <div>
                    <dt className="text-slate-500">Type</dt>
                    <dd>{selected.collector_type}</dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Site</dt>
                    <dd>{selected.site_id ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Version</dt>
                    <dd>{selected.version_string ?? "—"}</dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Last heartbeat</dt>
                    <dd>{fmtSeconds(selected.seconds_since_heartbeat)}</dd>
                  </div>
                </dl>

                {pollMutation.data && (
                  <div className="mt-3 space-y-1 text-xs">
                    {pollMutation.data.map((o, i) => (
                      <div key={i} className={o.succeeded ? "text-green-400" : "text-red-400"}>
                        {o.integration_id}: {o.succeeded ? `ok (${o.external_identifier ?? "—"})` : o.error}
                      </div>
                    ))}
                    {pollMutation.data.length === 0 && <p className="italic text-slate-500">No integrations assigned to poll.</p>}
                  </div>
                )}
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="rounded border border-slate-800 bg-slate-900 p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Capabilities</h3>
                  <div className="mb-2 flex flex-wrap gap-1">
                    {capabilitiesQuery.data?.map((c) => (
                      <span key={c.protocol_code} className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">
                        {c.protocol_code}
                      </span>
                    ))}
                    {capabilitiesQuery.data?.length === 0 && <p className="text-xs italic text-slate-500">None declared.</p>}
                  </div>
                  <form
                    onSubmit={(e: FormEvent) => {
                      e.preventDefault();
                      declareMutation.mutate();
                    }}
                    className="flex gap-1"
                  >
                    <select
                      value={capabilityInput}
                      onChange={(e) => setCapabilityInput(e.target.value)}
                      className="flex-1 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
                    >
                      <option value="icmp">icmp</option>
                      <option value="snmp">snmp</option>
                      <option value="rest">rest</option>
                    </select>
                    <button
                      type="submit"
                      disabled={declareMutation.isPending}
                      className="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                    >
                      Declare
                    </button>
                  </form>
                </div>

                <div className="rounded border border-slate-800 bg-slate-900 p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Assigned Integrations</h3>
                  <div className="mb-2 space-y-1">
                    {assignedIntegrations.map((i) => (
                      <div key={i.id} className="rounded bg-slate-800/60 px-2 py-1 text-xs">
                        {i.name} <span className="text-slate-500">({i.integration_type})</span>
                      </div>
                    ))}
                    {assignedIntegrations.length === 0 && <p className="text-xs italic text-slate-500">None assigned.</p>}
                  </div>
                  <form
                    onSubmit={(e: FormEvent) => {
                      e.preventDefault();
                      assignMutation.mutate();
                    }}
                    className="flex gap-1"
                  >
                    <select
                      value={assignIntegrationId}
                      onChange={(e) => setAssignIntegrationId(e.target.value)}
                      required
                      className="flex-1 rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
                    >
                      <option value="">Select integration…</option>
                      {integrationsQuery.data?.map((i) => (
                        <option key={i.id} value={i.id}>
                          {i.name}
                        </option>
                      ))}
                    </select>
                    <button
                      type="submit"
                      disabled={assignMutation.isPending}
                      className="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                    >
                      Assign
                    </button>
                  </form>
                  {assignMutation.isError && (
                    <p className="mt-1 text-xs text-red-400">
                      {assignMutation.error instanceof ApiError ? assignMutation.error.detail : (assignMutation.error as Error).message}
                    </p>
                  )}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
