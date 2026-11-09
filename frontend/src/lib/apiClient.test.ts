import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, refreshAccessToken } from "@/lib/apiClient";
import { useAuthStore } from "@/lib/authStore";

describe("refreshAccessToken failure policy", () => {
  beforeEach(() => {
    useAuthStore.setState({ accessToken: null, user: null, isInitialized: false });
    vi.unstubAllGlobals();
  });

  it("keeps the bootstrap caller's session but clears it for a normal caller sharing an invalid refresh", async () => {
    let finishRequest!: (response: Response) => void;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { finishRequest = resolve; }));
    vi.stubGlobal("fetch", fetchMock);

    const bootstrapRefresh = refreshAccessToken({ clearOnFailure: false });
    const normalRequestRefresh = refreshAccessToken();
    useAuthStore.getState().setSession("existing-token", null);
    finishRequest(new Response(JSON.stringify({ title: "Unauthorized", detail: "Session expired" }), { status: 401 }));

    const results = await Promise.allSettled([bootstrapRefresh, normalRequestRefresh]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(results.every((result) => result.status === "rejected" && result.reason instanceof ApiError)).toBe(true);
    expect(useAuthStore.getState()).toMatchObject({ isInitialized: true, accessToken: null, user: null });
  });

  it("preserves the session on a bootstrap-only invalid-refresh failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ title: "Unauthorized", detail: "Session expired" }), { status: 401 }),
    ));
    useAuthStore.getState().setSession("existing-token", null);

    await expect(refreshAccessToken({ clearOnFailure: false })).rejects.toBeInstanceOf(ApiError);
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "existing-token", isInitialized: true });
  });

  it.each([401, 403])("clears an invalid session after HTTP %s", async (status) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ title: "Unauthorized", detail: "Session expired" }), { status }),
    ));
    useAuthStore.getState().setSession("existing-token", null);

    await expect(refreshAccessToken()).rejects.toMatchObject({ status });
    expect(useAuthStore.getState()).toMatchObject({ accessToken: null, user: null, isInitialized: true });
  });

  it("preserves the session when a refresh request fails at the network layer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    useAuthStore.getState().setSession("existing-token", null);

    await expect(refreshAccessToken()).rejects.toThrow("Failed to fetch");
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "existing-token", isInitialized: true });
  });

  it("surfaces the original 401 and keeps the session when the refresh endpoint is temporarily unavailable", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Access token expired" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Retry later" }), { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);
    useAuthStore.getState().setSession("existing-token", null);

    await expect(apiFetch("/equipment")).rejects.toMatchObject({
      status: 401,
      detail: "Access token expired",
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "existing-token", isInitialized: true });
  });

  it("propagates transient refresh failure to bootstrap instead of converting it to the stale 401", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Access token expired" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Retry later" }), { status: 503 }));
    vi.stubGlobal("fetch", fetchMock);
    useAuthStore.getState().setSession("existing-token", null);

    await expect(apiFetch("/auth/me", { clearOnRefreshFailure: false })).rejects.toMatchObject({ status: 503 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "existing-token", isInitialized: true });
  });

  it("surfaces a network failure from the retried request after refresh succeeds", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Access token expired" }), { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ access_token: "refreshed-token" }), { status: 200 }))
      .mockRejectedValueOnce(new TypeError("Network failed after refresh"));
    vi.stubGlobal("fetch", fetchMock);
    useAuthStore.getState().setSession("existing-token", null);

    await expect(apiFetch("/equipment")).rejects.toThrow("Network failed after refresh");
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(useAuthStore.getState().accessToken).toBe("refreshed-token");
  });
});
