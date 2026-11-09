import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "@/lib/apiClient";
import { transitionManagedAssetLifecycle } from "@/features/managed-assets/api";

vi.mock("@/lib/apiClient", async (importOriginal) => {
  const actual = await importOriginal<typeof client>();
  return { ...actual, apiFetch: vi.fn() };
});

describe("managed asset lifecycle transition API", () => {
  beforeEach(() => vi.mocked(client.apiFetch).mockReset());

  it.each(["rack", "equipment"] as const)("uses the audited lifecycle endpoint for a %s", async (assetType) => {
    const assetId = assetType === "rack" ? "rack-1" : "equipment-1";
    await transitionManagedAssetLifecycle(assetId, "installed");
    expect(client.apiFetch).toHaveBeenCalledWith(`/managed-assets/${assetId}/lifecycle-transition`, {
      method: "POST",
      body: JSON.stringify({ to_status: "installed" }),
    });
  });
});
