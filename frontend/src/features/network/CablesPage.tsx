import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { useHasPermission } from "@/features/auth/useAuthorization";
import {
  Cable,
  CABLE_TYPES,
  CableStatus,
  CableType,
  createCable,
  deleteCable,
  installCable,
  listCables,
  removeCable,
} from "@/features/network/api";
import { PortPicker } from "@/features/network/PortPicker";
import { ApiError } from "@/lib/apiClient";

const STATUS_STYLE: Record<CableStatus, string> = {
  planned: "bg-amber-900 text-amber-200",
  installed: "bg-green-900 text-green-200",
  removed: "bg-slate-700 text-slate-300",
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : (error as Error).message;
}

function endpointText(cable: Cable, end: "A" | "B"): string {
  const endpoint = cable.endpoints.find((e) => e.end === end);
  if (!endpoint || endpoint.restricted || !endpoint.port) return "restricted";
  return `${endpoint.port.equipment_hostname ?? endpoint.port.equipment_asset_tag} · ${endpoint.port.port_name}`;
}

export function CablesPage() {
  const queryClient = useQueryClient();
  const canManage = useHasPermission("cable:manage");
  const [status, setStatus] = useState<CableStatus | "">("");
  const [label, setLabel] = useState("");
  const [cableType, setCableType] = useState<CableType>("copper_utp");
  const [lengthM, setLengthM] = useState("");
  const [notes, setNotes] = useState("");
  const [initial, setInitial] = useState<"planned" | "installed">("planned");
  const [a, setA] = useState({ equipmentId: "", portId: "" });
  const [b, setB] = useState({ equipmentId: "", portId: "" });

  const cables = useQuery({ queryKey: ["network", "cables", status], queryFn: () => listCables(status) });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["network"] });
  const create = useMutation({
    mutationFn: () =>
      createCable({
        label, cable_type: cableType, status: initial, length_m: lengthM || null, notes: notes || null,
        endpoint_a_port_id: a.portId, endpoint_b_port_id: b.portId,
      }),
    onSuccess: () => {
      setLabel("");
      setLengthM("");
      setNotes("");
      setA({ equipmentId: "", portId: "" });
      setB({ equipmentId: "", portId: "" });
      refresh();
    },
  });
  const action = useMutation({
    mutationFn: async ({ kind, cable }: { kind: "install" | "remove" | "delete"; cable: Cable }): Promise<void> => {
      if (kind === "install") await installCable(cable);
      else if (kind === "remove") await removeCable(cable);
      else await deleteCable(cable);
    },
    onSuccess: refresh,
  });

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Cables</h1>
      <p className="mb-4 max-w-3xl text-sm text-slate-400">
        Physical cables with identity, two fixed endpoints and a lifecycle (planned → installed → removed). Removed cables are kept as
        history. A cable realizes the logical port connection; it never replaces one it did not create.
      </p>

      <label className="mb-3 block text-xs text-slate-400">
        Status
        <select
          aria-label="Filter by status"
          value={status}
          onChange={(e) => setStatus(e.target.value as CableStatus | "")}
          className="ml-2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100"
        >
          <option value="">All</option>
          <option value="planned">planned</option>
          <option value="installed">installed</option>
          <option value="removed">removed</option>
        </select>
      </label>

      <div className="mb-6 overflow-x-auto rounded-sm border border-slate-800 bg-slate-900">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-slate-800 text-slate-400">
            <tr>
              <th className="p-2">Label</th>
              <th className="p-2">Type</th>
              <th className="p-2">End A</th>
              <th className="p-2">End B</th>
              <th className="p-2">Status</th>
              <th className="p-2">Source</th>
              <th className="p-2">Installed</th>
              <th className="p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {cables.data?.items.map((cable) => (
              <tr key={cable.id} className="border-b border-slate-800/50">
                <td className="p-2 font-mono">{cable.label}</td>
                <td className="p-2">{cable.cable_type}{cable.length_m ? ` · ${cable.length_m} m` : ""}</td>
                <td className="p-2">{endpointText(cable, "A")}</td>
                <td className="p-2">{endpointText(cable, "B")}</td>
                <td className="p-2"><span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${STATUS_STYLE[cable.status]}`}>{cable.status}</span></td>
                <td className="p-2">{cable.source === "discovery_confirmed" ? "from confirmed discovery" : cable.source}</td>
                <td className="p-2">{cable.installed_at ?? "—"}{cable.removed_at ? ` → ${cable.removed_at}` : ""}</td>
                <td className="space-x-1 p-2">
                  {cable.endpoints.find((e) => e.port)?.port && (
                    <Link
                      to={`/topology/trace?port=${cable.endpoints.find((e) => e.port)!.port!.port_id}`}
                      aria-label={`Trace cable ${cable.label}`}
                      className="rounded-sm bg-slate-700 px-2 py-0.5 text-[10px] text-slate-100 hover:bg-slate-600"
                    >
                      Trace
                    </Link>
                  )}
                  {canManage && cable.status === "planned" && (
                    <>
                      <button onClick={() => action.mutate({ kind: "install", cable })} aria-label={`Install cable ${cable.label}`}
                        className="rounded-sm bg-blue-600 px-2 py-0.5 text-[10px] text-white hover:bg-blue-500">Install</button>
                      <button onClick={() => action.mutate({ kind: "delete", cable })} aria-label={`Delete cable ${cable.label}`}
                        className="rounded-sm bg-slate-700 px-2 py-0.5 text-[10px] text-slate-100 hover:bg-slate-600">Delete</button>
                    </>
                  )}
                  {canManage && cable.status !== "removed" && (
                    <button onClick={() => action.mutate({ kind: "remove", cable })} aria-label={`Remove cable ${cable.label}`}
                      className="rounded-sm bg-slate-700 px-2 py-0.5 text-[10px] text-slate-100 hover:bg-slate-600">Remove</button>
                  )}
                </td>
              </tr>
            ))}
            {cables.data?.items.length === 0 && (
              <tr><td colSpan={8} className="p-4 text-center italic text-slate-500">No cables recorded.</td></tr>
            )}
          </tbody>
        </table>
        {action.isError && <p role="alert" className="p-2 text-xs text-red-400">{errorText(action.error)}</p>}
      </div>

      {canManage && (
        <form
          aria-label="Record a cable"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            create.mutate();
          }}
          className="grid max-w-3xl grid-cols-1 gap-3 rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs sm:grid-cols-2"
        >
          <h2 className="font-semibold text-slate-200 sm:col-span-2">Record a cable</h2>
          <label className="text-slate-400">Label
            <input value={label} onChange={(e) => setLabel(e.target.value)} required maxLength={64}
              className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100" />
          </label>
          <label className="text-slate-400">Type
            <select value={cableType} onChange={(e) => setCableType(e.target.value as CableType)}
              className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100">
              {CABLE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
            </select>
          </label>
          <PortPicker label="Endpoint A" equipmentId={a.equipmentId} portId={a.portId} onChange={setA} />
          <PortPicker label="Endpoint B" equipmentId={b.equipmentId} portId={b.portId} onChange={setB} />
          <label className="text-slate-400">Length (m)
            <input value={lengthM} onChange={(e) => setLengthM(e.target.value)} inputMode="decimal"
              className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100" />
          </label>
          <label className="text-slate-400">Initial status
            <select value={initial} onChange={(e) => setInitial(e.target.value as "planned" | "installed")}
              className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100">
              <option value="planned">planned</option>
              <option value="installed">installed</option>
            </select>
          </label>
          <label className="text-slate-400 sm:col-span-2">Notes
            <input value={notes} onChange={(e) => setNotes(e.target.value)} maxLength={2000}
              className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100" />
          </label>
          <div className="sm:col-span-2">
            <button type="submit" disabled={!label || !a.portId || !b.portId || create.isPending}
              className="rounded-sm bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-50">Record cable</button>
            {create.isError && <p role="alert" className="mt-1 text-red-400">{errorText(create.error)}</p>}
          </div>
        </form>
      )}
    </div>
  );
}
