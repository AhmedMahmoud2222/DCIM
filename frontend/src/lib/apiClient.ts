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

async function refreshAccessToken(options: { clearOnFailure?: boolean } = {}): Promise<string> {
  // Share only the network request. Each caller applies its own failure policy below;
  // bootstrap must be able to preserve a session on transient failures while normal
  // authenticated requests still clear an expired session.
  const request = refreshInFlight ?? (refreshInFlight = (async () => {
    const csrfToken = readCookie(CSRF_COOKIE_NAME);
    const res = await fetch(`${API_BASE}/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: csrfToken ? { [CSRF_HEADER_NAME]: csrfToken } : {},
    });
    if (!res.ok) await parseProblemResponse(res);
    const body = (await res.json()) as { access_token: string };
    useAuthStore.getState().setAccessToken(body.access_token);
    return body.access_token;
  })());
  try {
    return await request;
  } catch (error) {
    const invalidSession = error instanceof ApiError && (error.status === 401 || error.status === 403);
    if (options.clearOnFailure !== false && invalidSession) useAuthStore.getState().clear();
    throw error;
  } finally {
    if (refreshInFlight === request) refreshInFlight = null;
  }
}

export async function apiFetch<T>(
  path: string,
  options: RequestInit & { idempotencyKey?: string; ifMatch?: number; clearOnRefreshFailure?: boolean } = {},
): Promise<T> {
  const { idempotencyKey, ifMatch, clearOnRefreshFailure, ...init } = options;
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
      await refreshAccessToken({ clearOnFailure: clearOnRefreshFailure });
    } catch (error) {
      const invalidSession = error instanceof ApiError && (error.status === 401 || error.status === 403);
      if (clearOnRefreshFailure === false && !invalidSession) throw error;
      // Keep the original request error visible while a transient refresh failure
      // preserves the session; subsequent requests can retry refresh when service returns.
      await parseProblemResponse(res);
    }
    // Keep the retried request outside the refresh catch so its network errors are not
    // misreported as the original request's stale 401.
    res = await doFetch();
  }
  if (!res.ok) await parseProblemResponse(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

/** For endpoints that return raw bytes (an image), not JSON — apiFetch always calls
 * res.json(), which would fail on these. A plain `<img src="...">` can't carry the
 * Authorization bearer header this API requires, so callers fetch the bytes here and
 * turn them into an object URL themselves (see GraphicsEditorPage.tsx). */
export async function authenticatedFetchBlob(path: string): Promise<Blob> {
  const token = useAuthStore.getState().accessToken;
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`${API_BASE}${path}`, { headers, credentials: "include" });
  if (!res.ok) await parseProblemResponse(res);
  return res.blob();
}

export { refreshAccessToken };
