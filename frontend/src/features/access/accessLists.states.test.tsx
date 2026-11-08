import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/access/api";
import { GroupsPage } from "@/features/access/GroupsPage";
import { UsersPage } from "@/features/access/UsersPage";
import { ApiError } from "@/lib/apiClient";
import type { CurrentUser } from "@/lib/authStore";
import { ADMINISTRATOR_TEST_USER, renderWithProviders } from "@/test/renderWithProviders";

vi.mock("@/features/access/api");

const READER: CurrentUser = { ...ADMINISTRATOR_TEST_USER, permission_codes: ["user:read", "group:read"] };
const page = (items: unknown[]) => ({ items, total: items.length, limit: 200, offset: 0 });
const never = () => new Promise<never>(() => undefined);
const USER = { id: "u1", email: "ops@example.com", full_name: "Ops User", is_active: true, created_at: "2026-09-29T00:00:00Z", role_names: [], groups: [], is_restricted: true };
const GROUP = { id: "g1", name: "Ops", description: null, created_at: "2026-09-29T00:00:00Z", member_count: 0, site_count: 0, member_ids: [], allow_permissions: [], deny_permissions: [], sites: [] };

const CASES = [
  {
    name: "Users",
    ui: <UsersPage />,
    what: "users",
    permission: "user:read",
    row: "ops@example.com",
    list: () => vi.mocked(api.listUsers),
    data: page([USER]),
  },
  {
    name: "Groups",
    ui: <GroupsPage />,
    what: "groups",
    permission: "group:read",
    row: "Ops",
    list: () => vi.mocked(api.listGroups),
    data: page([GROUP]),
  },
] as const;

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.listUsers).mockResolvedValue(page([]) as never);
  vi.mocked(api.listGroups).mockResolvedValue(page([]) as never);
  vi.mocked(api.getGroup).mockResolvedValue(GROUP as never);
  vi.mocked(api.getPermissionCatalog).mockResolvedValue([] as never);
});

describe.each(CASES)("$name list states", (c) => {
  it("has one h1 and lists its rows", async () => {
    c.list().mockResolvedValue(c.data as never);
    renderWithProviders(c.ui, { user: READER });

    expect(await screen.findByText(c.row)).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("announces loading politely", async () => {
    c.list().mockReturnValue(never() as never);
    renderWithProviders(c.ui, { user: READER });

    const status = await screen.findByRole("status");
    expect(status).toHaveAttribute("aria-busy", "true");
    expect(status).toHaveTextContent(`Loading ${c.what}…`);
  });

  it("shows a failure as an alert with a working retry", async () => {
    c.list().mockRejectedValue(new Error("Upstream timed out"));
    const user = userEvent.setup();
    renderWithProviders(c.ui, { user: READER });

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(`Could not load ${c.what}.`);
    expect(alert).toHaveTextContent("Upstream timed out");

    c.list().mockResolvedValue(c.data as never);
    await user.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(c.row)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(`Could not load ${c.what}.`)).not.toBeInTheDocument());
  });

  it(`shows Access restricted naming ${c.permission} for a 403`, async () => {
    c.list().mockRejectedValue(new ApiError(403, "Forbidden", "no", null));
    renderWithProviders(c.ui, { user: READER });

    expect(await screen.findByRole("heading", { level: 2, name: "Access restricted" })).toBeInTheDocument();
    expect(screen.getByText(c.permission)).toBeInTheDocument();
    expect(screen.queryByText(c.row)).not.toBeInTheDocument();
  });
});
