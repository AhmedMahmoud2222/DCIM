import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch, ApiError } from "@/lib/apiClient";
import { useAuthStore } from "@/lib/authStore";

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const forbidden = () => jsonResponse(403, { title: "Forbidden", detail: "Missing required permission: network:manage", request_id: "r1" });
const refreshedMe = { id: "u1", email: "u@example.com", full_name: "U", permissions: ["network:read"] }; // network:manage was revoked

beforeEach(() => {
  useAuthStore.getState().setSession("stale-token", { id: "u1", email: "u@example.com", full_name: "U", permissions: ["network:read", "network:manage"] });
});

afterEach(() => {
  useAuthStore.getState().clear();
  vi.restoreAllMocks();
});

describe("apiFetch permission-cache handling on 403", () => {
  it("re-fetches /auth/me and updates the store's permissions when a mutation gets a 403", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/network/connections")) return Promise.resolve(forbidden());
      if (url.endsWith("/auth/me")) return Promise.resolve(jsonResponse(200, refreshedMe));
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/network/connections", { method: "POST", body: "{}" })).rejects.toBeInstanceOf(ApiError);

    expect(useAuthStore.getState().user?.permissions).toEqual(["network:read"]);
    const calledUrls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(calledUrls.some((url) => url.endsWith("/auth/me"))).toBe(true);
  });

  it("still throws the original 403 even if the capability refresh itself fails", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/network/connections")) return Promise.resolve(forbidden());
      if (url.endsWith("/auth/me")) return Promise.resolve(jsonResponse(401, { title: "Unauthorized", detail: "expired" }));
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/network/connections", { method: "POST", body: "{}" })).rejects.toMatchObject({ status: 403 });
  });

  it("does not attempt a capability refresh for a 403 on an /auth/* endpoint itself", async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      expect(String(input)).toContain("/auth/logout");
      return Promise.resolve(jsonResponse(403, { title: "Forbidden", detail: "CSRF token missing or invalid." }));
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/auth/logout", { method: "POST" })).rejects.toBeInstanceOf(ApiError);

    const calledUrls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(calledUrls.filter((url) => url.endsWith("/auth/me"))).toHaveLength(0);
  });

  it("deduplicates concurrent capability refreshes into a single /auth/me call", async () => {
    let meCalls = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/network/connections") || url.endsWith("/network/connections/x")) return Promise.resolve(forbidden());
      if (url.endsWith("/auth/me")) {
        meCalls += 1;
        return Promise.resolve(jsonResponse(200, refreshedMe));
      }
      throw new Error(`unexpected fetch ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await Promise.allSettled([
      apiFetch("/network/connections", { method: "POST", body: "{}" }),
      apiFetch("/network/connections/x", { method: "DELETE" }),
    ]);

    expect(meCalls).toBe(1);
  });

  it("does not call /auth/me when there is no access token (already logged out)", async () => {
    useAuthStore.getState().clear();
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      expect(String(input)).toContain("/network/connections");
      return Promise.resolve(forbidden());
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/network/connections", { method: "POST", body: "{}" })).rejects.toBeInstanceOf(ApiError);

    const calledUrls = fetchMock.mock.calls.map((call) => String(call[0]));
    expect(calledUrls.filter((url) => url.endsWith("/auth/me"))).toHaveLength(0);
  });
});
