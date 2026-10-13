import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LocationsPage } from "@/features/locations/LocationsPage";
import * as locationsApi from "@/features/locations/api";
import { CurrentUser } from "@/lib/authStore";
import { renderWithProviders } from "@/test/renderWithProviders";
import { Building, City, Country, Floor, Organization, Page, Room, Site } from "@/types";

vi.mock("@/features/locations/api");

const LOCATION_ADMIN_USER: CurrentUser = {
  id: "loc-admin",
  email: "loc-admin@example.com",
  full_name: "Location Admin",
  role_names: ["Administrator"],
  permission_codes: ["organization:read", "location:read", "location:manage", "location:update"],
};

function mockPage<T>(items: T[]): Page<T> {
  return { items, total: items.length, limit: 100, offset: 0 };
}

const mockOrg: Organization = { id: "org-1", name: "Demo Org", created_at: "2026-01-01T00:00:00Z" };
const mockCountry: Country = { id: "country-1", organization_id: "org-1", name: "United States", iso_code: "US" };
const mockCity: City = { id: "city-1", country_id: "country-1", name: "Austin" };
const mockSite: Site = { id: "site-1", city_id: "city-1", code: "SITE1", name: "Austin Data Center", timezone: "UTC" };
const mockBuilding: Building = { id: "building-1", site_id: "site-1", code: "BLDG-A", name: "Building A" };
const mockFloor: Floor = { id: "floor-1", building_id: "building-1", name: "Floor 1", level_number: 1 };
const mockRoom: Room = { id: "room-1", floor_id: "floor-1", code: "ROOM-101", name: "Data Hall 1", room_type: "data_hall", version: 1 };

describe("LocationsPage", () => {
  beforeEach(() => {
    vi.mocked(locationsApi.listOrganizations).mockResolvedValue(mockPage([mockOrg]));
    vi.mocked(locationsApi.listCountries).mockResolvedValue(mockPage([mockCountry]));
    vi.mocked(locationsApi.listCities).mockResolvedValue(mockPage([mockCity]));
    vi.mocked(locationsApi.listSites).mockResolvedValue(mockPage([mockSite]));
    vi.mocked(locationsApi.listBuildings).mockResolvedValue(mockPage([mockBuilding]));
    vi.mocked(locationsApi.listFloors).mockResolvedValue(mockPage([mockFloor]));
    vi.mocked(locationsApi.listRooms).mockResolvedValue(mockPage([mockRoom]));
    vi.mocked(locationsApi.createOrganization).mockResolvedValue({ id: "org-2", name: "New Enterprise Org", created_at: "2026-01-01T00:00:00Z" });
    vi.mocked(locationsApi.createSite).mockResolvedValue({ id: "site-2", city_id: "city-1", code: "SITE2", name: "Secondary Data Center", timezone: "UTC" });
    vi.mocked(locationsApi.updateRoom).mockResolvedValue({ ...mockRoom, name: "Updated Room", version: 2 });
  });

  it("renders location management interface and switches tabs", async () => {
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN_USER });

    expect(screen.getByTestId("locations-management-page")).toBeInTheDocument();
    expect(screen.getByText("Location Management")).toBeInTheDocument();

    // Switch to Organizations tab
    await userEvent.click(screen.getByRole("button", { name: /1\. Organizations/i }));
    await waitFor(() => {
      expect(screen.getByText("Demo Org")).toBeInTheDocument();
    });

    // Switch to Sites tab
    await userEvent.click(screen.getByRole("button", { name: /4\. Sites/i }));
    await waitFor(() => {
      expect(screen.getByText("SITE1")).toBeInTheDocument();
      expect(screen.getByText("Austin Data Center")).toBeInTheDocument();
    });
  });

  it("allows creating a new organization", async () => {
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN_USER });

    await userEvent.click(screen.getByRole("button", { name: /1\. Organizations/i }));

    const input = screen.getByTestId("create-org-name");
    await userEvent.type(input, "New Enterprise Org");
    const submitBtn = screen.getByTestId("create-org-submit");
    await userEvent.click(submitBtn);

    await waitFor(() => {
      expect(locationsApi.createOrganization).toHaveBeenCalledWith({ name: "New Enterprise Org" });
    });
  });

  it("allows creating a site and editing a room", async () => {
    renderWithProviders(<LocationsPage />, { user: LOCATION_ADMIN_USER });

    // Switch to Sites tab and fill create site form
    await userEvent.click(screen.getByRole("button", { name: /4\. Sites/i }));

    const citySelect = screen.getByTestId("create-site-city-select");
    await userEvent.selectOptions(citySelect, "city-1");

    const codeInput = screen.getByTestId("create-site-code");
    const nameInput = screen.getByTestId("create-site-name");
    await userEvent.type(codeInput, "SITE2");
    await userEvent.type(nameInput, "Secondary Data Center");

    const submitBtn = screen.getByTestId("create-site-submit");
    await userEvent.click(submitBtn);

    await waitFor(() => {
      expect(locationsApi.createSite).toHaveBeenCalledWith({
        city_id: "city-1",
        code: "SITE2",
        name: "Secondary Data Center",
        address: undefined,
        timezone: "UTC",
      });
    });

    // Switch to Rooms tab and edit room
    await userEvent.click(screen.getByRole("button", { name: /7\. Rooms/i }));
    await waitFor(() => {
      expect(screen.getByText("Data Hall 1")).toBeInTheDocument();
    });

    const editBtn = screen.getByRole("button", { name: /Edit Room/i });
    await userEvent.click(editBtn);

    expect(screen.getByText("Edit Room: ROOM-101")).toBeInTheDocument();

    const saveBtn = screen.getByRole("button", { name: /Save Changes/i });
    await userEvent.click(saveBtn);

    await waitFor(() => {
      expect(locationsApi.updateRoom).toHaveBeenCalledWith("room-1", { name: "Data Hall 1", room_type: "data_hall" }, 1);
    });
  });
});
