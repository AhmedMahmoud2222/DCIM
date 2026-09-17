import { useAuthStore } from "@/lib/authStore";

const API_BASE = "/api/v1";
const CSRF_COOKIE_NAME = "dcim_csrf_token";
const CSRF_HEADER_NAME = "X-CSRF-Token";

export class ApiError extends Error {
  constructor(
    public status: number,
    public title: string,
    public detail: string,
    public requestId: string | null,
  ) {
    super(detail);
  }
}

function readCookie(name: string): string | null {
  const match = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

async function parseProblemResponse(res: Response): Promise<never> {
  let body: { title?: string; detail?: string; request_id?: string | null } = {};
  try {
    body = await res.json();
  } catch {
    // response body wasn't JSON — fall through with generic detail below
  }
  throw new ApiError(res.status, body.title ?? "Error", body.detail ?? res.statusText, body.request_id ?? null);
}

let refreshInFlight: Promise<string> | null = null;

async function refreshAccessToken(): Promise<string> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    const csrfToken = readCookie(CSRF_COOKIE_NAME);
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: csrfToken ? { [CSRF_HEADER_NAME]: csrfToken } : {},
    });
    if (!res.ok) {
      useAuthStore.getState().clear();
      await parseProblemResponse(res);
    }
    const body = (await res.json()) as { access_token: string };
    useAuthStore.getState().setAccessToken(body.access_token);
    return body.access_token;
  })();
  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit & { idempotencyKey?: string; ifMatch?: number } = {},
): Promise<T> {
  const { idempotencyKey, ifMatch, ...init } = options;
  const doFetch = async (): Promise<Response> => {
    const token = useAuthStore.getState().accessToken;
    const headers = new Headers(init.headers);
    // FormData bodies (file uploads) need the browser to set Content-Type itself,
    // including the multipart boundary — an explicit application/json here would
    // otherwise silently corrupt every upload request.
    if (!(init.body instanceof FormData)) headers.set("Content-Type", "application/json");
    if (token) headers.set("Authorization", `Bearer ${token}`);
    if (idempotencyKey) headers.set("Idempotency-Key", idempotencyKey);
    if (ifMatch !== undefined) headers.set("If-Match", String(ifMatch));
    return fetch(`${API_BASE}${path}`, { ...init, headers, credentials: "include" });
  };

  let res = await doFetch();
  if (res.status === 401 && useAuthStore.getState().accessToken !== null) {
    try {
      await refreshAccessToken();
      res = await doFetch();
    } catch {
      await parseProblemResponse(res);
    }
  }
  if (!res.ok) await parseProblemResponse(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export { refreshAccessToken };
