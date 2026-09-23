import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { useAuthStore } from "@/lib/authStore";
import { useHasPermission } from "@/lib/permissions";

afterEach(() => {
  useAuthStore.getState().clear();
});

describe("useHasPermission", () => {
  it("defaults to false while the session hasn't loaded (user is null)", () => {
    const { result } = renderHook(() => useHasPermission("network:manage"));
    expect(result.current).toBe(false);
  });

  it("returns true for a writable session that holds the code", () => {
    useAuthStore.getState().setSession("token", { id: "u1", email: "manager@example.com", full_name: "Manager", permissions: ["network:read", "network:manage"] });
    const { result } = renderHook(() => useHasPermission("network:manage"));
    expect(result.current).toBe(true);
  });

  it("returns false for a read-only session that doesn't hold the code", () => {
    useAuthStore.getState().setSession("token", { id: "u2", email: "viewer@example.com", full_name: "Viewer", permissions: ["network:read"] });
    const { result } = renderHook(() => useHasPermission("network:manage"));
    expect(result.current).toBe(false);
  });

  it("falls back to false (never throws) after a stale session is cleared", () => {
    useAuthStore.getState().setSession("token", { id: "u3", email: "x@example.com", full_name: "X", permissions: ["network:manage"] });
    useAuthStore.getState().clear();
    const { result } = renderHook(() => useHasPermission("network:manage"));
    expect(result.current).toBe(false);
  });
});
