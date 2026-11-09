import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";

import {
  AlarmRule,
  CreateAlarmRuleInput,
  DisableRuleInput,
  EditAlarmRuleInput,
  ResolveRuleUnitInput,
  createAlarmRule,
  disableAlarmRule,
  editAlarmRule,
  listAlarmRules,
  resolveAlarmRuleUnit,
} from "@/features/telemetry/alarmRulesApi";
import { QueryStatus } from "@/components/data/QueryStatus";
import { PageHeader } from "@/components/layout/PageHeader";
import { useHasPermission } from "@/features/auth/useAuthorization";
import { listIntegrations } from "@/features/integrations/api";
import { ApiError } from "@/lib/apiClient";
import { Integration } from "@/types";

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

const METRICS = [
  { key: "temperature_c", unit: "degC" },
  { key: "humidity_percent", unit: "%" },
  { key: "power_kw", unit: "kW" },
  { key: "load_percent", unit: "%" },
  { key: "availability", unit: "1" },
  { key: "supply_air_temperature_c", unit: "degC" },
  { key: "return_air_temperature_c", unit: "degC" },
  { key: "airflow_m3_s", unit: "m3/s" },
  { key: "airflow_velocity_m_s", unit: "m/s" },
  { key: "differential_pressure_pa", unit: "Pa" },
  { key: "cooling_output_kw", unit: "kW" },
];

function CreateRuleForm({ integrations, onCreated, onCancel }: { integrations: Integration[]; onCreated: (id: string) => void; onCancel: () => void }) {
  const [name, setName] = useState("");
  const [integrationId, setIntegrationId] = useState(integrations[0]?.id ?? "");
  const [metric, setMetric] = useState("power_kw");
  const [ruleType, setRuleType] = useState("threshold_high");
  const [threshold, setThreshold] = useState("");
  const mutation = useMutation({ mutationFn: (input: CreateAlarmRuleInput) => createAlarmRule(input), onSuccess: (result) => onCreated(result.id) });
  const selectedMetric = METRICS.find((item) => item.key === metric);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    mutation.mutate({
      name: name.trim(), integration_id: integrationId, metric, rule_type: ruleType,
      threshold: ruleType === "availability_unavailable" ? null : Number(threshold),
    });
  }

  const field = "mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100";
  return (
    <form onSubmit={submit} aria-label="Add alarm rule" className="mb-4 grid gap-3 rounded-sm border border-slate-800 bg-slate-900 p-4 md:grid-cols-2">
      <h2 className="md:col-span-2 text-base font-semibold">Add alarm rule</h2>
      {integrations.length === 0 ? <p className="text-sm text-fg-secondary md:col-span-2">Create an integration before adding an alarm rule.</p> : (
        <>
          <label className="text-xs text-slate-400">Rule name<input required maxLength={128} value={name} onChange={(event) => setName(event.target.value)} className={field} /></label>
          <label className="text-xs text-slate-400">Integration<select required value={integrationId} onChange={(event) => setIntegrationId(event.target.value)} className={field}>
            {integrations.map((integration) => <option key={integration.id} value={integration.id}>{integration.name}{integration.enabled ? "" : " (disabled)"}</option>)}
          </select></label>
          <label className="text-xs text-slate-400">Metric<select required value={metric} onChange={(event) => setMetric(event.target.value)} className={field}>
            {METRICS.filter((item) => ruleType === "availability_unavailable" ? item.key === "availability" : item.key !== "availability").map((item) => <option key={item.key} value={item.key}>{item.key} ({item.unit})</option>)}
          </select></label>
          <label className="text-xs text-slate-400">Condition<select required value={ruleType} onChange={(event) => {
            const next = event.target.value;
            setRuleType(next);
            if (next === "availability_unavailable") setMetric("availability");
            else if (metric === "availability") setMetric("power_kw");
          }} className={field}>
            <option value="threshold_high">Above threshold</option><option value="threshold_low">Below threshold</option><option value="availability_unavailable">Unavailable</option>
          </select></label>
          {ruleType !== "availability_unavailable" && <label className="text-xs text-slate-400">Threshold ({selectedMetric?.unit})<input aria-label="Threshold" type="number" step="any" required value={threshold} onChange={(event) => setThreshold(event.target.value)} className={field} /></label>}
          {mutation.isError && <p role="alert" className="text-sm text-red-400 md:col-span-2">{errorText(mutation.error)}</p>}
          <div className="flex gap-2 md:col-span-2">
            <button type="submit" disabled={mutation.isPending || !integrationId || (ruleType !== "availability_unavailable" && threshold === "")} className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">{mutation.isPending ? "Saving…" : "Save rule"}</button>
            <button type="button" onClick={onCancel} className="rounded-sm border border-slate-700 px-3 py-1.5 text-sm text-slate-300">Cancel</button>
          </div>
        </>
      )}
      {integrations.length === 0 && <button type="button" onClick={onCancel} className="w-fit rounded-sm border border-slate-700 px-3 py-1.5 text-sm text-slate-300">Close</button>}
    </form>
  );
}

function EditRuleForm({ rule, onSaved, onCancel }: { rule: AlarmRule; onSaved: () => void; onCancel: () => void }) {
  const [name, setName] = useState(rule.name);
  const [threshold, setThreshold] = useState(rule.threshold === null ? "" : String(rule.threshold));
  const [reason, setReason] = useState("");
  const mutation = useMutation({ mutationFn: (input: EditAlarmRuleInput) => editAlarmRule(rule.id, input), onSuccess: onSaved });

  function submit(event: React.FormEvent) {
    event.preventDefault();
    mutation.mutate({
      name: name.trim(),
      ...(rule.rule_type !== "availability_unavailable" && rule.unit !== null ? { threshold: Number(threshold) } : {}),
      reason: reason.trim(), expected_updated_at: rule.updated_at,
    });
  }

  return (
    <form onSubmit={submit} aria-label={`Edit ${rule.name}`} className="mt-4 space-y-3 border-t border-slate-800 pt-3">
      {!rule.enabled && <p role="status" className="text-sm text-amber-300">This rule is disabled. Editing it will not re-enable it.</p>}
      <label className="block text-xs text-slate-400">Rule name<input required maxLength={128} value={name} onChange={(event) => setName(event.target.value)} className="mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100" /></label>
      {rule.rule_type !== "availability_unavailable" && rule.unit !== null ? (
        <label className="block text-xs text-slate-400">Threshold ({rule.unit})<input aria-label="Threshold" type="number" step="any" required value={threshold} onChange={(event) => setThreshold(event.target.value)} className="mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100" /></label>
      ) : rule.unit === null && <p className="text-sm text-amber-300">Resolve the threshold unit before editing this legacy rule's threshold.</p>}
      <label className="block text-xs text-slate-400">Reason for change<textarea required maxLength={2048} value={reason} onChange={(event) => setReason(event.target.value)} className="mt-1 block w-full rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100" /></label>
      {mutation.isError && <p role="alert" className="text-sm text-red-400">{errorText(mutation.error)}</p>}
      <div className="flex gap-2"><button type="submit" disabled={mutation.isPending || !name.trim() || !reason.trim() || (rule.rule_type !== "availability_unavailable" && rule.unit !== null && threshold === "")} className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm text-white disabled:opacity-50">{mutation.isPending ? "Saving…" : "Save changes"}</button><button type="button" onClick={onCancel} className="rounded-sm border border-slate-700 px-3 py-1.5 text-sm text-slate-300">Cancel</button></div>
    </form>
  );
}

export function AlarmRulesPage() {
  const queryClient = useQueryClient();
  const canRead = useHasPermission("alarm:read");
  const canManage = useHasPermission("alarm:manage");
  const [attentionOnly, setAttentionOnly] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [action, setAction] = useState<"create" | "edit" | "resolve" | "disable" | null>(null);

  const rulesQuery = useQuery({
    queryKey: ["alarm-rules", attentionOnly],
    queryFn: () => listAlarmRules({ needsAttention: attentionOnly }),
    enabled: canRead,
  });
  const integrationsQuery = useQuery({
    queryKey: ["integrations"],
    queryFn: listIntegrations,
    enabled: canManage && action === "create",
  });

  const rules = useMemo(() => rulesQuery.data ?? [], [rulesQuery.data]);
  const selected = rules.find((rule) => rule.id === selectedId) ?? rules[0] ?? null;

  function handleSettled() {
    queryClient.invalidateQueries({ queryKey: ["alarm-rules"] });
    setAction(null);
  }

  function handleCreated(id: string) {
    setAttentionOnly(false);
    setSelectedId(id);
    queryClient.invalidateQueries({ queryKey: ["alarm-rules"] });
    setAction(null);
  }

  if (!canRead) {
    return (
      <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-center text-sm text-fg-secondary">
        You do not have permission to view alarm rules.
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        title="Alarm Rules"
        description="Create and edit alarm rules, or resolve legacy threshold units. Editing a rule affects future readings; existing alarms are preserved."
      />

      {canManage && <div className="mb-4 flex gap-2">
        <button type="button" onClick={() => setAction(action === "create" ? null : "create")} className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500">Add alarm rule</button>
      </div>}

      {canManage && action === "create" && (
        integrationsQuery.isPending ? <p role="status" className="mb-4 text-sm text-fg-secondary">Loading integrations…</p>
          : integrationsQuery.isError ? <p role="alert" className="mb-4 text-sm text-red-400">Could not load integrations.</p>
            : <CreateRuleForm integrations={integrationsQuery.data ?? []} onCreated={handleCreated} onCancel={() => setAction(null)} />
      )}

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
      </div>

      <QueryStatus query={rulesQuery} what="alarm rules" permission="alarm:read" columns={4} />

      {rulesQuery.data !== undefined && !rulesQuery.isError && rules.length === 0 && (
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-center text-sm text-fg-secondary">
          {attentionOnly ? "No alarm rules currently need attention." : "No alarm rules found."}
        </div>
      )}

      {rules.length > 0 && (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="overflow-hidden rounded-sm border border-slate-800 bg-slate-900">
            <div role="region" aria-label="Alarm rules table" tabIndex={0} className="relative overflow-x-auto">
              <table aria-label="Alarm rules" className="w-full text-left text-sm">
                <thead className="text-xs uppercase tracking-wide text-fg-secondary">
                  <tr>
                    <th scope="col" className="px-3 py-2">Rule</th>
                    <th scope="col" className="px-3 py-2">Metric</th>
                    <th scope="col" className="px-3 py-2">Status</th>
                    <th scope="col" className="px-3 py-2">Blocked since</th>
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
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          setSelectedId(rule.id);
                          setAction(null);
                        }
                      }}
                      tabIndex={0}
                      aria-current={selected?.id === rule.id ? "true" : undefined}
                      className={`cursor-pointer border-t border-slate-800 ${selected?.id === rule.id ? "bg-slate-800/70" : "hover:bg-slate-800/40"}`}
                    >
                      <td className="max-w-[260px] px-3 py-2 font-medium text-slate-200">
                        {rule.name}
                        <p className="mt-0.5 font-mono text-[11px] text-fg-secondary">{rule.id.slice(0, 8)}…</p>
                      </td>
                      <td className="px-3 py-2">
                        {rule.metric}
                        <span className="text-fg-secondary"> · {rule.rule_type}</span>
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
              <p className="text-xs font-semibold uppercase tracking-wide text-fg-secondary">Rule detail</p>
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
                  <dt className="text-fg-secondary">Metric</dt>
                  <dd>{selected.metric}</dd>
                </div>
                <div>
                  <dt className="text-fg-secondary">Stored unit</dt>
                  <dd>{selected.unit ?? "none (evaluation blocked)"}</dd>
                </div>
                <div>
                  <dt className="text-fg-secondary">Registry version</dt>
                  <dd className="font-mono text-xs">{selected.registry_version ?? "legacy (unversioned)"}</dd>
                </div>
                <div>
                  <dt className="text-fg-secondary">Integration</dt>
                  <dd className="break-all font-mono text-xs">{selected.integration_id}</dd>
                </div>
                {selected.unit_resolution_reason && (
                  <div>
                    <dt className="text-fg-secondary">Resolution note</dt>
                    <dd>{selected.unit_resolution_reason}</dd>
                  </div>
                )}
              </dl>

              {canManage && action === null && (
                <div className="mt-4 flex flex-col gap-2">
                  <button
                    type="button"
                    onClick={() => setAction("edit")}
                    className="w-full rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
                  >
                    Edit rule
                  </button>
                </div>
              )}
              {canManage && selected.needs_attention && action === null && (
                <div className="mt-2 flex flex-col gap-2">
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
                <p className="mt-4 text-sm text-fg-secondary">This rule has a resolved unit and is evaluating normally.</p>
              )}
              {canManage && action === "resolve" && (
                <ResolveForm rule={selected} onResolved={handleSettled} onCancel={() => setAction(null)} />
              )}
              {canManage && action === "disable" && (
                <DisableForm rule={selected} onDisabled={handleSettled} onCancel={() => setAction(null)} />
              )}
              {canManage && action === "edit" && (
                <EditRuleForm rule={selected} onSaved={handleSettled} onCancel={() => setAction(null)} />
              )}
            </aside>
          )}
        </div>
      )}
    </div>
  );
}
