import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, refreshAccessToken } from "@/lib/apiClient";
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
    finishRequest(new Response(JSON.stringify({ title: "Unavailable", detail: "Retry later" }), { status: 503 }));

    const results = await Promise.allSettled([bootstrapRefresh, normalRequestRefresh]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(results.every((result) => result.status === "rejected" && result.reason instanceof ApiError)).toBe(true);
    expect(useAuthStore.getState().isInitialized).toBe(true);
    expect(useAuthStore.getState().accessToken).toBeNull();
  });

  it("preserves the session on a bootstrap-only transient failure", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ title: "Unavailable", detail: "Retry later" }), { status: 503 }),
    ));
    useAuthStore.getState().setSession("existing-token", null);

    await expect(refreshAccessToken({ clearOnFailure: false })).rejects.toBeInstanceOf(ApiError);
    expect(useAuthStore.getState()).toMatchObject({ accessToken: "existing-token", isInitialized: true });
  });
});
