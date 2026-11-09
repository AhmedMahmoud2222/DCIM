import { useEffect, useState } from "react";

import { ApiError, apiFetch, refreshAccessToken } from "@/lib/apiClient";
import { CurrentUser, useAuthStore } from "@/lib/authStore";

/** Restore a session from the HttpOnly refresh cookie before protected routes redirect. */
export function AuthBootstrap() {
  const { isInitialized, setSession, clear } = useAuthStore();
  const [attempt, setAttempt] = useState(0);
  const [restoreFailed, setRestoreFailed] = useState(false);

  useEffect(() => {
    if (isInitialized) return;

    let active = true;
    setRestoreFailed(false);
    void (async () => {
      try {
        const token = await refreshAccessToken({ clearOnFailure: false });
        const user = await apiFetch<CurrentUser>("/auth/me", { clearOnRefreshFailure: false });
        if (active) setSession(token, user);
      } catch (error) {
        if (!active) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          clear();
        } else {
          setRestoreFailed(true);
        }
      }
    })();

    return () => {
      active = false;
    };
  }, [attempt, clear, isInitialized, setSession]);

  if (!restoreFailed) return null;
  return (
    <div role="alert" className="m-6 rounded border border-amber-700 p-4 text-sm text-amber-200">
      <p>Your session could not be restored. Check your connection and retry, or sign in again.</p>
      <div className="mt-3 flex gap-3">
        <button type="button" onClick={() => setAttempt((current) => current + 1)} className="underline">Retry session restore</button>
        <button type="button" onClick={clear} className="underline">Go to sign in</button>
      </div>
    </div>
  );
}
