import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AppShell } from "@/components/layout/AppShell";
import { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";

const NETWORK_READER_USER: CurrentUser = {
  id: "user-net-reader",
  email: "net-reader@example.com",
  full_name: "Network Reader",
  role_names: ["Operator"],
  permission_codes: ["network_profile:read"],
};

const NO_NETWORK_USER: CurrentUser = {
  id: "user-no-net",
  email: "viewer@example.com",
  full_name: "Plain Viewer",
  role_names: ["Viewer"],
  permission_codes: ["catalog:read"],
};

describe("AppShell navigation terminology", () => {
  it("renders Network Profiles in primary navigation for network_profile:read users", async () => {
    renderWithProviders(<AppShell />, { user: NETWORK_READER_USER });

    const navLink = screen.getByRole("link", { name: "Network Profiles" });
    expect(navLink).toBeInTheDocument();
    expect(navLink).toHaveAttribute("href", "/network/profiles");
    expect(screen.queryByRole("link", { name: "Device Profiles" })).toBeNull();
  });

  it("hides profile navigation from users without network_profile:read", async () => {
    renderWithProviders(<AppShell />, { user: NO_NETWORK_USER });

    expect(screen.queryByRole("link", { name: "Network Profiles" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Device Profiles" })).toBeNull();
  });
});
