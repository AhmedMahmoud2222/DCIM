import { FormEvent, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "@/lib/apiClient";
import { ThemeToggle } from "@/components/layout/ThemeToggle";
import { APP_TITLE } from "@/lib/appTitle";

import { useAuth } from "./useAuth";

export function LoginPage() {
  const { login, isInitialized } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // The public sign-in page names itself, and gives the title back when it goes away.
  useEffect(() => {
    document.title = `Sign in · ${APP_TITLE}`;
    return () => {
      document.title = APP_TITLE;
    };
  }, []);

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await login(email, password);
      navigate("/", { replace: true });
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : "Login failed. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="relative flex min-h-screen items-center justify-center bg-surface-page px-4">
      <form onSubmit={handleSubmit} className="w-full max-w-sm overflow-hidden rounded-lg border border-line-subtle border-t-4 border-t-brand-red bg-surface-panel p-8 shadow-sm">
        <h1 className="mb-1 text-xl font-semibold text-fg-primary">DCIM Platform</h1>
        <p className="mb-6 text-sm text-fg-secondary">Sign in to continue</p>

        <label htmlFor="login-email" className="mb-1 block text-xs font-medium text-fg-secondary">Email</label>
        <input
          id="login-email"
          name="email"
          autoComplete="username"
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mb-4 w-full rounded-sm border border-line-control bg-surface-inset px-3 py-2 text-sm text-fg-primary focus:border-link focus:outline-hidden"
        />

        <label htmlFor="login-password" className="mb-1 block text-xs font-medium text-fg-secondary">Password</label>
        <input
          id="login-password"
          name="password"
          autoComplete="current-password"
          aria-describedby={error ? "login-error" : undefined}
          aria-invalid={Boolean(error)}
          type="password"
          required
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mb-4 w-full rounded-sm border border-line-control bg-surface-inset px-3 py-2 text-sm text-fg-primary focus:border-link focus:outline-hidden"
        />

        {error && <p id="login-error" role="alert" className="mb-4 text-sm text-red-400">{error}</p>}

        <button
          type="submit"
          disabled={submitting || !isInitialized}
          className="w-full rounded-sm bg-action-primary px-3 py-2 text-sm font-medium text-action-primary-fg hover:brightness-90 disabled:opacity-50"
        >
          {submitting ? "Signing in…" : !isInitialized ? "Checking session…" : "Sign in"}
        </button>
      </form>
      <ThemeToggle className="absolute right-4 top-4" />
    </div>
  );
}
