import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/network/api";
import { device, userWith, vendor } from "@/features/network/fixtures";
import { ProfilesPage } from "@/features/network/ProfilesPage";
import { renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/network/api");

describe("ProfilesPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(api.listVendorProfiles).mockResolvedValue([vendor]);
    vi.mocked(api.listDeviceProfiles).mockResolvedValue([device]);
    vi.mocked(api.listProfileTemplates).mockResolvedValue([]);
    vi.mocked(api.listDeviceMetricMappings).mockResolvedValue([
      { id: "m1", oid: "1.3.6.1.4.1.9.1", canonical_metric: "temperature_c", unit: "degC", scale: 0.1, value_type: "gauge", description: null },
    ]);
  });

  it("shows vendor and device profiles, protocol badges and effective mappings", async () => {
    renderWithProviders(<ProfilesPage />, { user: userWith("network_profile:read") });
    expect(await screen.findByText("Cisco")).toBeInTheDocument();
    expect(screen.getByText("LLDP")).toBeInTheDocument();
    expect(screen.getByText("CDP")).toBeInTheDocument();
    await userEvent.click(await screen.findByText("Cisco switch"));
    expect(await screen.findByText(/temperature_c \(degC\)/)).toBeInTheDocument();
    expect(screen.getByText(/model prefix C93/)).toBeInTheDocument();
  });

  it("hides every mutation control without network_profile:manage", async () => {
    renderWithProviders(<ProfilesPage />, { user: userWith("network_profile:read") });
    await screen.findByText("Cisco");
    expect(screen.queryByRole("button", { name: /retire/i })).toBeNull();
    expect(screen.queryByLabelText("Profile template")).toBeNull();
  });

  it("offers retirement to managers and sends the profile version", async () => {
    vi.mocked(api.retireDeviceProfile).mockResolvedValue({ ...device, status: "retired" });
    renderWithProviders(<ProfilesPage />, { user: userWith("network_profile:read", "network_profile:manage") });
    await userEvent.click(await screen.findByRole("button", { name: "Retire device profile cisco-switch" }));
    await waitFor(() => expect(api.retireDeviceProfile).toHaveBeenCalledWith("d1", 1));
  });

  it("states plainly that an ambiguous match selected nothing", async () => {
    vi.mocked(api.matchProfile).mockResolvedValue({
      state: "ambiguous", vendor_profile_id: "v1", device_profile_id: null, candidate_vendor_profile_ids: [],
      candidate_device_profile_ids: ["d1"], reasons: ["multiple device profiles match with equal rank"],
    });
    renderWithProviders(<ProfilesPage />, { user: userWith("network_profile:read") });
    await screen.findByText("Cisco");
    await userEvent.type(screen.getByLabelText("sys_object_id"), "1.3.6.1.4.1.9.1");
    await userEvent.click(screen.getByRole("button", { name: "Test match" }));
    expect(await screen.findByText(/No profile was selected/)).toBeInTheDocument();
    expect(screen.getByText(/multiple device profiles match/)).toBeInTheDocument();
    expect(screen.queryByText(/^Selected:/)).toBeNull();
  });
});
