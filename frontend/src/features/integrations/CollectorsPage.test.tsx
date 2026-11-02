import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { CollectorsPage } from "@/features/integrations/CollectorsPage";
import * as api from "@/features/integrations/api";
import { renderWithProviders } from "@/test/renderWithProviders";
import { Collector } from "@/types";

vi.mock("@/features/integrations/api");

const collector = (id: string, health: Collector["health"] | string, seconds: number | null): Collector => ({
  id,
  name: `collector-${id}`,
  collector_type: "edge",
  site_id: null,
  status: "active",
  version_string: "1.0",
  health: health as Collector["health"],
  last_heartbeat_at: seconds == null ? null : "2026-10-24T11:58:00Z",
  seconds_since_heartbeat: seconds,
});

describe("CollectorsPage connectivity", () => {
  beforeEach(() => {
    vi.mocked(api.listCollectors).mockResolvedValue([
      collector("a", "healthy", 20),
      collector("b", "stale", 150),
      collector("c", "offline", 4000),
      collector("d", "degraded", null),
    ]);
    vi.mocked(api.listIntegrations).mockResolvedValue([]);
    vi.mocked(api.listCollectorCapabilities).mockResolvedValue([]);
  });

  it("shows the backend health classification unchanged, as text with an icon, labelled as collector connectivity", async () => {
    renderWithProviders(<CollectorsPage />);

    for (const [id, label] of [["a", "Healthy"], ["b", "Stale"], ["c", "Offline"], ["d", "degraded"]] as const) {
      const row = (await screen.findByText(`collector-${id}`)).closest("button")!;
      const chip = within(row).getByText(label).parentElement!;
      expect(within(chip).getByText("Collector connectivity:", { exact: false })).toHaveClass("sr-only");
      expect(chip.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    }
  });

  it("never derives or shows measurement freshness from heartbeat timestamps", async () => {
    renderWithProviders(<CollectorsPage />);

    await screen.findByText("collector-a");
    expect(screen.queryByTestId("measurement-freshness")).not.toBeInTheDocument();
    expect(screen.queryByText(/Measurement freshness/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^(Fresh|Late)$/)).not.toBeInTheDocument();
  });
});
