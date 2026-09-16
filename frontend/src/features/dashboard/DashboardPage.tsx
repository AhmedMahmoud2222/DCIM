import { useAuth } from "@/features/auth/useAuth";

export function DashboardPage() {
  const { user } = useAuth();
  return (
    <div>
      <h1 className="mb-2 text-lg font-semibold">Welcome, {user?.full_name}</h1>
      <p className="max-w-2xl text-sm text-slate-400">
        This is the Phase 1 foundation shell: authentication, RBAC-aware navigation, and the location/managed-asset
        read paths are wired to the real API. Rack elevation, floor plans, the 3D twin, power/network topology, and
        telemetry dashboards are later-phase work and are intentionally not present here.
      </p>
    </div>
  );
}
