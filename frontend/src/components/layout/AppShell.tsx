import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { useAuth } from "@/features/auth/useAuth";
import { useHasPermission, useIsCatalogAdministrator } from "@/features/auth/useAuthorization";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard" },
  { to: "/infrastructure", label: "Infrastructure" },
  { to: "/managed-assets", label: "Managed Assets" },
  { to: "/racks", label: "Racks" },
  { to: "/equipment", label: "Equipment" },
  { to: "/floor-plans", label: "Floor Plans" },
  { to: "/floor-plans/3d-layout", label: "3D Layout" },
  { to: "/power", label: "Power Topology" },
  { to: "/events", label: "Events" },
  { to: "/collectors", label: "Collectors" },
  { to: "/integrations", label: "Integrations" },
  { to: "/discovery", label: "Discovery" },
];

const ADMIN_NAV_ITEMS = [{ to: "/admin/catalog", label: "Asset Catalog" }];
const ACCESS_NAV_ITEMS = [
  { to: "/admin/users", label: "Users", permission: "user:read" },
  { to: "/admin/groups", label: "Groups", permission: "group:read" },
];

export function AppShell() {
  const { user, logout } = useAuth();
  const isCatalogAdministrator = useIsCatalogAdministrator();
  const canReadUsers = useHasPermission("user:read");
  const canReadGroups = useHasPermission("group:read");
  const navigate = useNavigate();
  const accessItems = ACCESS_NAV_ITEMS.filter((i) => (i.permission === "user:read" ? canReadUsers : canReadGroups));
  const showAdminHeading = isCatalogAdministrator || accessItems.length > 0;

  async function handleLogout() {
    await logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="flex min-h-screen flex-col bg-slate-950 text-slate-100 sm:flex-row">
      <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:rounded focus:bg-blue-600 focus:px-3 focus:py-2 focus:text-white">
        Skip to main content
      </a>
      <aside className="w-full border-b border-slate-800 bg-slate-900 p-4 sm:w-56 sm:shrink-0 sm:border-b-0 sm:border-r">
        <div className="mb-6 text-sm font-semibold tracking-wide text-slate-300">DCIM PLATFORM</div>
        <nav aria-label="Primary" className="space-y-1">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `block rounded px-3 py-2 text-sm ${isActive ? "bg-blue-600 text-white" : "text-slate-400 hover:bg-slate-800 hover:text-slate-100"}`
              }
            >
              {item.label}
            </NavLink>
          ))}
          {showAdminHeading && (
            <div className="mb-1 mt-4 px-3 text-xs font-semibold uppercase tracking-wide text-slate-300">Admin</div>
          )}
          {[...(isCatalogAdministrator ? ADMIN_NAV_ITEMS : []), ...accessItems].map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                `block rounded px-3 py-2 text-sm ${isActive ? "bg-blue-600 text-white" : "text-slate-400 hover:bg-slate-800 hover:text-slate-100"}`
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-3 border-b border-slate-800 bg-slate-900 px-4 py-3 sm:px-6">
          <span className="min-w-0 break-all text-sm text-slate-400">{user?.email}</span>
          <button onClick={handleLogout} className="text-sm text-slate-400 hover:text-slate-100">
            Sign out
          </button>
        </header>
        <main id="main-content" tabIndex={-1} className="min-w-0 flex-1 p-4 sm:p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
