import { apiFetch } from "@/lib/apiClient";
import { CurrentUser, useAuthStore } from "@/lib/authStore";

interface LoginResponse {
  access_token: string;
}

function readCsrfToken(): string | null {
  const match = document.cookie.match(/(?:^|; )dcim_csrf_token=([^;]*)/);
  return match ? decodeURIComponent(match[1]) : null;
}

export function useAuth() {
  const { accessToken, user, isInitialized, setSession, clear } = useAuthStore();

  async function login(email: string, password: string): Promise<void> {
    const res = await apiFetch<LoginResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
    try {
      useAuthStore.getState().setAccessToken(res.access_token);
      const me = await apiFetch<CurrentUser>("/auth/me");
      setSession(res.access_token, me);
    } catch (error) {
      clear();
      throw error;
    }
  }

  async function logout(): Promise<void> {
    const csrfToken = readCsrfToken();
    try {
      await apiFetch("/auth/logout", {
        method: "POST",
        headers: csrfToken ? { "X-CSRF-Token": csrfToken } : {},
      });
    } finally {
      clear();
    }
  }

  return { accessToken, user, isAuthenticated: accessToken !== null, isInitialized, login, logout };
}
