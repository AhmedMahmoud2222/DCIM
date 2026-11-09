import { useEffect } from "react";

import { apiFetch, refreshAccessToken } from "@/lib/apiClient";
import { CurrentUser, useAuthStore } from "@/lib/authStore";

/** Restore a session from the HttpOnly refresh cookie before protected routes redirect. */
export function AuthBootstrap() {
  const { isInitialized, setSession, clear } = useAuthStore();

  useEffect(() => {
    if (useAuthStore.getState().accessToken || isInitialized) return;

    let active = true;
    void (async () => {
      try {
        const token = await refreshAccessToken();
        const user = await apiFetch<CurrentUser>("/auth/me");
        if (active) setSession(token, user);
      } catch {
        if (active) clear();
      }
    })();

    return () => {
      active = false;
    };
  }, [clear, isInitialized, setSession]);

  return null;
}
