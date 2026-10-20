import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";

import { getEquipmentPorts, listEquipment } from "@/features/equipment/api";
import { useHasPermission } from "@/features/auth/useAuthorization";
import { createPassThrough, deletePassThrough, listPassThroughs, PassThrough } from "@/features/network/api";
import { ApiError } from "@/lib/apiClient";

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : (error as Error).message;
}

export function PassThroughsPage() {
  const queryClient = useQueryClient();
  const canManage = useHasPermission("cable:manage");
  const [equipmentId, setEquipmentId] = useState("");
  const [portA, setPortA] = useState("");
  const [portB, setPortB] = useState("");
  const [label, setLabel] = useState("");

  const items = useQuery({ queryKey: ["network", "pass-throughs"], queryFn: () => listPassThroughs() });
  const equipment = useQuery({ queryKey: ["equipment", "list"], queryFn: listEquipment, enabled: canManage });
  const ports = useQuery({
    queryKey: ["equipment", equipmentId, "ports"],
    queryFn: () => getEquipmentPorts(equipmentId),
    enabled: equipmentId !== "",
  });
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["network"] });
  const create = useMutation({
    mutationFn: () => createPassThrough({ port_a_id: portA, port_b_id: portB, label: label || null }),
    onSuccess: () => {
      setPortA("");
      setPortB("");
      setLabel("");
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: (item: PassThrough) => deletePassThrough(item), onSuccess: refresh });

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Pass-throughs</h1>
      <p className="mb-4 max-w-3xl text-sm text-slate-400">
        How a signal continues inside a device, for example patch panel front 01 to rear 01. The trace follows these between cables to
        build an end-to-end path. Each port belongs to at most one pass-through.
      </p>

      <div className="mb-6 overflow-x-auto rounded-sm border border-slate-800 bg-slate-900">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-slate-800 text-slate-400">
            <tr>
              <th className="p-2">Device</th>
              <th className="p-2">Label</th>
              <th className="p-2">Ports</th>
              <th className="p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {items.data?.items.map((item) => (
              <tr key={item.id} className="border-b border-slate-800/50">
                <td className="p-2">{item.equipment_hostname ?? item.ports[0]?.equipment_asset_tag}</td>
                <td className="p-2 font-mono">{item.label ?? "—"}</td>
                <td className="p-2 font-mono">{item.ports.map((p) => p.port_name).join(" ↔ ")}</td>
                <td className="space-x-1 p-2">
                  {item.ports[0] && (
                    <Link
                      to={`/topology/trace?port=${item.ports[0].port_id}`}
                      aria-label={`Trace from ${item.ports[0].port_name}`}
                      className="rounded-sm bg-slate-700 px-2 py-0.5 text-[10px] text-slate-100 hover:bg-slate-600"
                    >
                      Trace
                    </Link>
                  )}
                  {canManage && (
                    <button
                      onClick={() => remove.mutate(item)}
                      aria-label={`Delete pass-through ${item.label ?? item.ports.map((p) => p.port_name).join(" ")}`}
                      className="rounded-sm bg-slate-700 px-2 py-0.5 text-[10px] text-slate-100 hover:bg-slate-600"
                    >
                      Delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {items.data?.items.length === 0 && (
              <tr><td colSpan={4} className="p-4 text-center italic text-slate-500">No pass-throughs recorded.</td></tr>
            )}
          </tbody>
        </table>
        {remove.isError && <p role="alert" className="p-2 text-xs text-red-400">{errorText(remove.error)}</p>}
      </div>

      {canManage && (
        <form
          aria-label="Record a pass-through"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            create.mutate();
          }}
          className="grid max-w-3xl grid-cols-1 gap-3 rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs sm:grid-cols-2"
        >
          <h2 className="font-semibold text-slate-200 sm:col-span-2">Record a pass-through</h2>
          <label className="text-slate-400 sm:col-span-2">Device
            <select aria-label="Pass-through device" value={equipmentId}
              onChange={(e) => { setEquipmentId(e.target.value); setPortA(""); setPortB(""); }}
              className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100">
              <option value="">Select equipment…</option>
              {equipment.data?.items.map((item) => (
                <option key={item.id} value={item.id}>{item.hostname ?? item.asset_tag} ({item.asset_tag})</option>
              ))}
            </select>
          </label>
          {([["Port A", portA, setPortA], ["Port B", portB, setPortB]] as const).map(([name, value, setter]) => (
            <label key={name} className="text-slate-400">{name}
              <select aria-label={`Pass-through ${name.toLowerCase()}`} value={value} disabled={equipmentId === ""}
                onChange={(e) => setter(e.target.value)}
                className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100">
                <option value="">Select port…</option>
                {ports.data?.ports.map((port) => <option key={port.id} value={port.id}>{port.display_name}</option>)}
              </select>
            </label>
          ))}
          <label className="text-slate-400 sm:col-span-2">Label (optional)
            <input value={label} onChange={(e) => setLabel(e.target.value)} maxLength={64}
              className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100" />
          </label>
          <div className="sm:col-span-2">
            <button type="submit" disabled={!portA || !portB || portA === portB || create.isPending}
              className="rounded-sm bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-50">Record pass-through</button>
            {create.isError && <p role="alert" className="mt-1 text-red-400">{errorText(create.error)}</p>}
          </div>
        </form>
      )}
    </div>
  );
}
