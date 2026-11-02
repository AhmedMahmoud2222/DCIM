import { ReactNode } from "react";

/** The title block every operational page starts with: one h1, an optional plain-language description,
 * and the page's actions on the right (they wrap under the title on a narrow screen). Actions are
 * buttons and links; status and metadata do not belong here. */
export function PageHeader({ title, description, actions }: { title: string; description?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-4 flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold text-fg-primary">{title}</h1>
        {description ? <p className="mt-1 max-w-3xl text-sm text-fg-secondary">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </header>
  );
}
