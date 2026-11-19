import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useId, useState } from "react";
import { Link } from "react-router-dom";

import { useAuth } from "@/features/auth/useAuth";
import {
  Access,
  ALARM_SEVERITIES,
  alarmAssetTarget,
  DASHBOARD_PERMISSIONS,
  isKnownSeverity,
  SeverityFilter,
  toggleSeverity,
  unrecognisedAlarmCount,
} from "@/features/dashboard/dashboardModel";
import { DashboardSection } from "@/features/dashboard/DashboardSection";
import { AlarmWorkflowStatus, ExceptionSeverity } from "@/features/status/StatusBadges";
import { Alarm, AlarmOverview, getAlarmOverview } from "@/features/telemetry/api";

const SEVERITY_LABEL = { critical: "Critical", warning: "Warning", info: "Info" } as const;

/** Text and border tone of each severity counter; class names are written out in full for Tailwind. */
const COUNTER_TONE = {
  critical: "border-tone-danger-fg text-tone-danger-fg",
  warning: "border-tone-caution-fg text-tone-caution-fg",
  info: "border-tone-info-fg text-tone-info-fg",
} as const;

/** A severity the server sent that this build does not know: neutral, never coloured as a known level. */
function AlarmSeverityBadge({ severity }: { severity: unknown }) {
  if (isKnownSeverity(severity)) return <ExceptionSeverity severity={severity} size="sm" />;
  return (
    <span className="inline-flex items-center gap-1 whitespace-nowrap rounded-sm border border-dashed border-tone-neutral-fg bg-tone-neutral-bg px-1.5 py-0.5 text-[10px] text-tone-neutral-fg">
      <span className="sr-only">Severity: </span>Unrecognised severity
    </span>
  );
}

function presentedValue(alarm: Alarm) {
  if (alarm.presentation_unit === "%") return `${alarm.presentation_value}%`;
  return `${alarm.presentation_value}${alarm.presentation_unit ? ` ${alarm.presentation_unit}` : ""}`;
}

function AlarmRow({ alarm }: { alarm: Alarm }) {
  const { user } = useAuth();
  const asset = alarmAssetTarget(alarm, user?.permission_codes);
  return (
    <li className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1 rounded-sm bg-surface-inset px-3 py-2 text-sm">
      <div className="min-w-0 [overflow-wrap:anywhere]">
        <div className="flex flex-wrap items-center gap-1.5">
          <AlarmSeverityBadge severity={alarm.severity} />
          <AlarmWorkflowStatus status={alarm.status} size="sm" />
          <span className="font-medium text-fg-primary">{alarm.subject_key}</span>
          <span className="tabular-nums text-fg-primary">{presentedValue(alarm)}</span>
        </div>
        <p className="mt-1 text-xs text-fg-secondary">
          Opened {new Date(alarm.opened_at).toLocaleString()} ·{" "}
          {alarm.acknowledged_at ? `Acknowledged ${new Date(alarm.acknowledged_at).toLocaleString()}` : "Not acknowledged"}
        </p>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
        <Link className="text-link underline underline-offset-2" to="/events" aria-label={`Open ${alarm.subject_key} in Events`}>
          Events
        </Link>
        {asset?.kind === "link" && (
          <Link className="text-link underline underline-offset-2" to={asset.to} aria-label={`Open ${asset.label.toLowerCase()} for ${alarm.subject_key}`}>
            {asset.label}
          </Link>
        )}
        {asset?.kind === "text" && <span className="font-mono text-fg-secondary">{asset.label}</span>}
      </div>
    </li>
  );
}

interface ControlsProps {
  data: AlarmOverview;
  severity: SeverityFilter;
  includeAcknowledged: boolean;
  updating: boolean;
  onSeverity: (value: SeverityFilter) => void;
  onIncludeAcknowledged: (value: boolean) => void;
}

function AlarmControls({ data, severity, includeAcknowledged, updating, onSeverity, onIncludeAcknowledged }: ControlsProps) {
  const groupName = useId();
  const switchId = useId();
  const unrecognised = unrecognisedAlarmCount(data.counts);
  return (
    <div className="mb-3 space-y-3">
      <div className="grid grid-cols-3 gap-2" role="group" aria-label="Alarm counts by severity">
        {ALARM_SEVERITIES.map((level) => (
          <button
            key={level}
            type="button"
            aria-pressed={severity === level}
            onClick={() => onSeverity(toggleSeverity(severity, level))}
            className={`min-h-11 rounded-sm border bg-surface-page px-2 py-1.5 text-left hover:bg-surface-hover ${
              severity === level ? `border-2 ${COUNTER_TONE[level]}` : "border-line-subtle text-fg-secondary"
            }`}
          >
            <span className="block text-xs font-medium">{SEVERITY_LABEL[level]}</span>
            <span className="block text-xl font-semibold tabular-nums text-fg-primary">{data.counts[level]}</span>
          </button>
        ))}
      </div>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 text-sm">
        <fieldset className="flex flex-wrap items-center gap-1">
          <legend className="sr-only">Severity shown in the list</legend>
          {(["all", ...ALARM_SEVERITIES] as SeverityFilter[]).map((value) => (
            <label
              key={value}
              className={`inline-flex min-h-8 cursor-pointer items-center rounded-sm border px-2.5 py-1 text-xs has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-focus ${
                severity === value ? "border-action-primary bg-action-primary text-action-primary-fg" : "border-line-control text-fg-secondary hover:bg-surface-hover"
              }`}
            >
              <input
                type="radio"
                name={groupName}
                value={value}
                checked={severity === value}
                onChange={() => onSeverity(value)}
                className="sr-only"
              />
              {value === "all" ? "All" : SEVERITY_LABEL[value]}
            </label>
          ))}
        </fieldset>
        <label htmlFor={switchId} className="inline-flex min-h-11 cursor-pointer items-center gap-2 text-xs text-fg-primary">
          <input
            id={switchId}
            type="checkbox"
            role="switch"
            checked={includeAcknowledged}
            aria-checked={includeAcknowledged}
            onChange={(event) => onIncludeAcknowledged(event.target.checked)}
            className="h-5 w-5 accent-action-primary"
          />
          Include acknowledged
        </label>
      </div>
      <p className="text-xs text-fg-secondary" data-testid="alarms-scope">
        <span className="font-semibold tabular-nums text-fg-primary">{data.counts.total}</span>{" "}
        {includeAcknowledged ? "active or acknowledged" : "active"} in total
        {unrecognised > 0 ? `, including ${unrecognised} with an unrecognised severity` : ""}.
        {updating ? <span role="status"> Updating…</span> : null}
      </p>
    </div>
  );
}

export function AlarmsPanel({ access, className = "" }: { access: Access; className?: string }) {
  const [severity, setSeverity] = useState<SeverityFilter>("all");
  const [includeAcknowledged, setIncludeAcknowledged] = useState(false);
  const query = useQuery({
    queryKey: ["alarms", "overview", { severity, includeAcknowledged }],
    queryFn: async () => {
      const data = await getAlarmOverview({ severity: severity === "all" ? null : severity, includeAcknowledged });
      // A response without counts or items is a failure, never a zero.
      if (!data || !Array.isArray(data.items) || typeof data.counts !== "object" || data.counts === null) {
        throw new Error("The alarm overview response was not in the expected shape.");
      }
      return data;
    },
    enabled: access === "granted",
    placeholderData: keepPreviousData,
  });
  const updating = query.isPlaceholderData && query.isFetching;
  const scopeText = includeAcknowledged ? "active or acknowledged" : "active";
  const filterText = severity === "all" ? "" : `${SEVERITY_LABEL[severity]} `;

  return (
    <DashboardSection
      id="alarms"
      title="Active alarms"
      what="active alarms"
      permission={DASHBOARD_PERMISSIONS.alarms}
      access={access}
      query={query}
      className={className}
      isEmpty={(data) => data.items.length === 0}
      emptyText={`No ${filterText}${scopeText} alarms.`}
      controls={(data) => (
        <AlarmControls
          data={data}
          severity={severity}
          includeAcknowledged={includeAcknowledged}
          updating={updating}
          onSeverity={setSeverity}
          onIncludeAcknowledged={setIncludeAcknowledged}
        />
      )}
      footer={
        <p className="mt-3 text-xs text-fg-secondary">
          <Link className="text-link underline underline-offset-2" to="/events">
            Open Events →
          </Link>
        </p>
      }
    >
      {(data) => (
        <>
          <ul className="space-y-2" aria-label={`Latest ${filterText}${scopeText} alarms`}>
            {data.items.map((alarm) => (
              <AlarmRow key={alarm.id} alarm={alarm} />
            ))}
          </ul>
          {(data.truncated || data.matching_total > data.items.length) && (
            <p className="mt-2 text-xs text-fg-secondary" data-testid="alarms-truncation">
              Showing latest {data.items.length} of {data.matching_total}.
            </p>
          )}
        </>
      )}
    </DashboardSection>
  );
}
