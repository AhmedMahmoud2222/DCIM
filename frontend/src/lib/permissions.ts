import { useAuthStore } from "@/lib/authStore";

/** Whether the current session holds a given permission code (e.g. "network:manage").
 * Defaults to `false` — while the session is still loading, logged out, or stale (a
 * failed token refresh clears `user` in the same store, see apiClient.ts) — so a
 * mutation control never renders as available before the server has actually granted
 * it. This is a UX convenience only: every mutation is independently re-checked and
 * enforced by the backend regardless of what this hook returns. */
export function useHasPermission(code: string): boolean {
  return useAuthStore((state) => state.user?.permissions.includes(code) ?? false);
}
