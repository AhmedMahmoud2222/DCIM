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

describe("AppShell navigation terminology", () => {
  it("renders Device Profiles in primary navigation for network_profile:read users", async () => {
    renderWithProviders(<AppShell />, { user: NETWORK_READER_USER });

    const navLink = screen.getByRole("link", { name: "Device Profiles" });
    expect(navLink).toBeInTheDocument();
    expect(navLink).toHaveAttribute("href", "/network/profiles");
  });
});
