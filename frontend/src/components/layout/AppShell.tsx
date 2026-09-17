import { NavLink, Outlet, useNavigate } from "react-router-dom";

import { useAuth } from "@/features/auth/useAuth";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard" },
  { to: "/locations", label: "Locations" },
  { to: "/managed-assets", label: "Managed Assets" },
  { to: "/racks", label: "Racks" },
  { to: "/equipment", label: "Equipment" },
  { to: "/floor-plans", label: "Floor Plans" },
];

export function AppShell() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();

  async function handleLogout() {
    await logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="flex min-h-screen bg-slate-950 text-slate-100">
      <aside className="w-56 shrink-0 border-r border-slate-800 bg-slate-900 p-4">
        <div className="mb-6 text-sm font-semibold tracking-wide text-slate-300">DCIM PLATFORM</div>
        <nav className="space-y-1">
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
        </nav>
      </aside>
      <div className="flex flex-1 flex-col">
        <header className="flex items-center justify-between border-b border-slate-800 bg-slate-900 px-6 py-3">
          <span className="text-sm text-slate-400">{user?.email}</span>
          <button onClick={handleLogout} className="text-sm text-slate-400 hover:text-slate-100">
            Sign out
          </button>
        </header>
        <main className="flex-1 p-6">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
