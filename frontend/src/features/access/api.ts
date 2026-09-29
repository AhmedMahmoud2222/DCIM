import { apiFetch } from "@/lib/apiClient";
import { Page, Rack, Site } from "@/types";

export interface GroupRef {
  id: string;
  name: string;
}

export interface AdminUser {
  id: string;
  email: string;
  full_name: string;
  is_active: boolean;
  created_at: string;
  role_names: string[];
  groups: GroupRef[];
  is_restricted: boolean;
}

export interface GroupSummary {
  id: string;
  name: string;
  description: string | null;
  created_at: string;
  member_count: number;
  site_count: number;
}

export type RackScope = "all" | "selected";

export interface SiteAccess {
  site_id: string;
  site_code: string;
  site_name: string;
  rack_scope: RackScope;
  rack_ids: string[];
}

export interface GroupDetail extends GroupSummary {
  member_ids: string[];
  allow_permissions: string[];
  deny_permissions: string[];
  sites: SiteAccess[];
}

export interface PermissionCatalogItem {
  code: string;
  resource: string;
  action: string;
  description: string | null;
  site_scoped: boolean;
}

export interface EffectiveAccess {
  user_id: string;
  is_active: boolean;
  unrestricted: boolean;
  role_names: string[];
  groups: GroupRef[];
  permissions: Record<string, string[]>;
  denied_permissions: string[];
  inactive_permissions: string[];
  sites: { site_id: string; code: string; name: string; rack_scope: RackScope; rack_ids: string[] }[];
}

const json = (body: unknown) => JSON.stringify(body);

export const listUsers = (q?: string) =>
  apiFetch<Page<AdminUser>>(`/users?limit=200${q ? `&q=${encodeURIComponent(q)}` : ""}`);
export const createUser = (body: {
  email: string;
  full_name: string;
  password: string;
  group_ids: string[];
  is_active: boolean;
}) => apiFetch<AdminUser>("/users", { method: "POST", body: json(body) });
export const updateUser = (
  id: string,
  body: { full_name?: string; is_active?: boolean; password?: string; group_ids?: string[] },
) => apiFetch<AdminUser>(`/users/${id}`, { method: "PATCH", body: json(body) });
export const deleteUser = (id: string) => apiFetch<void>(`/users/${id}`, { method: "DELETE" });
export const getEffectiveAccess = (id: string) => apiFetch<EffectiveAccess>(`/users/${id}/effective-access`);

export const listGroups = () => apiFetch<Page<GroupSummary>>("/groups?limit=200");
export const getGroup = (id: string) => apiFetch<GroupDetail>(`/groups/${id}`);
export const createGroup = (body: { name: string; description?: string }) =>
  apiFetch<GroupDetail>("/groups", { method: "POST", body: json(body) });
export const updateGroup = (id: string, body: { name?: string; description?: string }) =>
  apiFetch<GroupDetail>(`/groups/${id}`, { method: "PATCH", body: json(body) });
export const deleteGroup = (id: string) => apiFetch<void>(`/groups/${id}`, { method: "DELETE" });
export const setGroupMembers = (id: string, user_ids: string[]) =>
  apiFetch<GroupDetail>(`/groups/${id}/members`, { method: "PUT", body: json({ user_ids }) });
export const setGroupPermissions = (id: string, allow: string[], deny: string[]) =>
  apiFetch<GroupDetail>(`/groups/${id}/permissions`, { method: "PUT", body: json({ allow, deny }) });
export const setGroupSiteAccess = (
  id: string,
  sites: { site_id: string; rack_scope: RackScope; rack_ids: string[] }[],
) => apiFetch<GroupDetail>(`/groups/${id}/site-access`, { method: "PUT", body: json({ sites }) });
export const getPermissionCatalog = () => apiFetch<PermissionCatalogItem[]>("/groups/permission-catalog");

export const listAllSites = () => apiFetch<Page<Site>>("/sites?limit=200");
export const listSiteRacks = (siteId: string) => apiFetch<Page<Rack>>(`/racks?limit=200&site_id=${siteId}`);
