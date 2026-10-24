import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "@/app/App";
import * as api from "@/features/network/api";
import { userWith } from "@/features/network/fixtures";
import { useAuthStore } from "@/lib/authStore";

vi.mock("@/features/network/api");

describe("legacy network profile deep link", () => {
  afterEach(() => window.history.pushState({}, "", "/"));

  it("still renders Network Profiles at /network/profiles", async () => {
    vi.mocked(api.listVendorProfiles).mockResolvedValue([]);
    vi.mocked(api.listDeviceProfiles).mockResolvedValue([]);
    vi.mocked(api.listProfileTemplates).mockResolvedValue([]);
    useAuthStore.getState().setSession("test-access-token", userWith("network_profile:read"));
    window.history.pushState({}, "", "/network/profiles");

    render(<App />);

    expect(await screen.findByRole("heading", { name: "Network Profiles" })).toBeInTheDocument();
  });
});
