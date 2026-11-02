import { ReactNode } from "react";

import { formatAge, FreshnessResult, UNKNOWN_REASON_TEXT } from "@/features/status/freshness";
import {
  alarmWorkflowModel,
  connectivityModel,
  exceptionSeverityModel,
  freshnessModel,
  lifecycleModel,
  Tone,
} from "@/features/status/statusModels";

/** Five independent status dimensions. Hue carries urgency, the icon shape carries the dimension
 * (square: lifecycle, diamond: alarm workflow, octagon/triangle/circle: severity, signal bars:
 * collector connectivity, clock: measurement freshness) and the text always states the value, so
 * no chip relies on color alone. Class names are written out in full so Tailwind can see them. */
const TONE_CLASS: Record<Tone, string> = {
  neutral: "border-tone-neutral-fg bg-tone-neutral-bg text-tone-neutral-fg",
  info: "border-tone-info-fg bg-tone-info-bg text-tone-info-fg",
  ok: "border-tone-ok-fg bg-tone-ok-bg text-tone-ok-fg",
  caution: "border-tone-caution-fg bg-tone-caution-bg text-tone-caution-fg",
  serious: "border-tone-serious-fg bg-tone-serious-bg text-tone-serious-fg",
  danger: "border-tone-danger-fg bg-tone-danger-bg text-tone-danger-fg",
  attention: "border-tone-attention-fg bg-tone-attention-bg text-tone-attention-fg",
  unknown: "border-dashed border-tone-unknown-fg bg-tone-unknown-bg text-tone-unknown-fg",
};

export type ChipSize = "sm" | "md";
const SIZE_CLASS: Record<ChipSize, string> = {
  sm: "px-1.5 py-0.5 text-[10px]",
  md: "px-2 py-0.5 text-xs",
};

function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      viewBox="0 0 16 16"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      className="shrink-0"
    >
      {children}
    </svg>
  );
}

interface ChipProps {
  tone: Tone;
  size: ChipSize;
  icon: ReactNode;
  label: string;
  /** Spoken before the label so a screen reader hears which dimension this is. */
  dimension: string;
  title?: string;
  testId?: string;
  children?: ReactNode;
}

function Chip({ tone, size, icon, label, dimension, title, testId, children }: ChipProps) {
  return (
    <span
      title={title}
      data-testid={testId}
      className={`inline-flex items-center gap-1 whitespace-nowrap rounded-sm border ${SIZE_CLASS[size]} ${TONE_CLASS[tone]}`}
    >
      {icon}
      <span className="sr-only">{dimension}: </span>
      <span>{label}</span>
      {children}
    </span>
  );
}

// --- Asset lifecycle: square -------------------------------------------------------------------
export function LifecycleStatus({ value, size = "md" }: { value: string | null | undefined; size?: ChipSize }) {
  const model = lifecycleModel(value);
  const icon = (
    <Icon>
      <rect x="3" y="3" width="10" height="10" rx="1.5" strokeDasharray={model.known ? undefined : "2 2"} />
      {model.variant === "active" && <rect x="6" y="6" width="4" height="4" fill="currentColor" />}
      {model.variant === "maintenance" && <path d="M5.5 8h5" />}
      {model.variant === "removed" && <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" />}
    </Icon>
  );
  return (
    <Chip
      tone={model.tone}
      size={size}
      icon={icon}
      label={model.label}
      dimension="Lifecycle"
      title={model.known ? undefined : "Lifecycle value not recognised by this version of the application"}
    />
  );
}

// --- Alarm workflow: diamond -------------------------------------------------------------------
export function AlarmWorkflowStatus({ status, size = "md" }: { status: string | null | undefined; size?: ChipSize }) {
  const model = alarmWorkflowModel(status);
  const icon = (
    <Icon>
      <path
        d="M8 2l6 6-6 6-6-6z"
        fill={model.variant === "ACTIVE" ? "currentColor" : "none"}
        strokeDasharray={model.known ? undefined : "2 2"}
      />
      {model.variant === "ACKNOWLEDGED" && <path d="M5.5 8l1.8 1.8 3.2-3.4" />}
      {model.variant === "CLEARED" && <path d="M5.5 8h5" />}
    </Icon>
  );
  return (
    <Chip
      tone={model.tone}
      size={size}
      icon={icon}
      label={model.label}
      dimension="Alarm status"
      title="Alarm workflow state. It does not indicate severity."
    />
  );
}

// --- Exception severity: octagon / triangle / circle --------------------------------------------
export function ExceptionSeverity({ severity, size = "md" }: { severity: string | null | undefined; size?: ChipSize }) {
  const model = exceptionSeverityModel(severity);
  let shape: ReactNode;
  if (model.variant === "critical") {
    shape = (
      <>
        <path d="M5.2 1.8h5.6l3.4 3.4v5.6l-3.4 3.4H5.2L1.8 10.8V5.2z" />
        <path d="M8 5v3.4M8 10.6v.1" />
      </>
    );
  } else if (model.variant === "warning") {
    shape = (
      <>
        <path d="M8 2l6.5 11.5h-13z" />
        <path d="M8 6.5v3M8 11.4v.1" />
      </>
    );
  } else if (model.variant === "info") {
    shape = (
      <>
        <circle cx="8" cy="8" r="6" />
        <path d="M8 7.4v3.4M8 5.3v.1" />
      </>
    );
  } else {
    shape = (
      <>
        <circle cx="8" cy="8" r="6" strokeDasharray="2 2" />
        <path d="M6.4 6.4a1.7 1.7 0 113 1c-.7.5-1.4.8-1.4 1.8M8 11.6v.1" />
      </>
    );
  }
  return <Chip tone={model.tone} size={size} icon={<Icon>{shape}</Icon>} label={model.label} dimension="Severity" />;
}

// --- Collector connectivity: signal bars --------------------------------------------------------
export function CollectorConnectivity({ health, size = "sm" }: { health: string | null | undefined; size?: ChipSize }) {
  const model = connectivityModel(health);
  const lit = model.variant === "healthy" ? 3 : model.variant === "stale" ? 2 : 0;
  const bar = (x: number, top: number, index: number) => (
    <path key={x} d={`M${x} 13V${top}`} strokeWidth="2.4" opacity={index < lit ? 1 : 0.3} />
  );
  const icon = (
    <Icon>
      {[bar(3, 9, 0), bar(8, 6, 1), bar(13, 3, 2)]}
      {model.variant === "offline" && <path d="M2 2l12 12" strokeWidth="1.8" />}
      {!model.known && <path d="M2 14h12" strokeDasharray="2 2" />}
    </Icon>
  );
  return (
    <Chip
      tone={model.tone}
      size={size}
      icon={icon}
      label={model.label}
      dimension="Collector connectivity"
      title="Reported by the platform from the collector's last heartbeat. It is not measurement freshness."
    />
  );
}

// --- Measurement freshness (derived): clock -----------------------------------------------------
export function MeasurementFreshness({
  result,
  size = "sm",
  showAge = true,
}: {
  result: FreshnessResult;
  size?: ChipSize;
  showAge?: boolean;
}) {
  const model = freshnessModel(result);
  const icon = (
    <Icon>
      <circle cx="8" cy="8" r="6" strokeDasharray={model.variant === "stale" || model.variant === "unknown" ? "2.2 1.6" : undefined} />
      {model.variant === "unknown" ? (
        <path d="M6.5 6.5a1.6 1.6 0 113 .9c-.6.4-1.3.7-1.3 1.6M8 11.3v.1" />
      ) : (
        <path d={model.variant === "fresh" ? "M8 4.5V8" : model.variant === "late" ? "M8 4.5V8h2.6" : "M8 4.5V8l2.4 2.4"} />
      )}
    </Icon>
  );
  const detail =
    result.state === "unknown"
      ? result.reason
        ? UNKNOWN_REASON_TEXT[result.reason]
        : null
      : result.ageSeconds != null && showAge
        ? `measured ${formatAge(result.ageSeconds)} ago`
        : null;
  return (
    <span className="inline-flex flex-wrap items-center gap-x-1.5 gap-y-0.5" data-testid="measurement-freshness">
      <Chip
        tone={model.tone}
        size={size}
        icon={icon}
        label={model.label}
        dimension="Measurement freshness (derived)"
        title="Derived in the browser from the measurement time and the expected poll interval. Not a collector or backend status."
      />
      {detail && <span className="text-[11px] text-fg-secondary">{detail}</span>}
    </span>
  );
}
