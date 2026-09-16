import { create } from "zustand";

export interface CurrentUser {
  id: string;
  email: string;
  full_name: string;
}

interface AuthState {
  accessToken: string | null;
  user: CurrentUser | null;
  setSession: (accessToken: string, user: CurrentUser | null) => void;
  setAccessToken: (accessToken: string | null) => void;
  clear: () => void;
}

/** Access token lives only in memory (never localStorage) — §31 Security Hardening:
 * a stolen access token from a memory dump is a narrower window than one sitting in
 * persistent browser storage reachable by any XSS. The refresh token never reaches
 * JS at all; it is an HttpOnly cookie the browser attaches automatically. */
export const useAuthStore = create<AuthState>((set) => ({
  accessToken: null,
  user: null,
  setSession: (accessToken, user) => set({ accessToken, user }),
  setAccessToken: (accessToken) => set({ accessToken }),
  clear: () => set({ accessToken: null, user: null }),
}));
