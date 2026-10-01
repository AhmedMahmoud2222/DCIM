import { PermissionCatalogItem } from "./api";

export type PermissionState = "none" | "allow" | "deny";

/** Groups the flat catalog by resource for display, keeping actions sorted. */
export function groupByResource(items: PermissionCatalogItem[]): [string, PermissionCatalogItem[]][] {
  const map = new Map<string, PermissionCatalogItem[]>();
  for (const item of items) {
    const list = map.get(item.resource) ?? [];
    list.push(item);
    map.set(item.resource, list);
  }
  return [...map.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([resource, list]) => [resource, [...list].sort((x, y) => x.action.localeCompare(y.action))]);
}

/** Applies one tri-state edit and keeps allow/deny disjoint, mirroring the server rule
 * that a permission cannot be both allowed and denied. */
export function applyPermissionState(
  allow: ReadonlySet<string>,
  deny: ReadonlySet<string>,
  code: string,
  state: PermissionState,
): { allow: Set<string>; deny: Set<string> } {
  const nextAllow = new Set(allow);
  const nextDeny = new Set(deny);
  nextAllow.delete(code);
  nextDeny.delete(code);
  if (state === "allow") nextAllow.add(code);
  if (state === "deny") nextDeny.add(code);
  return { allow: nextAllow, deny: nextDeny };
}

export function permissionState(allow: ReadonlySet<string>, deny: ReadonlySet<string>, code: string): PermissionState {
  if (deny.has(code)) return "deny";
  return allow.has(code) ? "allow" : "none";
}

/** Toggles a site grant; new grants start as `selected` with no racks (least privilege). */
export function toggleSite<T extends { site_id: string; rack_scope: "all" | "selected"; rack_ids: string[] }>(
  sites: T[],
  siteId: string,
  make: () => T,
): T[] {
  return sites.some((s) => s.site_id === siteId) ? sites.filter((s) => s.site_id !== siteId) : [...sites, make()];
}
