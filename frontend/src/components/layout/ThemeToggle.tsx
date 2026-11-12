import { KeyboardEvent } from "react";

import { Theme, THEME_LABELS, useTheme } from "@/lib/theme";

const THEMES: readonly Theme[] = ["light", "dark"];

/** Two-option switch between Normal and Dark mode. A radio group: arrow keys move and select, Tab
 * leaves the group, and the selected option is announced by its checked state, not by color alone. */
export function ThemeToggle({ className = "" }: { className?: string }) {
  const [theme, setTheme] = useTheme();

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    const keys: Record<string, number> = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };
    const step = keys[event.key];
    if (!step) return;
    event.preventDefault();
    const next = THEMES[(THEMES.indexOf(theme) + step + THEMES.length) % THEMES.length];
    setTheme(next);
    const group = event.currentTarget.parentElement;
    window.requestAnimationFrame(() => group?.querySelector<HTMLElement>(`[data-theme-option="${next}"]`)?.focus());
  }

  return (
    <div role="radiogroup" aria-label="Color mode" className={`inline-flex rounded-sm border border-line-control p-0.5 ${className}`}>
      {THEMES.map((option) => {
        const checked = option === theme;
        return (
          <button
            key={option}
            type="button"
            role="radio"
            aria-checked={checked}
            tabIndex={checked ? 0 : -1}
            data-theme-option={option}
            onClick={() => setTheme(option)}
            onKeyDown={handleKeyDown}
            className={`flex items-center gap-1.5 rounded-xs px-2.5 py-1 text-xs font-medium min-h-9 sm:pointer-fine:min-h-0 ${
              checked ? "bg-action-primary text-action-primary-fg" : "text-fg-secondary hover:bg-surface-hover hover:text-fg-primary"
            }`}
          >
            <svg aria-hidden="true" viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
              {option === "light" ? (
                <>
                  <circle cx="12" cy="12" r="4" />
                  <path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4" />
                </>
              ) : (
                <path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z" />
              )}
            </svg>
            {THEME_LABELS[option]}
          </button>
        );
      })}
    </div>
  );
}
