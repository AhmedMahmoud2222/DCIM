import { Navigate, Outlet } from "react-router-dom";

import { useAuth } from "@/features/auth/useAuth";

/** Route-level gating is a UX convenience only — §27 of the Phase 1 prompt: "frontend
 * authorization is never security enforcement." Every actual permission check happens
 * server-side (see app/application/rbac.py); this component only avoids flashing
 * protected UI at an unauthenticated visitor before the API would reject them anyway. */
export function ProtectedRoute() {
  const { isAuthenticated, isInitialized } = useAuth();
  if (!isInitialized) {
    return <div role="status" aria-live="polite" className="p-6 text-sm text-fg-secondary">Restoring your session…</div>;
  }
  if (!isAuthenticated) return <Navigate to="/login" replace />;
  return <Outlet />;
}
