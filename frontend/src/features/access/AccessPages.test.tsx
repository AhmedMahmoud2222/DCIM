import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/access/api";
import { GroupsPage } from "@/features/access/GroupsPage";
import { UsersPage } from "@/features/access/UsersPage";
import { CurrentUser } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER, renderWithProviders } from "@/test/renderWithProviders";
import { Page } from "@/types";

vi.mock("@/features/access/api");

const MANAGER: CurrentUser = {
  ...ADMINISTRATOR_TEST_USER,
  permission_codes: ["user:read", "user:manage", "group:read", "group:manage"],
};
const READER: CurrentUser = { ...ADMINISTRATOR_TEST_USER, permission_codes: ["user:read", "group:read"] };

function page<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 200, offset: 0 };
}

const USER: api.AdminUser = {
  id: "u1", email: "ops@example.com", full_name: "Ops User", is_active: true, created_at: "2026-09-29T00:00:00Z",
  role_names: [], groups: [{ id: "g1", name: "Ops" }], is_restricted: true,
};
const GROUP: api.GroupDetail = {
  id: "g1", name: "Ops", description: null, created_at: "2026-09-29T00:00:00Z", member_count: 1, site_count: 0,
  member_ids: ["u1"], allow_permissions: ["rack:read"], deny_permissions: [], sites: [],
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listUsers).mockResolvedValue(page([USER]));
  vi.mocked(api.listGroups).mockResolvedValue(page([{ ...GROUP }]));
  vi.mocked(api.getGroup).mockResolvedValue(GROUP);
  vi.mocked(api.getPermissionCatalog).mockResolvedValue([
    { code: "rack:read", resource: "rack", action: "read", description: null, site_scoped: true },
    { code: "alarm:read", resource: "alarm", action: "read", description: null, site_scoped: false },
  ]);
  vi.mocked(api.getEffectiveAccess).mockResolvedValue({
    user_id: "u1", is_active: true, unrestricted: false, role_names: [], groups: [{ id: "g1", name: "Ops" }],
    permissions: { "rack:read": ["group:Ops"] }, denied_permissions: ["dashboard:read"], inactive_permissions: ["alarm:read"],
    sites: [{ site_id: "s1", code: "DXB1", name: "Dubai 1", rack_scope: "selected", rack_ids: ["r1"] }],
  });
  vi.mocked(api.listAllSites).mockResolvedValue(page([{ id: "s1", city_id: "c", code: "DXB1", name: "Dubai 1", timezone: "UTC" }]));
  vi.mocked(api.listSiteRacks).mockResolvedValue(page([]));
});

describe("UsersPage", () => {
  it("shows a user's effective access, including denied and inactive permissions", async () => {
    renderWithProviders(<UsersPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: /ops@example.com/ }));

    expect(await screen.findByText(/Site-restricted/)).toBeInTheDocument();
    expect(screen.getByText("dashboard:read")).toBeInTheDocument();
    expect(screen.getByText("Granted but inactive")).toBeInTheDocument();
    expect(screen.getByText(/DXB1 · Dubai 1/)).toBeInTheDocument();
  });

  it("hides mutation controls from a read-only viewer", async () => {
    renderWithProviders(<UsersPage />, { user: READER });
    await screen.findByRole("button", { name: /ops@example.com/ });
    expect(screen.queryByRole("button", { name: "+ New user" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /ops@example.com/ }));
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("surfaces a server rejection to the administrator", async () => {
    vi.mocked(api.updateUser).mockRejectedValue(new (await import("@/lib/apiClient")).ApiError(409, "Conflict", "No active administrator would remain.", null));
    renderWithProviders(<UsersPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: /ops@example.com/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Deactivate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No active administrator would remain.");
  });
});

describe("GroupsPage", () => {
  it("saves allow/deny permissions as disjoint sets", async () => {
    vi.mocked(api.setGroupPermissions).mockResolvedValue(GROUP);
    renderWithProviders(<GroupsPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: /Ops/ }));
    await userEvent.click(await screen.findByRole("tab", { name: "Permissions" }));
    await userEvent.selectOptions(await screen.findByLabelText("Effect for rack:read"), "deny");
    expect(screen.getAllByText("global users only").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: "Save permissions" }));

    await waitFor(() => expect(api.setGroupPermissions).toHaveBeenCalledWith("g1", [], ["rack:read"]));
  });

  it("grants a site with least privilege: selected racks, none ticked", async () => {
    vi.mocked(api.setGroupSiteAccess).mockResolvedValue(GROUP);
    renderWithProviders(<GroupsPage />, { user: MANAGER });
    await userEvent.click(await screen.findByRole("button", { name: /Ops/ }));
    await userEvent.click(await screen.findByRole("tab", { name: "Site & rack access" }));
    await userEvent.click(await screen.findByRole("checkbox", { name: /DXB1/ }));
    await userEvent.click(screen.getByRole("button", { name: /Save site/ }));

    await waitFor(() =>
      expect(api.setGroupSiteAccess).toHaveBeenCalledWith("g1", [{ site_id: "s1", rack_scope: "selected", rack_ids: [] }]),
    );
  });

  it("gives read-only users no save buttons", async () => {
    renderWithProviders(<GroupsPage />, { user: READER });
    await userEvent.click(await screen.findByRole("button", { name: /Ops/ }));
    await userEvent.click(await screen.findByRole("tab", { name: "Permissions" }));
    expect(screen.queryByRole("button", { name: "Save permissions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Create group" })).not.toBeInTheDocument();
  });
});
