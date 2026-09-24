import { useAuthStore } from "@/lib/authStore";

export function useHasPermission(code: string): boolean {
  return useAuthStore((s) => s.user?.permission_codes.includes(code) ?? false);
}

export function useHasRole(name: string): boolean {
  return useAuthStore((s) => s.user?.role_names.includes(name) ?? false);
}

/** Mirrors the backend's require_catalog_administrator() (app/application/rbac.py): every
 * catalog-mutation endpoint requires both a catalog:* permission code and Administrator role
 * membership, not permission alone. UI-only — the backend re-enforces this on every request
 * regardless of what this hook returns. */
export function useIsCatalogAdministrator(): boolean {
  const hasManage = useHasPermission("catalog:manage");
  const isAdministrator = useHasRole("Administrator");
  return hasManage && isAdministrator;
}
