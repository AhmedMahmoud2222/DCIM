import { ReactNode, useEffect, useRef } from "react";

const FOCUSABLE_SELECTOR = 'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** A minimal, dependency-free modal dialog: focus moves in on mount (to
 * `data-dialog-initial-focus` when present, else the first focusable element),
 * Tab/Shift+Tab wrap within the dialog, Escape closes it, and focus returns to
 * whatever triggered it on unmount. `busy` suppresses Escape and backdrop-click close
 * while a mutation is in flight, so a pending disconnect/create can't be dismissed out
 * from under itself. There is no dialog primitive elsewhere in this codebase to reuse —
 * this stays intentionally small rather than pulling in a UI library for one modal. */
export function Dialog({
  titleId,
  descriptionId,
  title,
  description,
  onClose,
  busy = false,
  children,
}: {
  titleId: string;
  descriptionId?: string;
  title: ReactNode;
  description?: ReactNode;
  onClose: () => void;
  busy?: boolean;
  children: ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const previouslyFocused = useRef<HTMLElement | null>(null);

  useEffect(() => {
    previouslyFocused.current = document.activeElement as HTMLElement | null;
    const container = containerRef.current;
    const initial = container?.querySelector<HTMLElement>("[data-dialog-initial-focus]") ?? container?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR) ?? container;
    initial?.focus();
    return () => {
      previouslyFocused.current?.focus?.();
    };
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        if (busy) return;
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const container = containerRef.current;
      if (!container) return;
      const focusable = Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR));
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [busy, onClose]);

  return (
    <div
      className="fixed inset-0 z-50 grid place-items-center bg-slate-950/75 p-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        ref={containerRef}
        className="surface w-full max-w-md p-5 shadow-2xl outline-none"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
      >
        <h2 id={titleId} className="text-lg font-semibold">
          {title}
        </h2>
        {description && (
          <p id={descriptionId} className="mt-2 text-sm text-slate-400">
            {description}
          </p>
        )}
        {children}
      </section>
    </div>
  );
}
