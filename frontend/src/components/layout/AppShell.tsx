import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "@/features/auth/useAuth";

const NAV_GROUPS = [
  { label: "Operate", items: [{ to: "/", label: "Dashboard", mark: "◈" }, { to: "/events", label: "Events", mark: "!" }, { to: "/power", label: "Power", mark: "↯" }] },
  { label: "Infrastructure", items: [{ to: "/infrastructure", label: "Sites & rooms", mark: "⌂" }, { to: "/racks", label: "Racks", mark: "▥" }, { to: "/equipment", label: "Equipment", mark: "▣" }, { to: "/floor-plans", label: "Floor plans", mark: "⌘" }, { to: "/managed-assets", label: "Asset inventory", mark: "◇" }] },
  { label: "Connectivity", items: [{ to: "/collectors", label: "Collectors", mark: "◌" }, { to: "/integrations", label: "Integrations", mark: "⇄" }, { to: "/discovery", label: "Discovery", mark: "◎" }] },
];

function breadcrumb(pathname: string) {
  if (pathname === "/") return "Operations overview";
  if (pathname.startsWith("/events")) return "Operations / Events";
  if (pathname.startsWith("/sites/")) return "Infrastructure / Site";
  if (pathname.startsWith("/floor-plans/room/")) return "Infrastructure / Room / Floor plan";
  if (pathname.startsWith("/racks/")) return "Infrastructure / Rack";
  if (pathname.startsWith("/equipment/")) return "Infrastructure / Equipment";
  const part = pathname.split("/")[1] ?? "";
  return part.replace(/-/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function AppShell() {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();

  async function handleLogout() {
    await logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="min-h-screen bg-[#080d17] text-slate-100 lg:flex">
      <aside className="border-b border-slate-800 bg-[#0b1220] px-2 py-3 lg:fixed lg:inset-y-0 lg:w-60 lg:border-b-0 lg:border-r">
        <div className="mb-4 flex items-center gap-2 px-2">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-indigo-500 text-sm font-bold text-white shadow-lg shadow-indigo-500/20">D</span>
          <div><p className="text-sm font-semibold tracking-wide text-slate-100">DCIM</p><p className="text-[11px] text-slate-500">Operations workspace</p></div>
        </div>
        <nav className="flex gap-1 overflow-x-auto lg:block lg:space-y-3">
          {NAV_GROUPS.map((group) => (
            <div key={group.label} className="shrink-0">
              <p className="mb-1 px-2 text-[10px] font-semibold uppercase tracking-[0.1em] text-slate-500 lg:mt-0">{group.label}</p>
              <div className="flex gap-1 lg:block lg:space-y-1">
                {group.items.map((item) => <NavLink key={item.to} to={item.to} end={item.to === "/"} className={({ isActive }) => `flex items-center gap-2 rounded-lg px-2.5 py-2 text-sm transition ${isActive ? "bg-indigo-500/15 text-indigo-200 ring-1 ring-inset ring-indigo-400/20" : "text-slate-400 hover:bg-slate-800/70 hover:text-slate-100"}`}><span className="text-sm leading-none text-slate-500">{item.mark}</span>{item.label}</NavLink>)}
              </div>
            </div>
          ))}
        </nav>
      </aside>
      <div className="min-h-screen flex-1 lg:ml-60">
        <header className="sticky top-0 z-20 flex min-h-10 items-center justify-between border-b border-slate-800 bg-[#080d17]/90 px-4 backdrop-blur lg:px-6">
          <p className="text-xs text-slate-500"><span className="hidden sm:inline">DCIM / </span>{breadcrumb(location.pathname)}</p>
          <div className="flex items-center gap-3"><span className="hidden text-xs text-slate-500 sm:inline">{user?.email}</span>
          <button onClick={handleLogout} className="rounded-md px-2 py-1 text-xs text-slate-400 transition hover:bg-slate-800 hover:text-slate-100">
            Sign out
          </button></div>
        </header>
        <main className="flex-1 px-3 py-4 sm:px-4 lg:px-6 lg:py-4">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
