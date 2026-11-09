import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch } from "@/lib/apiClient";
import { useAuthStore } from "@/lib/authStore";

import { useAuth } from "./useAuth";

vi.mock("@/lib/apiClient", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/apiClient")>()),
  apiFetch: vi.fn(),
}));

describe("useAuth session completion", () => {
  beforeEach(() => {
    useAuthStore.setState({ accessToken: null, user: null, isInitialized: false });
    vi.mocked(apiFetch).mockReset();
  });

  it("clears a partial token when login user lookup fails", async () => {
    vi.mocked(apiFetch)
      .mockResolvedValueOnce({ access_token: "partial-token" })
      .mockRejectedValueOnce(new ApiError(503, "Unavailable", "Lookup failed", null));
    const { result } = renderHook(() => useAuth());

    await act(async () => {
      await expect(result.current.login("user@example.com", "password")).rejects.toThrow("Lookup failed");
    });

    expect(useAuthStore.getState()).toMatchObject({ accessToken: null, user: null, isInitialized: true });
  });

  it("marks the session initialized when logout clears state", async () => {
    useAuthStore.setState({ accessToken: "token", user: null, isInitialized: false });
    vi.mocked(apiFetch).mockRejectedValue(new ApiError(503, "Unavailable", "Logout unavailable", null));
    const { result } = renderHook(() => useAuth());

    await act(async () => {
      await expect(result.current.logout()).rejects.toThrow("Logout unavailable");
    });

    expect(useAuthStore.getState()).toMatchObject({ accessToken: null, user: null, isInitialized: true });
  });
});
