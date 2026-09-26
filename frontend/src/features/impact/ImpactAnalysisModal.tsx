import { useMutation } from "@tanstack/react-query";
import { KeyboardEvent, useEffect, useRef } from "react";

import { simulateImpact } from "@/features/impact/api";
import { ApiError } from "@/lib/apiClient";
import { ImpactedEquipmentItem, ImpactSimulationResult } from "@/types";

export interface ImpactTarget {
  type: "power_node" | "network_port";
  id: string;
  label: string;
}

const IMPACT_TYPE_COLORS: Record<string, string> = {
  power_loss: "text-red-400",
  network_isolated: "text-red-400",
  degraded_redundancy: "text-yellow-400",
  network_degraded: "text-yellow-400",
};

/** Phase 10C: interactive "what-if" blast-radius visualizer. Runs POST /impact/simulate
 * for `target` as soon as it's set and renders directly/indirectly impacted equipment,
 * lost-redundancy narratives, and affected services. `onResult` lets the caller
 * (RackElevationView) highlight the affected slots on the elevation itself while this
 * stays open — cleared (called with null) on close/unmount. */
export function ImpactAnalysisModal({
  target,
  onClose,
  onResult,
}: {
  target: ImpactTarget | null;
  onClose: () => void;
  onResult?: (result: ImpactSimulationResult | null) => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const mutation = useMutation({
    mutationFn: (t: ImpactTarget) => simulateImpact({ target_type: t.type, target_id: t.id }),
  });

  useEffect(() => {
    if (target) mutation.mutate(target);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.type, target?.id]);

  useEffect(() => {
    onResult?.(mutation.data ?? null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mutation.data]);

  useEffect(
    () => () => onResult?.(null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  useEffect(() => {
    if (!target) return;
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    return () => previousFocus?.focus();
  }, [target]);

  function handleDialogKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    ) ?? []);
    if (!controls.length) {
      event.preventDefault();
      return;
    }
    if (event.shiftKey && document.activeElement === controls[0]) {
      event.preventDefault();
      controls[controls.length - 1].focus();
    } else if (!event.shiftKey && document.activeElement === controls[controls.length - 1]) {
      event.preventDefault();
      controls[0].focus();
    }
  }

  if (!target) return null;
  const result = mutation.data;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="impact-dialog-title"
        onKeyDown={handleDialogKeyDown}
        data-testid="impact-analysis-modal"
        className="max-h-[85vh] w-full max-w-2xl overflow-auto rounded border border-slate-700 bg-slate-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 id="impact-dialog-title" className="text-base font-semibold text-slate-100">Simulate failure: {target.label}</h2>
          <button ref={closeButtonRef} onClick={onClose} className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700">
            Close
          </button>
        </div>

        {mutation.isPending && <p role="status" className="text-sm text-slate-400">Running simulation…</p>}
        {mutation.isError && (
          <p role="alert" className="text-sm text-red-400">
            {mutation.error instanceof ApiError ? mutation.error.detail : "Simulation failed."}
          </p>
        )}

        {result && (
          <div role="status" aria-label="Simulation results">
            {result.directly_impacted.length === 0 && result.indirectly_impacted.length === 0 ? (
              <p className="text-sm italic text-slate-500">No modeled equipment is affected by this failure.</p>
            ) : (
              <>
                <ImpactSection title="Directly impacted" items={result.directly_impacted} />
                <ImpactSection title="Indirectly impacted" items={result.indirectly_impacted} />
              </>
            )}
            {result.lost_redundancy_paths.length > 0 && (
              <div data-testid="impact-lost-redundancy" className="mt-4 rounded border border-yellow-800 bg-yellow-950/40 p-3">
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-yellow-400">Lost redundancy</h3>
                <ul className="space-y-1 text-sm text-yellow-200">
                  {result.lost_redundancy_paths.map((path) => (
                    <li key={path}>{path}</li>
                  ))}
                </ul>
              </div>
            )}
            {result.affected_services.length > 0 && (
              <div className="mt-4">
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-400">Affected services</h3>
                <div className="flex flex-wrap gap-1.5">
                  {result.affected_services.map((service) => (
                    <span key={service} className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-200">
                      {service}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function ImpactSection({ title, items }: { title: string; items: ImpactedEquipmentItem[] }) {
  if (items.length === 0) return null;
  return (
    <div className="mb-4">
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">{title}</h3>
      <div className="space-y-1.5">
        {items.map((item) => (
          <div key={item.equipment_id} data-testid="impact-item" className="rounded bg-slate-800/60 p-2 text-sm">
            <div className="flex items-center justify-between">
              <span className="font-medium text-slate-200">{item.hostname ?? item.asset_tag}</span>
              <span className={`text-xs font-semibold uppercase ${IMPACT_TYPE_COLORS[item.impact_type] ?? "text-slate-400"}`}>
                {item.impact_type.replace(/_/g, " ")}
              </span>
            </div>
            <p className="mt-1 text-xs text-slate-400">{item.message}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
