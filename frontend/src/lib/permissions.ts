import { useAuthStore } from "@/lib/authStore";

/** Whether the current session holds a given permission code (e.g. "network:manage").
 * Defaults to `false` — while the session is still loading, logged out, or stale (a
 * failed token refresh clears `user` in the same store, see apiClient.ts) — so a
 * mutation control never renders as available before the server has actually granted
 * it. This is a UX convenience only: every mutation is independently re-checked and
 * enforced by the backend regardless of what this hook returns.
 *
 * Known limitation: this list is a snapshot from /auth/me at login/token refresh, so a
 * role change made elsewhere mid-session isn't reflected here until something re-syncs
 * it — the control could stay visible for a session whose access was just revoked
 * (never the reverse security-wise, since the backend still rejects the mutation).
 * apiClient.ts's apiFetch() closes that window reactively: any 403 it receives
 * re-fetches /auth/me and updates this same store, so the stale control disappears on
 * the very next render rather than staying wrong until the user reloads. There is no
 * proactive polling — the cache is only ever corrected by an actual authorization
 * failure, never a timer. */
export function useHasPermission(code: string): boolean {
  return useAuthStore((state) => state.user?.permissions.includes(code) ?? false);
}
