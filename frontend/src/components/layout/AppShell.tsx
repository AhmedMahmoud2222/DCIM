import { KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "@/features/auth/useAuth";
import { useHasPermission, useIsCatalogAdministrator } from "@/features/auth/useAuthorization";
import { useAuthStore } from "@/lib/authStore";

const NAV_ITEMS = [
  { to: "/", label: "Dashboard" },
  { to: "/infrastructure", label: "Infrastructure" },
  { to: "/locations", label: "Locations", permission: "location:read" },
  { to: "/managed-assets", label: "Managed Assets" },
  { to: "/racks", label: "Racks" },
  { to: "/equipment", label: "Equipment" },
  { to: "/floor-plans", label: "Floor Plans" },
  { to: "/floor-plans/3d-layout", label: "3D Layout" },
  { to: "/power", label: "Power Topology" },
  { to: "/events", label: "Events" },
  { to: "/alarm-rules", label: "Alarm Rules", permission: "alarm:read" },
  { to: "/collectors", label: "Collectors" },
  { to: "/integrations", label: "Integrations" },
  { to: "/discovery", label: "Discovery" },
  { to: "/discovery/neighbors", label: "Neighbors", permission: "discovery:read" },
  { to: "/network/profiles", label: "Device Profiles", permission: "network_profile:read" },
  { to: "/cables", label: "Cables", permission: "cable:read" },
  { to: "/topology/pass-throughs", label: "Pass-throughs", permission: "cable:read" },
  { to: "/topology/trace", label: "Trace", permission: "cable:read" },
] as ReadonlyArray<{ to: string; label: string; permission?: string }>;

const ADMIN_NAV_ITEMS = [{ to: "/admin/catalog", label: "Asset Catalog" }];
const ACCESS_NAV_ITEMS = [
  { to: "/admin/users", label: "Users", permission: "user:read" },
  { to: "/admin/groups", label: "Groups", permission: "group:read" },
];

/** Tailwind's `sm` breakpoint (40rem). Below it the sidebar becomes a modal drawer. The same
 * value drives the `sm:` classes below, so layout and behaviour switch at the same width. */
const DESKTOP_QUERY = "(min-width: 40rem)";

function readIsDesktop(): boolean {
  // Environments without matchMedia (such as jsdom) are treated as desktop, which keeps the
  // always-visible sidebar that existing tests expect.
  return typeof window.matchMedia !== "function" ? true : window.matchMedia(DESKTOP_QUERY).matches;
}

function useIsDesktop(): boolean {
  const [isDesktop, setIsDesktop] = useState(readIsDesktop);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const query = window.matchMedia(DESKTOP_QUERY);
    const update = () => setIsDesktop(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  return isDesktop;
}

// 44px minimum target by default (small screens and touch); relaxed only on a wide screen with
// a precise pointer, where the compact sidebar is the intended look.
const TOUCH_TARGET = "min-h-11 sm:pointer-fine:min-h-0";

function navLinkClass({ isActive }: { isActive: boolean }): string {
  return `flex items-center rounded-r-sm border-l-[3px] px-3 py-2 text-sm ${TOUCH_TARGET} ${
    isActive
      ? "border-link bg-side-selected font-semibold text-white"
      : "border-transparent text-side-fg hover:bg-white/5 hover:text-white"
  }`;
}

export function AppShell() {
  const { user, logout } = useAuth();
  const isCatalogAdministrator = useIsCatalogAdministrator();
  const canReadUsers = useHasPermission("user:read");
  const canReadGroups = useHasPermission("group:read");
  const permissions = useAuthStore((s) => s.user?.permission_codes);
  const navItems = NAV_ITEMS.filter((item) => !item.permission || permissions?.includes(item.permission));
  const navigate = useNavigate();
  const location = useLocation();
  const accessItems = ACCESS_NAV_ITEMS.filter((i) => (i.permission === "user:read" ? canReadUsers : canReadGroups));
  const showAdminHeading = isCatalogAdministrator || accessItems.length > 0;

  const isDesktop = useIsDesktop();
  const [open, setOpen] = useState(false);
  const drawerOpen = open && !isDesktop;
  const sidebarRef = useRef<HTMLElement | null>(null);
  const menuButtonRef = useRef<HTMLButtonElement | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);

  const closeMenu = useCallback((returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) menuButtonRef.current?.focus();
  }, []);

  // Leaving the small-screen layout, or navigating, closes the drawer.
  useEffect(() => {
    if (isDesktop) setOpen(false);
  }, [isDesktop]);
  useEffect(() => {
    setOpen(false);
  }, [location.pathname]);

  // Modal behaviour while open: focus moves into the drawer, and the page behind does not scroll.
  useEffect(() => {
    if (!drawerOpen) return;
    closeButtonRef.current?.focus();
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [drawerOpen]);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (!drawerOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeMenu(true);
      return;
    }
    if (event.key !== "Tab") return;
    const sidebar = sidebarRef.current;
    const focusable = sidebar?.querySelectorAll<HTMLElement>("a[href], button:not([disabled])");
    if (!sidebar || !focusable || focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    const outside = !sidebar.contains(active);
    if (event.shiftKey && (active === first || outside)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || outside)) {
      event.preventDefault();
      first.focus();
    }
  }

  async function handleLogout() {
    await logout();
    navigate("/login", { replace: true });
  }

  // Desktop: a landmark sidebar in the page flow, as before. Small screens: an off-canvas
  // drawer that is a dialog while open and inert (unfocusable, hidden from assistive
  // technology) while closed.
  const setSidebarNode = (node: HTMLElement | null) => {
    sidebarRef.current = node;
  };
  const sidebarClass = [
    "z-40 w-72 max-w-[86%] overflow-y-auto border-r border-line-subtle bg-surface-sidebar p-4",
    "fixed inset-y-0 left-0 transition-transform duration-150 ease-out",
    drawerOpen ? "translate-x-0 shadow-2xl" : "-translate-x-full",
    "sm:static sm:z-auto sm:w-56 sm:max-w-none sm:shrink-0 sm:translate-x-0 sm:overflow-visible sm:shadow-none sm:transition-none",
  ].join(" ");
  const sidebarProps = {
    id: "app-sidebar",
    "data-sidebar": "",
    className: sidebarClass,
    inert: !isDesktop && !drawerOpen ? ("" as const) : undefined,
    role: drawerOpen ? ("dialog" as const) : undefined,
    "aria-modal": drawerOpen ? (true as const) : undefined,
    "aria-label": drawerOpen ? "Main menu" : undefined,
  };

  const sidebarContent = (
    <>
        <div className="mb-6 flex items-center justify-between gap-2">
          <div className="text-sm font-semibold tracking-wide text-side-fg">DCIM PLATFORM</div>
          {!isDesktop && (
            <button
              ref={closeButtonRef}
              type="button"
              aria-label="Close menu"
              onClick={() => closeMenu(true)}
              className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-sm border border-side-edge text-white hover:bg-white/5"
            >
              <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round">
                <path d="M6 6l12 12M18 6 6 18" />
              </svg>
            </button>
          )}
        </div>
        <nav aria-label="Primary" className="space-y-1">
          {navItems.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"} onClick={() => setOpen(false)} className={navLinkClass}>
              {item.label}
            </NavLink>
          ))}
          {showAdminHeading && (
            <div className="mb-1 mt-4 px-3 text-xs font-semibold uppercase tracking-wide text-side-fg-muted">Admin</div>
          )}
          {[...(isCatalogAdministrator ? ADMIN_NAV_ITEMS : []), ...accessItems].map((item) => (
            <NavLink key={item.to} to={item.to} onClick={() => setOpen(false)} className={navLinkClass}>
              {item.label}
            </NavLink>
          ))}
        </nav>
    </>
  );

  return (
    <div className="flex min-h-screen flex-col bg-surface-page text-fg-primary sm:flex-row" onKeyDown={handleKeyDown}>
      <a href="#main-content" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:rounded-sm focus:bg-action-primary focus:px-3 focus:py-2 focus:text-action-primary-fg">
        Skip to main content
      </a>
      {drawerOpen && (
        <div data-testid="menu-scrim" aria-hidden="true" className="fixed inset-0 z-30 bg-black/60" onClick={() => closeMenu(true)} />
      )}
      {isDesktop ? (
        <aside ref={setSidebarNode} {...sidebarProps}>
          {sidebarContent}
        </aside>
      ) : (
        <div ref={setSidebarNode} {...sidebarProps}>
          {sidebarContent}
        </div>
      )}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center justify-between gap-3 border-b border-line-subtle bg-surface-panel px-4 py-3 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            {!isDesktop && (
              <button
                ref={menuButtonRef}
                type="button"
                aria-label="Open main menu"
                aria-expanded={drawerOpen}
                aria-controls="app-sidebar"
                onClick={() => setOpen(true)}
                className="inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-sm border border-line-control text-fg-primary hover:bg-white/5"
              >
                <svg aria-hidden="true" viewBox="0 0 24 24" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round">
                  <path d="M4 6h16M4 12h16M4 18h16" />
                </svg>
              </button>
            )}
            <span className="min-w-0 break-all text-sm text-fg-secondary">{user?.email}</span>
          </div>
          <button onClick={handleLogout} className={`shrink-0 rounded-sm px-2 text-sm text-fg-secondary hover:text-fg-primary ${TOUCH_TARGET}`}>
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
