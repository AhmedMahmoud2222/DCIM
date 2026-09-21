import { ReactNode } from "react";

type Tone = "neutral" | "healthy" | "warning" | "critical" | "stale" | "info";

const toneClasses: Record<Tone, string> = {
  neutral: "border-slate-700 bg-slate-800 text-slate-300",
  healthy: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
  warning: "border-amber-400/30 bg-amber-400/10 text-amber-200",
  critical: "border-rose-400/30 bg-rose-500/10 text-rose-200",
  stale: "border-orange-400/30 bg-orange-400/10 text-orange-200",
  info: "border-sky-400/30 bg-sky-400/10 text-sky-200",
};

export function StatusBadge({ label, tone = "neutral" }: { label: string; tone?: Tone }) {
  return <span className={`status-badge ${toneClasses[tone]}`}>{label}</span>;
}

export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
  eyebrow?: string;
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <p className="page-eyebrow">{eyebrow}</p>}
        <h1>{title}</h1>
        {description && <p className="page-description">{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}

export function MetricCard({ label, value, detail, tone = "neutral" }: { label: string; value: string | number; detail?: string; tone?: Tone }) {
  const accent: Record<Tone, string> = {
    neutral: "bg-slate-500", healthy: "bg-emerald-400", warning: "bg-amber-400", critical: "bg-rose-400", stale: "bg-orange-400", info: "bg-sky-400",
  };
  return (
    <div className="metric-card">
      <span className={`metric-card-accent ${accent[tone]}`} />
      <p className="metric-card-label">{label}</p>
      <p className="metric-card-value">{value}</p>
      {detail && <p className="metric-card-detail">{detail}</p>}
    </div>
  );
}

export function EmptyState({ title, detail, action }: { title: string; detail: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="empty-state-mark" aria-hidden="true">—</div>
      <div><p className="font-medium text-slate-200">{title}</p><p className="mt-1 text-sm text-slate-500">{detail}</p>{action && <div className="mt-3">{action}</div>}</div>
    </div>
  );
}

export function SectionTitle({ title, detail, action }: { title: string; detail?: string; action?: ReactNode }) {
  return <div className="section-title"><div><h2>{title}</h2>{detail && <p>{detail}</p>}</div>{action}</div>;
}
