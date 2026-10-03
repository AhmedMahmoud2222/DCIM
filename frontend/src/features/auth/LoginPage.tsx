import { FormEvent, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError } from "@/lib/apiClient";

import { useAuth } from "./useAuth";

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

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
    <div className="flex min-h-screen items-center justify-center bg-slate-950">
      <form onSubmit={handleSubmit} className="w-full max-w-sm rounded-lg border border-slate-800 bg-slate-900 p-8">
        <h1 className="mb-1 text-xl font-semibold text-slate-100">DCIM Platform</h1>
        <p className="mb-6 text-sm text-slate-400">Sign in to continue</p>

        <label htmlFor="login-email" className="mb-1 block text-xs font-medium text-slate-400">Email</label>
        <input
          id="login-email"
          name="email"
          autoComplete="username"
          type="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mb-4 w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
        />

        <label htmlFor="login-password" className="mb-1 block text-xs font-medium text-slate-400">Password</label>
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
          className="mb-4 w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-2 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
        />

        {error && <p id="login-error" role="alert" className="mb-4 text-sm text-red-400">{error}</p>}

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-sm bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
