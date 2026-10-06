import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import {
  CABLE_TYPES,
  CableType,
  confirmNeighbor,
  createCableFromNeighbor,
  listNeighbors,
  Neighbor,
  NeighborFilters,
  ReconciliationState,
  rejectNeighbor,
  rematchNeighbor,
  revokeNeighbor,
} from "@/features/network/api";
import { ApiError } from "@/lib/apiClient";

const STATE_STYLE: Record<ReconciliationState, string> = {
  unmatched: "bg-slate-700 text-slate-200",
  ambiguous: "bg-red-900 text-red-200",
  proposed: "bg-blue-900 text-blue-200",
  conflict: "bg-orange-900 text-orange-200",
  confirmed: "bg-green-900 text-green-200",
  rejected: "bg-slate-800 text-slate-400",
};

const STATE_HELP: Record<ReconciliationState, string> = {
  unmatched: "Evidence kept; not enough information to match an asset or port.",
  ambiguous: "More than one candidate, or the evidence disagrees. Nothing was chosen.",
  proposed: "Both ends resolved deterministically. Waiting for an operator to confirm.",
  conflict: "Resolvable, but it contradicts an authoritative link. The authoritative link is unchanged.",
  confirmed: "An operator linked the ports.",
  rejected: "An operator declared this evidence wrong.",
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : (error as Error).message;
}

function explanation(neighbor: Neighbor): string[] {
  const evidence = neighbor.match_evidence;
  const lines = [evidence.local?.reason, evidence.remote_device?.reason, evidence.remote_port?.reason, ...(evidence.reasons ?? [])];
  for (const conflict of evidence.conflicts ?? []) lines.push(`Conflicts with a ${conflict.kind.replace(/_/g, " ")}.`);
  for (const other of evidence.protocol_disagreement ?? []) lines.push(`${other.protocol.toUpperCase()} reports a different neighbor (${other.remote}) on this port.`);
  return lines.filter((l): l is string => Boolean(l));
}

export function NeighborReviewPage() {
  const queryClient = useQueryClient();
  const canReconcile = useHasPermission("discovery:reconcile");
  const canCable = useHasPermission("cable:manage");
  const [filters, setFilters] = useState<NeighborFilters>({ reconciliation_state: "", protocol: "" });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [localPort, setLocalPort] = useState("");
  const [remotePort, setRemotePort] = useState("");
  const [reason, setReason] = useState("");
  const [cableLabel, setCableLabel] = useState("");
  const [cableType, setCableType] = useState<CableType>("copper_utp");

  const neighbors = useQuery({ queryKey: ["network", "neighbors", filters], queryFn: () => listNeighbors(filters) });
  const selected = neighbors.data?.items.find((n) => n.id === selectedId) ?? null;
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["network"] });

  const decision = useMutation({
    mutationFn: ({ action, neighbor, explicit }: { action: "confirm" | "reject" | "revoke" | "rematch"; neighbor: Neighbor; explicit?: boolean }) => {
      const body = { reason: reason || null, ...(explicit ? { local_port_id: localPort || null, remote_port_id: remotePort || null } : {}) };
      if (action === "confirm") return confirmNeighbor(neighbor, body);
      if (action === "reject") return rejectNeighbor(neighbor, body);
      if (action === "revoke") return revokeNeighbor(neighbor, body);
      return rematchNeighbor(neighbor);
    },
    onSuccess: refresh,
  });
  const cable = useMutation({
    mutationFn: (neighbor: Neighbor) =>
      createCableFromNeighbor(neighbor.id, { label: cableLabel, cable_type: cableType, status: "installed" }),
    onSuccess: () => {
      setCableLabel("");
      refresh();
    },
  });

  const counts = (neighbors.data?.items ?? []).reduce<Record<string, number>>((acc, n) => {
    acc[n.reconciliation_state] = (acc[n.reconciliation_state] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Discovered Neighbors</h1>
      <p className="mb-4 max-w-3xl text-sm text-slate-400">
        LLDP/CDP evidence reported by collectors. This is not authoritative topology: nothing here changes a port or cable until an
        operator confirms it, and a confirmed adjacency still becomes a physical cable only by an explicit action.
      </p>

      <div className="mb-3 flex flex-wrap items-end gap-3 text-xs">
        <label className="text-slate-400">
          State
          <select
            aria-label="Filter by state"
            value={filters.reconciliation_state}
            onChange={(e) => setFilters((f) => ({ ...f, reconciliation_state: e.target.value as ReconciliationState | "" }))}
            className="ml-2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100"
          >
            <option value="">All</option>
            {(Object.keys(STATE_STYLE) as ReconciliationState[]).map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        <label className="text-slate-400">
          Protocol
          <select
            aria-label="Filter by protocol"
            value={filters.protocol}
            onChange={(e) => setFilters((f) => ({ ...f, protocol: e.target.value as "lldp" | "cdp" | "" }))}
            className="ml-2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100"
          >
            <option value="">Both</option>
            <option value="lldp">LLDP</option>
            <option value="cdp">CDP</option>
          </select>
        </label>
        <p className="text-slate-400" aria-label="State counts">
          {(["ambiguous", "unmatched", "proposed", "conflict"] as const).map((s) => `${s}: ${counts[s] ?? 0}`).join(" · ")}
        </p>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="overflow-x-auto rounded-sm border border-slate-800 bg-slate-900 lg:col-span-2">
          <table className="w-full text-left text-xs">
            <thead className="border-b border-slate-800 text-slate-400">
              <tr>
                <th className="p-2">Observed on</th>
                <th className="p-2">Local port</th>
                <th className="p-2">Remote device</th>
                <th className="p-2">Remote port</th>
                <th className="p-2">Proto</th>
                <th className="p-2">State</th>
              </tr>
            </thead>
            <tbody>
              {neighbors.data?.items.map((n) => (
                <tr
                  key={n.id}
                  onClick={() => setSelectedId(n.id)}
                  className={`cursor-pointer border-b border-slate-800/50 hover:bg-slate-800/60 ${selectedId === n.id ? "bg-slate-800" : ""}`}
                >
                  <td className="p-2">{n.integration_name ?? n.integration_id}</td>
                  <td className="p-2 font-mono">{n.local_port_name ?? `#${n.local_port_ref}`}</td>
                  <td className="p-2">{n.remote_system_name ?? n.remote_chassis_ident}</td>
                  <td className="p-2 font-mono">{n.remote_port_ident}</td>
                  <td className="p-2 uppercase">{n.protocol}</td>
                  <td className="p-2">
                    <button onClick={() => setSelectedId(n.id)} className="text-left" aria-label={`Review neighbor ${n.remote_system_name ?? n.remote_chassis_ident} on ${n.local_port_name ?? n.local_port_ref}`}>
                      <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${STATE_STYLE[n.reconciliation_state]}`}>{n.reconciliation_state}</span>
                      {n.effective_status === "stale" && <span className="ml-1 rounded-sm bg-yellow-900 px-1.5 py-0.5 text-[10px] text-yellow-200">stale</span>}
                    </button>
                  </td>
                </tr>
              ))}
              {neighbors.data?.items.length === 0 && (
                <tr><td colSpan={6} className="p-4 text-center italic text-slate-500">No neighbors match.</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <aside aria-label="Neighbor detail" className="rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs">
          {!selected && <p className="italic text-slate-500">Select a neighbor to review its evidence.</p>}
          {selected && (
            <div className="space-y-3">
              <div>
                <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${STATE_STYLE[selected.reconciliation_state]}`}>{selected.reconciliation_state}</span>
                <p className="mt-1 text-slate-400">{STATE_HELP[selected.reconciliation_state]}</p>
                {selected.effective_status === "stale" && (
                  <p className="mt-1 text-yellow-300">Not seen recently (last seen {selected.last_seen_at}). Decisions already made are unchanged.</p>
                )}
              </div>
              <dl className="grid grid-cols-2 gap-1 text-slate-300">
                <dt className="text-slate-500">Remote chassis</dt><dd className="break-all font-mono">{selected.remote_chassis_ident}</dd>
                <dt className="text-slate-500">Remote port</dt><dd className="break-all font-mono">{selected.remote_port_ident}</dd>
                <dt className="text-slate-500">Mgmt address</dt><dd className="font-mono">{selected.remote_management_address ?? "—"}</dd>
                <dt className="text-slate-500">Platform</dt><dd>{selected.remote_platform ?? "—"}</dd>
                <dt className="text-slate-500">Capabilities</dt><dd>{selected.capabilities.join(", ") || "—"}</dd>
                <dt className="text-slate-500">Native VLAN</dt><dd>{selected.native_vlan ?? "—"}</dd>
                <dt className="text-slate-500">First seen</dt><dd>{selected.first_seen_at}</dd>
                <dt className="text-slate-500">Last seen</dt><dd>{selected.last_seen_at}</dd>
                <dt className="text-slate-500">Source</dt><dd>{selected.protocol.toUpperCase()} via collector {selected.source_collector_id ?? "—"}</dd>
              </dl>
              {explanation(selected).length > 0 && (
                <div>
                  <h2 className="mb-1 font-semibold text-slate-200">Why this state</h2>
                  <ul className="list-inside list-disc text-slate-400">
                    {explanation(selected).map((line) => <li key={line}>{line}</li>)}
                  </ul>
                </div>
              )}
              {selected.match_evidence.proposal && (
                <p className="font-mono text-slate-400">
                  Proposal: {selected.match_evidence.proposal.local_port_id} ↔ {selected.match_evidence.proposal.remote_port_id}
                </p>
              )}
              {selected.reconciliation_state === "confirmed" && (
                <p className="font-mono text-green-300">Linked: {selected.local_port_id} ↔ {selected.remote_port_id}</p>
              )}

              {canReconcile && (
                <div className="space-y-2 border-t border-slate-800 pt-2">
                  <input
                    aria-label="Decision reason"
                    placeholder="Reason (optional)"
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100"
                  />
                  {selected.reconciliation_state === "proposed" && (
                    <button
                      onClick={() => decision.mutate({ action: "confirm", neighbor: selected })}
                      className="rounded-sm bg-blue-600 px-3 py-1 text-white hover:bg-blue-500"
                    >
                      Confirm proposal
                    </button>
                  )}
                  {["unmatched", "ambiguous", "conflict"].includes(selected.reconciliation_state) && (
                    <form
                      onSubmit={(e: FormEvent) => {
                        e.preventDefault();
                        decision.mutate({ action: "confirm", neighbor: selected, explicit: true });
                      }}
                      className="space-y-1"
                    >
                      <p className="text-slate-400">Resolve manually by naming both ports:</p>
                      <input aria-label="Local port ID" placeholder="Local port ID" value={localPort} onChange={(e) => setLocalPort(e.target.value)}
                        className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 font-mono text-slate-100" />
                      <input aria-label="Remote port ID" placeholder="Remote port ID" value={remotePort} onChange={(e) => setRemotePort(e.target.value)}
                        className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 font-mono text-slate-100" />
                      <button type="submit" disabled={!localPort || !remotePort} className="rounded-sm bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-50">
                        Confirm with these ports
                      </button>
                    </form>
                  )}
                  <div className="flex gap-2">
                    {["unmatched", "ambiguous", "proposed", "conflict"].includes(selected.reconciliation_state) && (
                      <>
                        <button onClick={() => decision.mutate({ action: "rematch", neighbor: selected })} className="rounded-sm bg-slate-700 px-3 py-1 text-slate-100 hover:bg-slate-600">Re-evaluate</button>
                        <button onClick={() => decision.mutate({ action: "reject", neighbor: selected })} className="rounded-sm bg-slate-700 px-3 py-1 text-slate-100 hover:bg-slate-600">Reject</button>
                      </>
                    )}
                    {["confirmed", "rejected"].includes(selected.reconciliation_state) && (
                      <button onClick={() => decision.mutate({ action: "revoke", neighbor: selected })} className="rounded-sm bg-slate-700 px-3 py-1 text-slate-100 hover:bg-slate-600">Revoke decision</button>
                    )}
                  </div>
                  {decision.isError && <p role="alert" className="text-red-400">{errorText(decision.error)}</p>}
                </div>
              )}

              {canReconcile && canCable && selected.reconciliation_state === "confirmed" && (
                <form
                  aria-label="Record physical cable"
                  onSubmit={(e: FormEvent) => {
                    e.preventDefault();
                    cable.mutate(selected);
                  }}
                  className="space-y-1 border-t border-slate-800 pt-2"
                >
                  <h2 className="font-semibold text-slate-200">Record the physical cable</h2>
                  <p className="text-slate-400">Confirming did not create a cable. Do that explicitly here.</p>
                  <input aria-label="Cable label" placeholder="Cable label" value={cableLabel} onChange={(e) => setCableLabel(e.target.value)}
                    className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100" />
                  <select aria-label="Cable type" value={cableType} onChange={(e) => setCableType(e.target.value as CableType)}
                    className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100">
                    {CABLE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </select>
                  <button type="submit" disabled={!cableLabel || cable.isPending} className="rounded-sm bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-50">
                    Record cable
                  </button>
                  {cable.isError && <p role="alert" className="text-red-400">{errorText(cable.error)}</p>}
                  {cable.isSuccess && <p role="status" className="text-green-300">Cable {cable.data.label} recorded.</p>}
                </form>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
