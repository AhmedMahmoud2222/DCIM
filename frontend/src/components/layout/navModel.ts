/** Primary navigation: one ungrouped landing link plus five groups. Routes and the codes that
 * gate each link are unchanged from before grouping, except that the Power Topology link now
 * follows `power:read` like the new Power & Forecast link. Hiding a link is a convenience only;
 * the backend still authorizes every request. */
export interface NavItem {
  to: string;
  label: string;
  /** Link is shown only to users holding this code. */
  permission?: string;
  /** Link is shown only to catalog administrators (`catalog:manage` and the Administrator role). */
  catalogAdmin?: boolean;
  /** Extra route prefixes whose pages (such as detail pages) belong under this link. */
  alsoActiveFor?: ReadonlyArray<string>;
}

export interface NavGroup {
  id: string;
  label: string;
  items: ReadonlyArray<NavItem>;
}

export const DASHBOARD_ITEM: NavItem = { to: "/", label: "Dashboard" };

export const NAV_GROUPS: ReadonlyArray<NavGroup> = [
  {
    id: "monitoring",
    label: "Monitoring",
    items: [
      { to: "/events", label: "Events" },
      { to: "/alarm-rules", label: "Alarm Rules", permission: "alarm:read" },
      { to: "/power/analytics", label: "Power & Forecast", permission: "power:read" },
    ],
  },
  {
    id: "infrastructure",
    label: "Infrastructure",
    items: [
      { to: "/infrastructure", label: "Sites & Rooms", alsoActiveFor: ["/sites"] },
      { to: "/floor-plans", label: "Floor Plans" },
      { to: "/floor-plans/3d-layout", label: "3D Layout" },
      { to: "/racks", label: "Racks" },
      { to: "/equipment", label: "Equipment" },
      { to: "/managed-assets", label: "Managed Assets" },
      { to: "/power", label: "Power Topology", permission: "power:read" },
    ],
  },
  {
    id: "network",
    label: "Network",
    items: [
      { to: "/cables", label: "Cables", permission: "cable:read" },
      { to: "/topology/trace", label: "Path Trace", permission: "cable:read" },
      { to: "/topology/pass-throughs", label: "Pass-throughs", permission: "cable:read" },
      { to: "/network/profiles", label: "Device Profiles", permission: "network_profile:read" },
    ],
  },
  {
    id: "data-sources",
    label: "Data Sources",
    items: [
      { to: "/discovery", label: "Discovery" },
      { to: "/discovery/neighbors", label: "Neighbor Review", permission: "discovery:read" },
      { to: "/collectors", label: "Collectors" },
      { to: "/integrations", label: "Integrations" },
    ],
  },
  {
    id: "administration",
    label: "Administration",
    items: [
      { to: "/locations", label: "Location Hierarchy", permission: "location:read" },
      { to: "/admin/catalog", label: "Asset Catalog", catalogAdmin: true },
      { to: "/admin/users", label: "Users", permission: "user:read" },
      { to: "/admin/groups", label: "Groups", permission: "group:read" },
    ],
  },
];

export interface NavViewer {
  permissions: ReadonlyArray<string> | undefined;
  isCatalogAdministrator: boolean;
}

export function isNavItemVisible(item: NavItem, viewer: NavViewer): boolean {
  if (item.catalogAdmin) return viewer.isCatalogAdministrator;
  return !item.permission || (viewer.permissions?.includes(item.permission) ?? false);
}

/** Groups with at least one visible link, each reduced to its visible links. A group whose
 * children are all hidden does not appear. */
export function visibleNavGroups(viewer: NavViewer): ReadonlyArray<NavGroup> {
  return NAV_GROUPS.map((group) => ({ ...group, items: group.items.filter((item) => isNavItemVisible(item, viewer)) })).filter(
    (group) => group.items.length > 0,
  );
}

function matchesRoute(pathname: string, route: string): boolean {
  return route === "/" ? pathname === "/" : pathname === route || pathname.startsWith(`${route}/`);
}

/** The link that owns the current page: the longest matching route, so a detail page highlights
 * its parent and `/floor-plans/3d-layout` highlights 3D Layout rather than Floor Plans too. */
export function activeNavLink(pathname: string, items: ReadonlyArray<NavItem>): NavItem | undefined {
  let best: NavItem | undefined;
  let bestLength = -1;
  for (const item of items) {
    for (const route of [item.to, ...(item.alsoActiveFor ?? [])]) {
      if (matchesRoute(pathname, route) && route.length > bestLength) {
        best = item;
        bestLength = route.length;
      }
    }
  }
  return best;
}
