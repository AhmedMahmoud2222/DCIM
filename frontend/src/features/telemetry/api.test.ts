import { describe, expect, it, vi } from "vitest";
import { apiFetch } from "@/lib/apiClient";
import { getLatestTelemetry } from "./api";

vi.mock("@/lib/apiClient", () => ({ apiFetch: vi.fn() }));

describe("getLatestTelemetry", () => {
  it("requests latest-per-series so Equipment Detail shows one reading per series", async () => {
    vi.mocked(apiFetch).mockResolvedValue([]);
    await getLatestTelemetry("eq-1");
    expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(1);
    const [path] = vi.mocked(apiFetch).mock.calls[0];
    expect(typeof path).toBe("string");
    expect(path).toContain("/telemetry/latest");
    const url = new URL(path, "https://local.test");
    expect(url.searchParams.get("managed_asset_id")).toBe("eq-1");
    expect(url.searchParams.get("per_series")).toBe("true");
    expect(url.searchParams.get("limit")).toBe("100");
  });
});
