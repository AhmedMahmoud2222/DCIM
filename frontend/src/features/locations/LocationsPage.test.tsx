import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as api from "@/features/locations/api";
import { LocationsPage } from "@/features/locations/LocationsPage";
import { ApiError } from "@/lib/apiClient";
import { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";
import { Building, City, Country, Floor, Organization, Page, Room, Site } from "@/types";

vi.mock("@/features/locations/api");

// Mirrors the seeded role/permission model (backend/app/application/rbac.py): Administrator
// carries location:manage + location:update, Viewer only reads. NO_ACCESS pins the UI's
// permission-aware fallback without relying on any backend change.
const LOCATION_ADMIN: CurrentUser = {
  id: "user-admin",
  email: "admin@example.com",
  full_name: "Location Admin",
  role_names: ["Administrator"],
  permission_codes: ["organization:read", "location:read", "location:manage", "location:update"],
};
const LOCATION_VIEWER: CurrentUser = {
  ...LOCATION_ADMIN,
  id: "user-viewer",
  email: "viewer@example.com",
  role_names: ["Viewer"],
  permission_codes: ["organization:read", "location:read"],
};
const NO_ACCESS: CurrentUser = { ...LOCATION_ADMIN, id: "user-none", permission_codes: [] };

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 500, offset: 0 };
}

const org: Organization = { id: "org-1", name: "Acme Corp", created_at: "2026-01-01T00:00:00Z" };
const country: Country = { id: "country-1", organization_id: "org-1", name: "UAE", iso_code: "AE" };
const city: City = { id: "city-1", country_id: "country-1", name: "Dubai" };
const site: Site = { id: "site-1", city_id: "city-1", code: "SITE1", name: "Dubai DC", timezone: "UTC" };
const building: Building = { id: "building-1", site_id: "site-1", code: "A", name: "Building A" };
const floor: Floor = { id: "floor-1", building_id: "building-1", name: "Ground", level_number: 0 };
const room: Room = { id: "room-1", floor_id: "floor-1", code: "R101", name: "Data Hall 1", room_type: "data_hall", version: 1 };

async function drillToRooms(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: /Acme Corp/ }));
  await user.click(await screen.findByRole("button", { name: /UAE/ }));
  await user.click(await screen.findByRole("button", { name: /Dubai/ }));
  await user.click(await screen.findByRole("button", { name: /Dubai DC/ }));
  await user.click(await screen.findByRole("button", { name: /Building A/ }));
  await user.click(await screen.findByRole("button", { name: /Ground/ }));
  return screen.findByRole("button", { name: /Data Hall 1/ });
}

describe("LocationsPage", () => {
  beforeEach(() => {
    vi.mocked(api.listOrganizations).mockResolvedValue(mockPage([org]));
    vi.mocked(api.listCountries).mockResolvedValue(mockPage([country]));
    vi.mocked(api.listCities).mockResolvedValue(mockPage([city]));
    vi.mocked(api.listSites).mockResolvedValue(mockPage([site]));
    vi.mocked(api.listBuildings).mockResolvedValue(mockPage([building]));
    vi.mocked(api.listFloors).mockResolvedValue(mockPage([floor]));
    vi.mocked(api.listRooms).mockResolvedValue(mockPage([room]));
  });

  it("lists organizations and shows the create form for a location:manage user", async () => {
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN });

    expect(await screen.findByRole("heading", { name: "Organizations" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Acme Corp/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create organization" })).toBeInTheDocument();
  });

  it("hides create affordances from a read-only user but still lists organizations", async () => {
    renderWithProviders(<LocationsPage />, { user: LOCATION_VIEWER });

    expect(await screen.findByRole("button", { name: /Acme Corp/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create organization" })).not.toBeInTheDocument();
  });

  it("shows no location data to a user without read permissions", async () => {
    renderWithProviders(<LocationsPage />, { user: NO_ACCESS });

    expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission to view location data.");
    expect(screen.queryByRole("heading", { name: "Organizations" })).not.toBeInTheDocument();
    expect(api.listOrganizations).not.toHaveBeenCalled();
  });

  it("reveals Countries with the selected parent id once an organization is chosen", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN });

    await user.click(await screen.findByRole("button", { name: /Acme Corp/ }));

    expect(await screen.findByRole("heading", { name: "Countries" })).toBeInTheDocument();
    await waitFor(() => expect(api.listCountries).toHaveBeenCalledWith("org-1"));
    expect(await screen.findByRole("button", { name: /UAE/ })).toBeInTheDocument();
  });

  it("validates a required field before calling the API", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN });

    await user.click(await screen.findByRole("button", { name: "Create organization" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Organization name is required.");
    expect(api.createOrganization).not.toHaveBeenCalled();
  });

  it("creates an organization with a trimmed name", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createOrganization).mockResolvedValue(org);
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN });

    await user.type(await screen.findByLabelText("Organization name"), "  New Org  ");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    await waitFor(() => expect(api.createOrganization).toHaveBeenCalledWith({ name: "New Org" }));
  });

  it("reports a duplicate (409) with actionable wording", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createOrganization).mockRejectedValue(
      new ApiError(409, "Conflict", "The request conflicts with an existing resource or constraint.", null),
    );
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN });

    await user.type(await screen.findByLabelText("Organization name"), "Acme Corp");
    await user.click(screen.getByRole("button", { name: "Create organization" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/already exists/);
  });

  it("surfaces the API's missing-parent 404 detail", async () => {
    const user = userEvent.setup();
    vi.mocked(api.createCountry).mockRejectedValue(new ApiError(404, "Not Found", "Organization org-1 not found.", null));
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN });

    await user.click(await screen.findByRole("button", { name: /Acme Corp/ }));
    await user.type(await screen.findByLabelText("Country name"), "Nowhere");
    await user.click(screen.getByRole("button", { name: "Create country" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Organization org-1 not found.");
  });

  it("edits a room with the current version sent as If-Match", async () => {
    const user = userEvent.setup();
    vi.mocked(api.updateRoom).mockResolvedValue({ ...room, name: "Data Hall 2", version: 2 });
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN });

    await user.click(await drillToRooms(user));
    const nameInput = await screen.findByLabelText("Edit room name");
    await user.clear(nameInput);
    await user.type(nameInput, "Data Hall 2");
    await user.click(screen.getByRole("button", { name: "Save room" }));

    await waitFor(() =>
      expect(api.updateRoom).toHaveBeenCalledWith("room-1", { name: "Data Hall 2", room_type: "data_hall" }, 1),
    );
  });

  it("does not offer room editing to a read-only user", async () => {
    const user = userEvent.setup();
    renderWithProviders(<LocationsPage />, { user: LOCATION_VIEWER });

    await user.click(await drillToRooms(user));

    expect(await screen.findByText(/location:update/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save room" })).not.toBeInTheDocument();
  });
});
