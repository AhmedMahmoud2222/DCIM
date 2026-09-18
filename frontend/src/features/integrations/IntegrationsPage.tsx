import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { createIntegration, listIntegrations, updateIntegration } from "@/features/integrations/api";
import { ApiError } from "@/lib/apiClient";

function fmtDate(iso: string | null): string {
  return iso ? new Date(iso).toLocaleString() : "—";
}

export function IntegrationsPage() {
  const queryClient = useQueryClient();
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [name, setName] = useState("");
  const [integrationType, setIntegrationType] = useState("icmp");
  const [targetHost, setTargetHost] = useState("");
  const [credential, setCredential] = useState("");

  const integrationsQuery = useQuery({ queryKey: ["integrations"], queryFn: listIntegrations });

  const createMutation = useMutation({
    mutationFn: () =>
      createIntegration({
        name,
        integration_type: integrationType,
        target_host: targetHost,
        credential: credential || undefined,
      }),
    onSuccess: () => {
      setShowCreateForm(false);
      setName("");
      setTargetHost("");
      setCredential("");
      queryClient.invalidateQueries({ queryKey: ["integrations"] });
    },
  });

  const toggleMutation = useMutation({
    mutationFn: ({ id, enabled, version }: { id: string; enabled: boolean; version: number }) =>
      updateIntegration(id, { enabled: !enabled }, version),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["integrations"] }),
  });

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Integrations</h1>
      <p className="mb-6 max-w-2xl text-sm text-slate-400">
        An Integration describes a device or device group a collector polls (protocol, target, credentials). Credentials
        are write-only — never returned by this API once stored.
      </p>

      <div className="mb-4">
        <button
          onClick={() => setShowCreateForm((v) => !v)}
          className="rounded bg-slate-800 px-3 py-1.5 text-xs text-slate-200 hover:bg-slate-700"
        >
          + New Integration
        </button>
      </div>

      {showCreateForm && (
        <form
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            createMutation.mutate();
          }}
          className="mb-4 max-w-md space-y-2 rounded border border-slate-700 bg-slate-800/50 p-3"
        >
          <input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Integration name"
            required
            className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <select
            value={integrationType}
            onChange={(e) => setIntegrationType(e.target.value)}
            className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          >
            <option value="icmp">icmp</option>
            <option value="snmp">snmp</option>
            <option value="rest">rest</option>
          </select>
          <input
            value={targetHost}
            onChange={(e) => setTargetHost(e.target.value)}
            placeholder="Target host (IP or hostname)"
            required
            className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <input
            value={credential}
            onChange={(e) => setCredential(e.target.value)}
            placeholder="Credential (optional, e.g. SNMP community string)"
            type="password"
            className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <button
            type="submit"
            disabled={createMutation.isPending}
            className="w-full rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            Create
          </button>
          {createMutation.isError && (
            <p className="text-xs text-red-400">
              {createMutation.error instanceof ApiError ? createMutation.error.detail : (createMutation.error as Error).message}
            </p>
          )}
        </form>
      )}

      <div className="overflow-x-auto rounded border border-slate-800 bg-slate-900">
        <table className="w-full text-left text-xs">
          <thead className="border-b border-slate-800 text-slate-400">
            <tr>
              <th className="p-2">Name</th>
              <th className="p-2">Type</th>
              <th className="p-2">Target</th>
              <th className="p-2">Enabled</th>
              <th className="p-2">Last poll</th>
              <th className="p-2">Last success</th>
              <th className="p-2">Failures</th>
              <th className="p-2">Credential</th>
              <th className="p-2"></th>
            </tr>
          </thead>
          <tbody>
            {integrationsQuery.data?.map((i) => (
              <tr key={i.id} className="border-b border-slate-800/50">
                <td className="p-2">{i.name}</td>
                <td className="p-2">{i.integration_type}</td>
                <td className="p-2">{i.target_host}</td>
                <td className="p-2">
                  <span className={i.enabled ? "text-green-400" : "text-slate-500"}>{i.enabled ? "enabled" : "disabled"}</span>
                </td>
                <td className="p-2">{fmtDate(i.last_poll_at)}</td>
                <td className="p-2">{fmtDate(i.last_success_at)}</td>
                <td className="p-2">{i.consecutive_failures}</td>
                <td className="p-2">{i.has_credential ? "set" : "—"}</td>
                <td className="p-2">
                  <button
                    onClick={() => toggleMutation.mutate({ id: i.id, enabled: i.enabled, version: i.version })}
                    className="rounded bg-slate-800 px-2 py-0.5 text-[10px] text-slate-200 hover:bg-slate-700"
                  >
                    {i.enabled ? "Disable" : "Enable"}
                  </button>
                </td>
              </tr>
            ))}
            {integrationsQuery.data?.length === 0 && (
              <tr>
                <td colSpan={9} className="p-4 text-center italic text-slate-500">
                  No integrations configured yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
