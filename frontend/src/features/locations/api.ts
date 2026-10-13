import { apiFetch } from "@/lib/apiClient";
import { Building, City, Country, Floor, Organization, Page, Room, Site } from "@/types";

export async function listOrganizations(): Promise<Page<Organization>> {
  return apiFetch<Page<Organization>>("/organizations?limit=100");
}

export async function createOrganization(data: { name: string }): Promise<Organization> {
  return apiFetch<Organization>("/organizations", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function listCountries(organizationId?: string): Promise<Page<Country>> {
  const query = organizationId ? `?organization_id=${organizationId}&limit=100` : "?limit=100";
  return apiFetch<Page<Country>>(`/countries${query}`);
}

export async function createCountry(data: {
  organization_id: string;
  name: string;
  iso_code?: string;
}): Promise<Country> {
  return apiFetch<Country>("/countries", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function listCities(countryId?: string): Promise<Page<City>> {
  const query = countryId ? `?country_id=${countryId}&limit=100` : "?limit=100";
  return apiFetch<Page<City>>(`/cities${query}`);
}

export async function createCity(data: { country_id: string; name: string }): Promise<City> {
  return apiFetch<City>("/cities", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function listSites(cityId?: string): Promise<Page<Site>> {
  const query = cityId ? `?city_id=${cityId}&limit=100` : "?limit=100";
  return apiFetch<Page<Site>>(`/sites${query}`);
}

export async function getSite(siteId: string): Promise<Site> {
  return apiFetch<Site>(`/sites/${siteId}`);
}

export async function createSite(data: {
  city_id: string;
  code: string;
  name: string;
  address?: string;
  timezone?: string;
}): Promise<Site> {
  return apiFetch<Site>("/sites", {
    method: "POST",
    body: JSON.stringify({
      ...data,
      timezone: data.timezone || "UTC",
    }),
  });
}

export async function listBuildings(siteId?: string): Promise<Page<Building>> {
  const query = siteId ? `?site_id=${siteId}&limit=100` : "?limit=100";
  return apiFetch<Page<Building>>(`/buildings${query}`);
}

export async function createBuilding(data: {
  site_id: string;
  code: string;
  name: string;
}): Promise<Building> {
  return apiFetch<Building>("/buildings", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function listFloors(buildingId?: string): Promise<Page<Floor>> {
  const query = buildingId ? `?building_id=${buildingId}&limit=100` : "?limit=100";
  return apiFetch<Page<Floor>>(`/floors${query}`);
}

export async function createFloor(data: {
  building_id: string;
  name: string;
  level_number: number;
}): Promise<Floor> {
  return apiFetch<Floor>("/floors", {
    method: "POST",
    body: JSON.stringify(data),
  });
}

export async function listRooms(params?: { floor_id?: string; site_id?: string }): Promise<Page<Room>> {
  const searchParams = new URLSearchParams({ limit: "100" });
  if (params?.floor_id) searchParams.set("floor_id", params.floor_id);
  if (params?.site_id) searchParams.set("site_id", params.site_id);
  return apiFetch<Page<Room>>(`/rooms?${searchParams.toString()}`);
}

export async function getRoom(roomId: string): Promise<Room> {
  return apiFetch<Room>(`/rooms/${roomId}`);
}

export async function createRoom(data: {
  floor_id: string;
  code: string;
  name: string;
  room_type?: string;
}): Promise<Room> {
  return apiFetch<Room>("/rooms", {
    method: "POST",
    body: JSON.stringify({
      ...data,
      room_type: data.room_type || "data_hall",
    }),
  });
}

export async function updateRoom(
  roomId: string,
  data: { name?: string; room_type?: string },
  version: number,
): Promise<Room> {
  return apiFetch<Room>(`/rooms/${roomId}`, {
    method: "PATCH",
    headers: {
      "If-Match": String(version),
    },
    body: JSON.stringify(data),
  });
}
