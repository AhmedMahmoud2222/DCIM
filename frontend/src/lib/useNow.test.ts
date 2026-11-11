import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useNow } from "./useNow";

const setVisibility = (state: "visible" | "hidden") => {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => state });
  document.dispatchEvent(new Event("visibilitychange"));
};

describe("useNow", () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(1_000_000); setVisibility("visible"); });
  afterEach(() => { vi.useRealTimers(); });

  it("advances on the interval", () => {
    const { result } = renderHook(() => useNow(5_000));
    const first = result.current;
    act(() => { vi.advanceTimersByTime(5_000); });
    expect(result.current - first).toBe(5_000);
  });

  it("stops while hidden and refreshes when visible again", () => {
    const { result } = renderHook(() => useNow(5_000));
    act(() => setVisibility("hidden"));
    const frozen = result.current;
    act(() => { vi.advanceTimersByTime(20_000); });
    expect(result.current).toBe(frozen);
    act(() => setVisibility("visible"));
    expect(result.current - frozen).toBe(20_000);
  });
});
