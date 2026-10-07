import { useQuery } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { TracePortView, traceFromPort, TraceResult, TraceStep, TraceTermination } from "@/features/network/api";
import { PortPicker } from "@/features/network/PortPicker";
import { ApiError } from "@/lib/apiClient";

const AGREEMENT_TEXT: Record<TraceResult["evidence"]["agreement"], { text: string; style: string }> = {
  agrees: { text: "Discovery agrees with the recorded cable", style: "bg-green-900 text-green-200" },
  disagrees: { text: "Discovery disagrees with the recorded cable", style: "bg-red-900 text-red-200" },
  no_evidence: { text: "No discovery evidence for this link", style: "bg-slate-700 text-slate-300" },
  undocumented_adjacency: { text: "Discovery sees a neighbor but no cable is recorded", style: "bg-amber-900 text-amber-200" },
};

const TERMINATION_TEXT: Record<TraceTermination, { text: string; style: string }> = {
  end_of_path: { text: "End of path: the last port has no pass-through.", style: "bg-green-900 text-green-200" },
  no_link: { text: "Open end: nothing is connected beyond this point.", style: "bg-slate-700 text-slate-300" },
  restricted: { text: "Stopped at the edge of your access scope.", style: "bg-amber-900 text-amber-200" },
  cycle_detected: { text: "Loop detected: the path returns to a port it already passed.", style: "bg-red-900 text-red-200" },
  hop_limit: { text: "Hop limit reached: the path is longer than the trace follows.", style: "bg-amber-900 text-amber-200" },
  broken_topology: { text: "The recorded topology is inconsistent here, so the trace stopped.", style: "bg-red-900 text-red-200" },
};

function LinkBox({ step }: { step: TraceStep }) {
  return (
    <div className="rounded-sm border border-dashed border-slate-600 px-3 py-2 text-center text-xs" data-testid="trace-link">
      {step.link.kind === "cable" && step.link.cable && (
        <>
          <div className="font-mono text-slate-100">{step.link.cable.label}</div>
          <div className="text-slate-400">{step.link.cable.cable_type} · {step.link.cable.status}</div>
          {step.link.cable.source === "discovery_confirmed" && <div className="text-emerald-300">from confirmed discovery</div>}
        </>
      )}
      {step.link.kind === "port_connection" && (
        <>
          <div className="text-slate-100">Logical connection</div>
          <div className="text-amber-300">{step.link.note}</div>
        </>
      )}
      {step.link.kind === "cable" && !step.link.cable && <div className="text-slate-400">Cable</div>}
    </div>
  );
}

function PortBox({ title, port }: { title: string; port: TracePortView }) {
  return (
    <div className="min-w-40 rounded-sm border border-slate-700 bg-slate-900 p-3 text-xs">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{title}</div>
      <div className="font-semibold text-slate-100">{port.equipment_hostname ?? port.equipment_asset_tag}</div>
      <div className="text-slate-400">{port.equipment_asset_tag}</div>
      <div className="mt-1 font-mono text-slate-200">{port.port_name}</div>
    </div>
  );
}

export function TracePage() {
  const [params, setParams] = useSearchParams();
  const portId = params.get("port") ?? "";
  const [picker, setPicker] = useState({ equipmentId: "", portId });
  const trace = useQuery({ queryKey: ["network", "trace", portId], queryFn: () => traceFromPort(portId), enabled: portId !== "" });
  const result = trace.data;

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Connection Trace</h1>
      <p className="mb-4 max-w-3xl text-sm text-slate-400">
        Follow a port over its cable to the far port, through any patch-panel pass-through, and on to the next cable until the path
        ends. What discovery observed is shown separately and is never merged into the recorded path.
      </p>

      <div className="mb-6 max-w-md space-y-2">
        <PortPicker label="Start port" equipmentId={picker.equipmentId} portId={picker.portId} onChange={setPicker} />
        <button
          disabled={!picker.portId}
          onClick={() => setParams({ port: picker.portId })}
          className="rounded-sm bg-blue-600 px-3 py-1 text-xs text-white hover:bg-blue-500 disabled:opacity-50"
        >
          Trace
        </button>
      </div>

      {trace.isError && (
        <p role="alert" className="text-xs text-red-400">{trace.error instanceof ApiError ? trace.error.detail : (trace.error as Error).message}</p>
      )}

      {result && (
        <div className="space-y-6">
          <section aria-label="Recorded path">
            <ol className="flex flex-wrap items-center gap-3" data-testid="trace-path">
              <li><PortBox title="From" port={result.start} /></li>
              {result.path.map((step, index) => (
                <Fragment key={index}>
                  <li data-testid="trace-step" aria-label={`Hop ${index + 1}`}><LinkBox step={step} /></li>
                  {step.hop && !step.hop.restricted && step.hop.remote && (
                    <li>
                      <PortBox title={index === result.path.length - 1 && !step.pass_through ? "To" : `Hop ${index + 1}`} port={step.hop.remote} />
                      {step.hop.cycle && <div className="mt-1 text-[10px] text-red-300">already visited</div>}
                    </li>
                  )}
                  {step.hop?.restricted && (
                    <li className="rounded-sm border border-slate-700 bg-slate-900 p-3 text-xs italic text-slate-400">
                      Remote end is outside your access scope.
                    </li>
                  )}
                  {step.pass_through && (
                    <li data-testid="trace-pass-through">
                      <div className="rounded-sm border border-slate-600 bg-slate-800 px-3 py-2 text-center text-xs">
                        <div className="text-[10px] uppercase tracking-wide text-slate-500">Passes through</div>
                        {step.pass_through.label && <div className="font-mono text-slate-100">{step.pass_through.label}</div>}
                      </div>
                    </li>
                  )}
                  {step.pass_through && (
                    <li><PortBox title="Continues on" port={step.pass_through.to} /></li>
                  )}
                </Fragment>
              ))}
            </ol>
            <p className="mt-3 flex flex-wrap items-center gap-2 text-xs" data-testid="trace-termination">
              <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${TERMINATION_TEXT[result.terminated].style}`}>
                {TERMINATION_TEXT[result.terminated].text}
              </span>
              {result.path.length === 0 && result.terminated === "no_link" && (
                <span className="italic text-slate-400">This port has no recorded cable or connection.</span>
              )}
              <span className="text-slate-500">{result.hop_count ?? result.path.length} link(s)</span>
            </p>
          </section>

          {result.previous_cables.length > 0 && (
            <section aria-label="Previous cables" className="text-xs text-slate-400">
              <h2 className="mb-1 font-semibold text-slate-300">Previously on this port</h2>
              <ul>
                {result.previous_cables.map((c) => (
                  <li key={c.id} className="font-mono">{c.label} ({c.cable_type}) removed {c.removed_at}</li>
                ))}
              </ul>
            </section>
          )}

          <section aria-label="Discovery evidence" className="rounded-sm border border-slate-800 bg-slate-900 p-3 text-xs">
            <h2 className="mb-1 font-semibold text-slate-200">Discovery evidence <span className="font-normal text-slate-500">(observed, not authoritative)</span></h2>
            <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${AGREEMENT_TEXT[result.evidence.agreement].style}`}>
              {AGREEMENT_TEXT[result.evidence.agreement].text}
            </span>
            <ul className="mt-2 space-y-1 text-slate-400">
              {result.evidence.neighbors.map((n) => (
                <li key={n.neighbor_id}>
                  {n.protocol.toUpperCase()} saw {n.remote_system_name ?? n.remote_chassis_ident} / {n.remote_port_ident} — {n.reconciliation_state}
                  {n.status === "stale" ? " (stale)" : ""}
                </li>
              ))}
              {result.evidence.neighbors.length === 0 && <li className="italic text-slate-500">Nothing observed.</li>}
            </ul>
          </section>
        </div>
      )}
    </div>
  );
}
