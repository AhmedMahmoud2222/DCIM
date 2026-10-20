import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  AlarmRule,
  DisableRuleInput,
  ResolveRuleUnitInput,
  disableAlarmRule,
  listAlarmRules,
  resolveAlarmRuleUnit,
} from "@/features/telemetry/alarmRulesApi";
import { useHasPermission } from "@/features/auth/useAuthorization";
import { ApiError } from "@/lib/apiClient";

// Issue #128 / G8: operator panel for alarm rules that need attention — legacy rules stored
// without an explicit threshold unit (unit_status "unresolved") whose evaluation is blocked
// until an operator resolves a unit or disables the rule. Actions call the existing
// /alarms/rules/{id}/resolve-unit and /disable endpoints; the backend enforces alarm:read /
// alarm:manage, optimistic concurrency (expected_updated_at) and audit logging regardless of
// what this panel hides.
const UNIT_STATUS_LABEL: Record<AlarmRule["unit_status"], string> = {
  unresolved: "Needs unit",
  invalid_contract: "Invalid contract",
  inferred_legacy: "Inferred (legacy)",
  explicit: "Explicit",
};

const UNIT_STATUS_BADGE: Record<AlarmRule["unit_status"], string> = {
  unresolved: "bg-amber-900 text-amber-100",
  invalid_contract: "bg-red-900 text-red-100",
  inferred_legacy: "bg-slate-700 text-slate-200",
  explicit: "bg-green-800 text-green-100",
};

interface ResolveFormProps {
  rule: AlarmRule;
  onResolved: () => void;
  onCancel: () => void;
}

const errorText = (error: unknown) =>
  error instanceof ApiError ? `${error.title}: ${error.detail}` : error instanceof Error ? error.message : "Request failed";

function ResolveForm({ rule, onResolved, onCancel }: ResolveFormProps) {
  const [unit, setUnit] = useState(rule.unit ?? "");
  const [reason, setReason] = useState("");
  const resolveMutation = useMutation({
    mutationFn: (input: ResolveRuleUnitInput) => resolveAlarmRuleUnit(rule.id, input),
    onSuccess: onResolved,
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    resolveMutation.mutate({
      unit: unit.trim(),
      reason: reason.trim(),
      expected_updated_at: rule.updated_at,
    });
  }

  return (
    <form onSubmit={submit} className="space-y-3 border-t border-slate-800 pt-3" aria-label={`Resolve unit for ${rule.name}`}>
      <label className="block text-xs text-slate-400">
        Threshold unit
        <input
          aria-label="Threshold unit"
          required
          maxLength={32}
          value={unit}
          onChange={(event) => setUnit(event.target.value)}
          className="mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
          placeholder="e.g. kW, %, °C"
        />
      </label>
      <label className="block text-xs text-slate-400">
        Reason for resolution
        <textarea
          aria-label="Reason for resolution"
          required
          maxLength={2048}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          className="mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
          placeholder="Why this unit is correct for this rule"
        />
      </label>
      {resolveMutation.isError && (
        <p className="text-sm text-red-400" role="alert">
          {errorText(resolveMutation.error)}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={resolveMutation.isPending}
          className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {resolveMutation.isPending ? "Resolving…" : "Save unit"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-sm border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
        >
          Cancel
        </button>
      </div>
      {resolveMutation.isSuccess && (
        <p className="text-sm text-green-400" role="status">
          Unit resolved. Evaluation resumes on the next reading; the blocked window is not replayed.{" "}
          {resolveMutation.data.retained_blocked_window_readings} reading(s) retained during the block.
        </p>
      )}
    </form>
  );
}

interface DisableFormProps {
  rule: AlarmRule;
  onDisabled: () => void;
  onCancel: () => void;
}

function DisableForm({ rule, onDisabled, onCancel }: DisableFormProps) {
  const [reason, setReason] = useState("");
  const disableMutation = useMutation({
    mutationFn: (input: DisableRuleInput) => disableAlarmRule(rule.id, input),
    onSuccess: onDisabled,
  });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    disableMutation.mutate({ reason: reason.trim(), expected_updated_at: rule.updated_at });
  }

  return (
    <form onSubmit={submit} className="space-y-3 border-t border-slate-800 pt-3" aria-label={`Disable ${rule.name}`}>
      <p className="text-sm text-slate-400">
        Disabling stops this rule from evaluating future readings. Existing alarms are preserved.
      </p>
      <label className="block text-xs text-slate-400">
        Reason for disabling
        <textarea
          aria-label="Reason for disabling"
          required
          maxLength={2048}
          value={reason}
          onChange={(event) => setReason(event.target.value)}
          className="mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
          placeholder="Why this rule should stop evaluating"
        />
      </label>
      {disableMutation.isError && (
        <p className="text-sm text-red-400" role="alert">
          {errorText(disableMutation.error)}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={disableMutation.isPending}
          className="rounded-sm bg-red-800 px-3 py-1.5 text-sm font-medium text-red-100 hover:bg-red-700 disabled:opacity-50"
        >
          {disableMutation.isPending ? "Disabling…" : "Disable rule"}
        </button>
        <button
          type="button"
          onClick={onCancel}
          className="rounded-sm border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:bg-slate-800"
        >
          Cancel
        </button>
      </div>
      {disableMutation.isSuccess && <p className="text-sm text-green-400">Rule disabled.</p>}
    </form>
  );
}

export function AlarmRulesPage() {
  const queryClient = useQueryClient();
  const canRead = useHasPermission("alarm:read");
  const canManage = useHasPermission("alarm:manage");
  const [attentionOnly, setAttentionOnly] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [action, setAction] = useState<"resolve" | "disable" | null>(null);

  const rulesQuery = useQuery({
    queryKey: ["alarm-rules", attentionOnly],
    queryFn: () => listAlarmRules({ needsAttention: attentionOnly }),
    enabled: canRead,
  });

  const rules = useMemo(() => rulesQuery.data ?? [], [rulesQuery.data]);
  const selected = rules.find((rule) => rule.id === selectedId) ?? rules[0] ?? null;

  function handleSettled() {
    queryClient.invalidateQueries({ queryKey: ["alarm-rules"] });
    setAction(null);
  }

  if (!canRead) {
    return (
      <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-center text-sm text-slate-500">
        You do not have permission to view alarm rules.
      </div>
    );
  }

  return (
    <div>
      <div className="mb-4">
        <h1 className="mb-1 text-lg font-semibold">Alarm Rules</h1>
        <p className="text-sm text-slate-500">
          Rules needing attention are legacy rules stored without an explicit threshold unit. Their evaluation is blocked until
          a unit is resolved or the rule is disabled; nothing is auto-inferred or replayed.
        </p>
      </div>

      <div className="mb-4 flex items-center gap-3 rounded-sm border border-slate-800 bg-slate-900 p-3">
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={attentionOnly}
            onChange={(event) => {
              setAttentionOnly(event.target.checked);
              setSelectedId(null);
              setAction(null);
            }}
          />
          Needs attention only
        </label>
        {rulesQuery.isLoading && <span className="text-sm text-slate-500">Loading rules…</span>}
      </div>

      {rulesQuery.isError && (
        <div className="rounded-sm border border-red-900 bg-red-950/40 p-4 text-sm text-red-300" role="alert">
          Unable to retrieve alarm rules. Your permissions may not allow this view.
        </div>
      )}

      {!rulesQuery.isLoading && !rulesQuery.isError && rules.length === 0 && (
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-center text-sm text-slate-500">
          {attentionOnly ? "No alarm rules currently need attention." : "No alarm rules found."}
        </div>
      )}

      {rules.length > 0 && (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="overflow-hidden rounded-sm border border-slate-800 bg-slate-900">
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-3 py-2">Rule</th>
                    <th className="px-3 py-2">Metric</th>
                    <th className="px-3 py-2">Status</th>
                    <th className="px-3 py-2">Blocked since</th>
                  </tr>
                </thead>
                <tbody>
                  {rules.map((rule) => (
                    <tr
                      key={rule.id}
                      onClick={() => {
                        setSelectedId(rule.id);
                        setAction(null);
                      }}
                      className={`cursor-pointer border-t border-slate-800 ${selected?.id === rule.id ? "bg-slate-800/70" : "hover:bg-slate-800/40"}`}
                    >
                      <td className="max-w-[260px] px-3 py-2 font-medium text-slate-200">
                        {rule.name}
                        <p className="mt-0.5 font-mono text-[11px] text-slate-500">{rule.id.slice(0, 8)}…</p>
                      </td>
                      <td className="px-3 py-2">
                        {rule.metric}
                        <span className="text-slate-500"> · {rule.rule_type}</span>
                      </td>
                      <td className="px-3 py-2">
                        <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${UNIT_STATUS_BADGE[rule.unit_status]}`}>
                          {UNIT_STATUS_LABEL[rule.unit_status]}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-3 py-2 text-slate-400">
                        {rule.blocked_notified_at ? new Date(rule.blocked_notified_at).toLocaleString() : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {selected && (
            <aside className="rounded-sm border border-slate-800 bg-slate-900 p-4" aria-label="Rule detail">
              <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Rule detail</p>
              <div className="mt-3 flex items-center gap-2">
                <span className={`rounded-sm px-1.5 py-0.5 text-[10px] ${UNIT_STATUS_BADGE[selected.unit_status]}`}>
                  {UNIT_STATUS_LABEL[selected.unit_status]}
                </span>
                <span className="text-sm text-slate-400">
                  {selected.threshold !== null ? `${selected.rule_type} ${selected.threshold}` : selected.rule_type}
                </span>
              </div>
              <h2 className="mt-3 wrap-break-word text-lg font-semibold text-slate-100">{selected.name}</h2>
              <dl className="mt-4 space-y-2 text-sm">
                <div>
                  <dt className="text-slate-500">Metric</dt>
                  <dd>{selected.metric}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Stored unit</dt>
                  <dd>{selected.unit ?? "none (evaluation blocked)"}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Registry version</dt>
                  <dd className="font-mono text-xs">{selected.registry_version ?? "legacy (unversioned)"}</dd>
                </div>
                <div>
                  <dt className="text-slate-500">Integration</dt>
                  <dd className="break-all font-mono text-xs">{selected.integration_id}</dd>
                </div>
                {selected.unit_resolution_reason && (
                  <div>
                    <dt className="text-slate-500">Resolution note</dt>
                    <dd>{selected.unit_resolution_reason}</dd>
                  </div>
                )}
              </dl>

              {canManage && selected.needs_attention && action === null && (
                <div className="mt-4 flex flex-col gap-2">
                  <button
                    type="button"
                    onClick={() => setAction("resolve")}
                    className="w-full rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
                  >
                    Resolve threshold unit
                  </button>
                  <button
                    type="button"
                    onClick={() => setAction("disable")}
                    className="w-full rounded-sm bg-red-800 px-3 py-1.5 text-sm font-medium text-red-100 hover:bg-red-700"
                  >
                    Disable rule
                  </button>
                </div>
              )}
              {canManage && !selected.needs_attention && (
                <p className="mt-4 text-sm text-slate-500">This rule has a resolved unit and is evaluating normally.</p>
              )}
              {canManage && action === "resolve" && (
                <ResolveForm rule={selected} onResolved={handleSettled} onCancel={() => setAction(null)} />
              )}
              {canManage && action === "disable" && (
                <DisableForm rule={selected} onDisabled={handleSettled} onCancel={() => setAction(null)} />
              )}
            </aside>
          )}
        </div>
      )}
    </div>
  );
}
