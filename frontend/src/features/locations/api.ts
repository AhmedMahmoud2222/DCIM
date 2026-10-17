import { apiFetch } from "@/lib/apiClient";
import { Building, City, Country, Floor, Organization, Page, Room, Site } from "@/types";

/** Authoritative location-hierarchy client (backend/app/api/v1/locations.py).
 * Create/list for every level plus the Room read/update already supported by that router;
 * there is deliberately no delete here because the API exposes none. */
export type { Building, City, Country, Floor, Organization, Room, Site };

const HIERARCHY_LIMIT = 500;

export const listOrganizations = () =>
  apiFetch<Page<Organization>>(`/organizations?limit=${HIERARCHY_LIMIT}`);

export const createOrganization = (body: { name: string }) =>
  apiFetch<Organization>("/organizations", { method: "POST", body: JSON.stringify(body) });

export const listCountries = (organizationId: string) =>
  apiFetch<Page<Country>>(`/countries?organization_id=${organizationId}&limit=${HIERARCHY_LIMIT}`);

export const createCountry = (body: { organization_id: string; name: string; iso_code?: string | null }) =>
  apiFetch<Country>("/countries", { method: "POST", body: JSON.stringify(body) });

export const listCities = (countryId: string) =>
  apiFetch<Page<City>>(`/cities?country_id=${countryId}&limit=${HIERARCHY_LIMIT}`);

export const createCity = (body: { country_id: string; name: string }) =>
  apiFetch<City>("/cities", { method: "POST", body: JSON.stringify(body) });

export const listSites = (cityId: string) =>
  apiFetch<Page<Site>>(`/sites?city_id=${cityId}&limit=${HIERARCHY_LIMIT}`);

export const getSite = (id: string) => apiFetch<Site>(`/sites/${id}`);

export const createSite = (body: { city_id: string; code: string; name: string; timezone?: string }) =>
  apiFetch<Site>("/sites", { method: "POST", body: JSON.stringify(body) });

export const listBuildings = (siteId: string) =>
  apiFetch<Page<Building>>(`/buildings?site_id=${siteId}&limit=${HIERARCHY_LIMIT}`);

export const createBuilding = (body: { site_id: string; code: string; name: string }) =>
  apiFetch<Building>("/buildings", { method: "POST", body: JSON.stringify(body) });

export const listFloors = (buildingId: string) =>
  apiFetch<Page<Floor>>(`/floors?building_id=${buildingId}&limit=${HIERARCHY_LIMIT}`);

export const createFloor = (body: { building_id: string; name: string; level_number: number }) =>
  apiFetch<Floor>("/floors", { method: "POST", body: JSON.stringify(body) });

export const listRooms = (floorId: string) =>
  apiFetch<Page<Room>>(`/rooms?floor_id=${floorId}&limit=${HIERARCHY_LIMIT}`);

export const createRoom = (body: { floor_id: string; code: string; name: string; room_type?: string }) =>
  apiFetch<Room>("/rooms", { method: "POST", body: JSON.stringify(body) });

export const getRoom = (id: string) => apiFetch<Room>(`/rooms/${id}`);

export const updateRoom = (id: string, body: { name?: string; room_type?: string }, ifMatch: number) =>
  apiFetch<Room>(`/rooms/${id}`, { method: "PATCH", body: JSON.stringify(body), ifMatch });
