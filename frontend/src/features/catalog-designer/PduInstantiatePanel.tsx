import { useMutation, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { instantiatePdu, PDUInstantiateInput } from "@/features/power/api";
import { ApiError } from "@/lib/apiClient";
import { PDUInstantiatedAsset } from "@/types";

function newIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `pdu-inst-${Date.now()}-${Math.floor(Math.random() * 1e9)}`;
}

const inputClass = "w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100";

/** Issue #150 M5 — instantiate a PDU from a published revision
 * (`POST /power/pdus/instantiate`, `power:manage`). The electrical values and the outlet
 * set are derived server-side from the published revision, so this form only collects
 * asset identity + management protocol. A stable `Idempotency-Key` is reused across
 * retries of the same submission and rotated after success, so a network-flaked double
 * submit cannot create two PDUs. */
export function PduInstantiatePanel({ revisionId }: { revisionId: string }) {
  const queryClient = useQueryClient();
  const [assetTag, setAssetTag] = useState("");
  const [name, setName] = useState("");
  const [ipAddress, setIpAddress] = useState("");
  const [protocol, setProtocol] = useState<"snmp" | "rest" | "none">("none");
  const [result, setResult] = useState<PDUInstantiatedAsset | null>(null);
  const idempotencyKeyRef = useRef<string | null>(null);

  const instantiateMutation = useMutation({
    mutationFn: (body: PDUInstantiateInput) => {
      if (idempotencyKeyRef.current === null) idempotencyKeyRef.current = newIdempotencyKey();
      return instantiatePdu(body, idempotencyKeyRef.current);
    },
    onSuccess: (created) => {
      idempotencyKeyRef.current = null;
      setResult(created);
      setAssetTag("");
      setName("");
      setIpAddress("");
      // Outlet nodes and their connections live under the same ["power"] key family
      // the topology page observes.
      queryClient.invalidateQueries({ queryKey: ["power"] });
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    instantiateMutation.mutate({
      asset_tag: assetTag.trim(),
      name: name.trim(),
      catalog_model_revision_id: revisionId,
      ip_address: ipAddress.trim() === "" ? null : ipAddress.trim(),
      protocol,
    });
  }

  function resetResult() {
    setResult(null);
    instantiateMutation.reset();
  }

  return (
    <section className="mb-6 rounded-sm border border-slate-800 bg-slate-900 p-4" aria-label="Instantiate PDU">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Instantiate PDU</h2>
        {result && (
          <button
            type="button"
            onClick={resetResult}
            className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700"
          >
            Instantiate another
          </button>
        )}
      </div>

      {!result && (
        <>
          <p className="mb-3 text-xs text-slate-400">
            Creates a PDU asset, its <code className="text-slate-300">pdu</code> power node and one{" "}
            <code className="text-slate-300">pdu_outlet</code> node per published outlet template — rated input
            voltage/current, phase and outlets come from this published revision, never from this form.
          </p>
          <form onSubmit={handleSubmit} className="space-y-2">
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
              <label className="block text-[11px] text-slate-400">
                Asset tag
                <input
                  value={assetTag}
                  onChange={(e) => setAssetTag(e.target.value)}
                  required
                  maxLength={64}
                  aria-label="Asset tag"
                  className={inputClass}
                />
              </label>
              <label className="block text-[11px] text-slate-400">
                Name
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  maxLength={128}
                  aria-label="Name"
                  className={inputClass}
                />
              </label>
              <label className="block text-[11px] text-slate-400">
                IP address (optional)
                <input
                  value={ipAddress}
                  onChange={(e) => setIpAddress(e.target.value)}
                  maxLength={45}
                  aria-label="IP address"
                  className={inputClass}
                />
              </label>
              <label className="block text-[11px] text-slate-400">
                Management protocol
                <select
                  value={protocol}
                  onChange={(e) => setProtocol(e.target.value as "snmp" | "rest" | "none")}
                  aria-label="Management protocol"
                  className={inputClass}
                >
                  <option value="none">none</option>
                  <option value="snmp">snmp</option>
                  <option value="rest">rest</option>
                </select>
              </label>
            </div>
            <button
              type="submit"
              disabled={instantiateMutation.isPending}
              className="rounded-sm bg-green-700 px-3 py-1.5 text-xs font-medium text-white hover:bg-green-600 disabled:opacity-50"
            >
              {instantiateMutation.isPending ? "Instantiating…" : "Instantiate PDU"}
            </button>
            {instantiateMutation.isError && (
              <p className="text-xs text-red-400" role="alert">
                {instantiateMutation.error instanceof ApiError
                  ? instantiateMutation.error.detail
                  : (instantiateMutation.error as Error).message}
              </p>
            )}
          </form>
        </>
      )}

      {result && (
        <div className="space-y-3" aria-label="PDU instantiation result">
          <p className="text-sm text-green-400">
            PDU instantiated — <span className="text-slate-200">{result.label}</span> with {result.outlet_count}{" "}
            outlet{result.outlet_count === 1 ? "" : "s"}.
          </p>
          <dl className="grid grid-cols-1 gap-1 text-xs sm:grid-cols-2">
            <div>
              <dt className="text-slate-500">PDU power node</dt>
              <dd className="font-mono break-all text-slate-300">{result.id}</dd>
            </div>
            <div>
              <dt className="text-slate-500">Managed asset</dt>
              <dd className="font-mono break-all text-slate-300">{result.managed_asset_id ?? "—"}</dd>
            </div>
          </dl>
          <div>
            <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">
              Outlet power nodes ({result.outlets.length})
            </h3>
            <ul className="space-y-1 text-xs">
              {result.outlets.map((outlet) => (
                <li key={outlet.id} className="flex items-center justify-between gap-3 rounded-sm bg-slate-800/50 px-2 py-1">
                  <span className="truncate text-slate-200">{outlet.label}</span>
                  <span className="font-mono break-all text-[10px] text-slate-500">{outlet.id}</span>
                </li>
              ))}
            </ul>
          </div>
          <Link to="/power" className="inline-block text-xs text-sky-400 underline">
            Open Power Topology to connect these outlets →
          </Link>
        </div>
      )}
    </section>
  );
}
