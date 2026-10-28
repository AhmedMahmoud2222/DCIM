import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ManagedAssetsPage } from "@/features/managed-assets/ManagedAssetsPage";
import { apiFetch } from "@/lib/apiClient";
import { renderWithProviders, VIEWER_TEST_USER } from "@/test/renderWithProviders";

vi.mock("@/lib/apiClient", () => ({ apiFetch: vi.fn() }));

const asset = (id: string, lifecycle_status: string) => ({
  id,
  asset_type: "equipment",
  asset_tag: `TAG-${id}`,
  lifecycle_status,
  serial_number: null,
});

describe("ManagedAssetsPage lifecycle status", () => {
  beforeEach(() => {
    vi.mocked(apiFetch).mockResolvedValue({
      items: [asset("1", "active"), asset("2", "decommissioned"), asset("3", "quarantined")],
      total: 3,
      limit: 200,
      offset: 0,
    });
  });

  it("keeps the backend lifecycle words and marks each as a lifecycle value with an icon", async () => {
    renderWithProviders(<ManagedAssetsPage />, { user: VIEWER_TEST_USER });

    for (const word of ["active", "decommissioned"]) {
      const chip = (await screen.findByText(word)).parentElement!;
      expect(within(chip).getByText("Lifecycle:", { exact: false })).toHaveClass("sr-only");
      expect(chip.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("shows an unrecognised lifecycle value as itself instead of a known state", async () => {
    renderWithProviders(<ManagedAssetsPage />, { user: VIEWER_TEST_USER });

    const chip = (await screen.findByText("quarantined")).parentElement!;
    expect(chip).toHaveAttribute("title", expect.stringMatching(/not recognised/i));
    expect(chip).toHaveClass("border-dashed");
  });
});
