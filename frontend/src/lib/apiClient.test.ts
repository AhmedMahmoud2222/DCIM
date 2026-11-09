import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch, refreshAccessToken } from "@/lib/apiClient";
import { useAuthStore } from "@/lib/authStore";

describe("refreshAccessToken failure policy", () => {
  beforeEach(() => {
    useAuthStore.setState({ accessToken: null, user: null, isInitialized: false });
    vi.unstubAllGlobals();
  });

  it("applies clear-on-failure per caller while sharing the refresh request", async () => {
    let finishRequest!: (response: Response) => void;
    const fetchMock = vi.fn(() => new Promise<Response>((resolve) => { finishRequest = resolve; }));
    vi.stubGlobal("fetch", fetchMock);

    const bootstrapRefresh = refreshAccessToken({ clearOnFailure: false });
    const normalRequestRefresh = refreshAccessToken();
    useAuthStore.getState().setSession("existing-token", null);
    finishRequest(new Response(JSON.stringify({ title: "Unavailable", detail: "Retry later" }), { status: 503 }));

    const results = await Promise.allSettled([bootstrapRefresh, normalRequestRefresh]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(results.every((result) => result.status === "rejected" && result.reason instanceof ApiError)).toBe(true);
    expect(useAuthStore.getState()).toMatchObject({ isInitialized: true, accessToken: "existing-token" });
  });

  it("preserves the session on a bootstrap-only transient failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ title: "Unavailable", detail: "Retry later" }), { status: 503 }),
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
});
