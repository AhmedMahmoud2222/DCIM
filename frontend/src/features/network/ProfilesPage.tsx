import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import {
  createDeviceProfile,
  createVendorProfile,
  DeviceProfile,
  listDeviceMetricMappings,
  listDeviceProfiles,
  listProfileTemplates,
  listVendorProfiles,
  matchProfile,
  MatchFacts,
  MatchResult,
  retireDeviceProfile,
  retireVendorProfile,
  VendorProfile,
} from "@/features/network/api";
import { ApiError } from "@/lib/apiClient";

const MATCH_BADGE: Record<MatchResult["state"], string> = {
  matched: "bg-green-900 text-green-200",
  vendor_only: "bg-blue-900 text-blue-200",
  ambiguous: "bg-red-900 text-red-200",
  no_match: "bg-slate-700 text-slate-300",
};

function errorText(error: unknown): string {
  return error instanceof ApiError ? error.detail : (error as Error).message;
}

function Protocols({ vendor }: { vendor: VendorProfile }) {
  const lldp = vendor.neighbor_discovery.lldp?.enabled;
  const cdp = vendor.neighbor_discovery.cdp?.enabled;
  return (
    <span className="flex flex-wrap gap-1">
      {vendor.supported_protocols.map((p) => (
        <span key={p} className="rounded-sm bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">{p}</span>
      ))}
      {lldp && <span className="rounded-sm bg-emerald-900 px-1.5 py-0.5 text-[10px] text-emerald-200">LLDP</span>}
      {cdp && <span className="rounded-sm bg-emerald-900 px-1.5 py-0.5 text-[10px] text-emerald-200">CDP</span>}
    </span>
  );
}

export function ProfilesPage() {
  const queryClient = useQueryClient();
  const canManage = useHasPermission("network_profile:manage");
  const [includeRetired, setIncludeRetired] = useState(false);
  const [selectedDevice, setSelectedDevice] = useState<DeviceProfile | null>(null);
  const [facts, setFacts] = useState<MatchFacts>({ sys_object_id: "" });
  const [templateKey, setTemplateKey] = useState("");

  const vendors = useQuery({ queryKey: ["network", "vendors", includeRetired], queryFn: () => listVendorProfiles(includeRetired) });
  const devices = useQuery({ queryKey: ["network", "devices", includeRetired], queryFn: () => listDeviceProfiles(includeRetired) });
  const templates = useQuery({ queryKey: ["network", "templates"], queryFn: listProfileTemplates, enabled: canManage });
  const mappings = useQuery({
    queryKey: ["network", "mappings", selectedDevice?.id],
    queryFn: () => listDeviceMetricMappings(selectedDevice!.id),
    enabled: selectedDevice !== null,
  });

  const refresh = () => queryClient.invalidateQueries({ queryKey: ["network"] });
  const match = useMutation({ mutationFn: matchProfile });
  const retireVendor = useMutation({ mutationFn: (v: VendorProfile) => retireVendorProfile(v.id, v.version), onSuccess: refresh });
  const retireDevice = useMutation({ mutationFn: (d: DeviceProfile) => retireDeviceProfile(d.id, d.version), onSuccess: refresh });
  const createFromTemplate = useMutation({
    mutationFn: async (key: string) => {
      const template = templates.data?.find((t) => t.key === key);
      if (!template) throw new Error("Choose a template first.");
      const vendor = await createVendorProfile(template.vendor);
      for (const device of template.devices) await createDeviceProfile(vendor.id, device);
    },
    onSuccess: refresh,
  });

  const nameOf = (id: string | null) =>
    id ? (devices.data?.find((d) => d.id === id)?.name ?? vendors.data?.find((v) => v.id === id)?.name ?? id) : null;
  const result = match.data;

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Vendor &amp; Device Profiles</h1>
      <p className="mb-4 max-w-3xl text-sm text-slate-400">
        Profiles are data: sysObjectID matching, discovery OIDs, LLDP/CDP table layouts and metric mappings live here, not in
        collector code. They never hold credentials. An ambiguous match selects nothing.
      </p>

      <label className="mb-3 flex items-center gap-2 text-xs text-slate-400">
        <input type="checkbox" checked={includeRetired} onChange={(e) => setIncludeRetired(e.target.checked)} />
        Show retired profiles
      </label>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <section aria-label="Vendor profiles" className="space-y-3 lg:col-span-2">
          {vendors.data?.map((vendor) => (
            <div key={vendor.id} className="rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs">
              <div className="mb-1 flex items-center justify-between gap-2">
                <div>
                  <span className="font-semibold text-slate-100">{vendor.name}</span>{" "}
                  <span className="font-mono text-slate-500">{vendor.code}</span>{" "}
                  <span className="text-slate-500">v{vendor.version}</span>
                </div>
                <div className="flex items-center gap-2">
                  {vendor.status === "retired" && <span className="rounded-sm bg-slate-700 px-1.5 py-0.5 text-[10px]">retired</span>}
                  {canManage && vendor.status === "active" && (
                    <button
                      onClick={() => retireVendor.mutate(vendor)}
                      className="rounded-sm bg-slate-700 px-2 py-1 text-[10px] text-slate-100 hover:bg-slate-600"
                    >
                      Retire vendor {vendor.code}
                    </button>
                  )}
                </div>
              </div>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-slate-400">
                <Protocols vendor={vendor} />
                <span className="font-mono">{vendor.sys_object_id_prefixes.join(", ") || "no sysObjectID prefix"}</span>
              </div>
              <ul className="space-y-1">
                {devices.data
                  ?.filter((d) => d.vendor_profile_id === vendor.id)
                  .map((device) => (
                    <li key={device.id} className="flex items-center justify-between rounded-sm bg-slate-800/60 px-2 py-1">
                      <button className="text-left text-slate-200 hover:underline" onClick={() => setSelectedDevice(device)}>
                        {device.name} <span className="font-mono text-slate-500">{device.code}</span>
                        <span className="ml-2 text-slate-500">{device.device_class}</span>
                        {device.status === "retired" && <span className="ml-2 text-slate-500">(retired)</span>}
                      </button>
                      {canManage && device.status === "active" && (
                        <button
                          onClick={() => retireDevice.mutate(device)}
                          aria-label={`Retire device profile ${device.code}`}
                          className="rounded-sm bg-slate-700 px-2 py-0.5 text-[10px] text-slate-100 hover:bg-slate-600"
                        >
                          Retire
                        </button>
                      )}
                    </li>
                  ))}
              </ul>
            </div>
          ))}
          {vendors.data?.length === 0 && <p className="text-xs italic text-slate-500">No vendor profiles yet.</p>}
          {(retireVendor.isError || retireDevice.isError) && (
            <p role="alert" className="text-xs text-red-400">{errorText(retireVendor.error ?? retireDevice.error)}</p>
          )}

          {selectedDevice && (
            <div className="rounded-sm border border-slate-700 bg-slate-900 p-3 text-xs" aria-label="Device profile detail">
              <h2 className="mb-1 font-semibold text-slate-200">{selectedDevice.name}</h2>
              <dl className="mb-2 grid grid-cols-2 gap-1 text-slate-400">
                <dt>Firmware</dt>
                <dd>{selectedDevice.firmware_min ?? "any"} – {selectedDevice.firmware_max ?? "any"}</dd>
                <dt>Priority</dt>
                <dd>{selectedDevice.priority}</dd>
                <dt>SNMP versions</dt>
                <dd>{selectedDevice.capabilities.snmp_versions?.join(", ") || "—"}</dd>
                <dt>Match criteria</dt>
                <dd className="font-mono">
                  {selectedDevice.match_criteria.length === 0
                    ? "vendor-wide"
                    : selectedDevice.match_criteria.map((c) => `${c.field} ${c.op} ${String(c.value)}`).join("; ")}
                </dd>
              </dl>
              <h3 className="mb-1 text-slate-300">Effective metric mappings</h3>
              <ul className="font-mono text-slate-400">
                {mappings.data?.map((m) => (
                  <li key={m.id}>{m.oid} → {m.canonical_metric} ({m.unit}) ×{m.scale}</li>
                ))}
                {mappings.data?.length === 0 && <li className="italic text-slate-500">none</li>}
              </ul>
            </div>
          )}
        </section>

        <aside className="space-y-4">
          <form
            aria-label="Test profile matching"
            onSubmit={(e: FormEvent) => {
              e.preventDefault();
              match.mutate(Object.fromEntries(Object.entries(facts).filter(([, v]) => v)) as MatchFacts);
            }}
            className="space-y-2 rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs"
          >
            <h2 className="font-semibold text-slate-200">Test the matcher</h2>
            {(["sys_object_id", "sys_descr", "model", "firmware"] as const).map((field) => (
              <label key={field} className="block text-slate-400">
                {field}
                <input
                  value={facts[field] ?? ""}
                  onChange={(e) => setFacts((f) => ({ ...f, [field]: e.target.value }))}
                  className="mt-0.5 w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100"
                />
              </label>
            ))}
            <button type="submit" className="rounded-sm bg-blue-600 px-3 py-1 text-white hover:bg-blue-500">Test match</button>
            {match.isError && <p role="alert" className="text-red-400">{errorText(match.error)}</p>}
            {result && (
              <div role="status" className="space-y-1 rounded-sm bg-slate-800 p-2">
                <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${MATCH_BADGE[result.state]}`}>{result.state}</span>
                {result.device_profile_id && <p>Selected: {nameOf(result.device_profile_id)}</p>}
                {result.state === "vendor_only" && <p>Vendor: {nameOf(result.vendor_profile_id)} (no device profile fits)</p>}
                {result.state === "ambiguous" && (
                  <div>
                    <p className="text-red-300">No profile was selected. Candidates:</p>
                    <ul className="list-inside list-disc">
                      {[...result.candidate_vendor_profile_ids, ...result.candidate_device_profile_ids].map((id) => (
                        <li key={id}>{nameOf(id)}</li>
                      ))}
                    </ul>
                  </div>
                )}
                {result.reasons.map((r) => (
                  <p key={r} className="text-slate-400">{r}</p>
                ))}
              </div>
            )}
          </form>

          {canManage && (
            <div className="space-y-2 rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs">
              <h2 className="font-semibold text-slate-200">Create from template</h2>
              <select
                aria-label="Profile template"
                value={templateKey}
                onChange={(e) => setTemplateKey(e.target.value)}
                className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-slate-100"
              >
                <option value="">Select a template…</option>
                {templates.data?.map((t) => (
                  <option key={t.key} value={t.key}>{t.title}</option>
                ))}
              </select>
              <button
                disabled={!templateKey || createFromTemplate.isPending}
                onClick={() => createFromTemplate.mutate(templateKey)}
                className="rounded-sm bg-blue-600 px-3 py-1 text-white hover:bg-blue-500 disabled:opacity-50"
              >
                Create profiles
              </button>
              {createFromTemplate.isError && <p role="alert" className="text-red-400">{errorText(createFromTemplate.error)}</p>}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
