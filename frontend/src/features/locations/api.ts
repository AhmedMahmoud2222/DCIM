import { apiFetch } from "@/lib/apiClient";
import { Building, City, Country, Floor, Organization, Page, Room, Site } from "@/types";

/** Authoritative location-hierarchy client (backend/app/api/v1/locations.py).
 * Create/list for every level plus the Room read/update already supported by that router;
 * there is deliberately no delete here because the API exposes none.
 *
 * Listing is offset-paged and the page size must stay within the backend's cap: every list
 * endpoint takes `limit=Query(50, ge=1, le=MAX_LIMIT)` with `MAX_LIMIT = 200`
 * (backend/app/api/pagination.py), and anything above that is a 422 — which is why
 * `HIERARCHY_PAGE_SIZE` and `HIERARCHY_MAX_LIMIT` are pinned to that ceiling and why the
 * page fetches further pages via `offset` rather than raising `limit`. */
export type { Building, City, Country, Floor, Organization, Room, Site };

/** Backend `app/api/pagination.MAX_LIMIT`. Sending a larger `limit` is rejected with 422. */
export const HIERARCHY_MAX_LIMIT = 200;
/** Page size used for every hierarchy list request; never exceeds HIERARCHY_MAX_LIMIT. */
export const HIERARCHY_PAGE_SIZE = HIERARCHY_MAX_LIMIT;

export const listOrganizations = (limit = HIERARCHY_PAGE_SIZE, offset = 0) =>
  apiFetch<Page<Organization>>(`/organizations?limit=${limit}&offset=${offset}`);

export const createOrganization = (body: { name: string }) =>
  apiFetch<Organization>("/organizations", { method: "POST", body: JSON.stringify(body) });

export const listCountries = (organizationId: string, limit = HIERARCHY_PAGE_SIZE, offset = 0) =>
  apiFetch<Page<Country>>(`/countries?organization_id=${organizationId}&limit=${limit}&offset=${offset}`);

export const createCountry = (body: { organization_id: string; name: string; iso_code?: string | null }) =>
  apiFetch<Country>("/countries", { method: "POST", body: JSON.stringify(body) });

export const listCities = (countryId: string, limit = HIERARCHY_PAGE_SIZE, offset = 0) =>
  apiFetch<Page<City>>(`/cities?country_id=${countryId}&limit=${limit}&offset=${offset}`);

export const createCity = (body: { country_id: string; name: string }) =>
  apiFetch<City>("/cities", { method: "POST", body: JSON.stringify(body) });

export const listSites = (cityId: string, limit = HIERARCHY_PAGE_SIZE, offset = 0) =>
  apiFetch<Page<Site>>(`/sites?city_id=${cityId}&limit=${limit}&offset=${offset}`);

export const getSite = (id: string) => apiFetch<Site>(`/sites/${id}`);

export const createSite = (body: { city_id: string; code: string; name: string; timezone?: string }) =>
  apiFetch<Site>("/sites", { method: "POST", body: JSON.stringify(body) });

export const listBuildings = (siteId: string, limit = HIERARCHY_PAGE_SIZE, offset = 0) =>
  apiFetch<Page<Building>>(`/buildings?site_id=${siteId}&limit=${limit}&offset=${offset}`);

export const createBuilding = (body: { site_id: string; code: string; name: string }) =>
  apiFetch<Building>("/buildings", { method: "POST", body: JSON.stringify(body) });

export const listFloors = (buildingId: string, limit = HIERARCHY_PAGE_SIZE, offset = 0) =>
  apiFetch<Page<Floor>>(`/floors?building_id=${buildingId}&limit=${limit}&offset=${offset}`);

export const createFloor = (body: { building_id: string; name: string; level_number: number }) =>
  apiFetch<Floor>("/floors", { method: "POST", body: JSON.stringify(body) });

export const listRooms = (floorId: string, limit = HIERARCHY_PAGE_SIZE, offset = 0) =>
  apiFetch<Page<Room>>(`/rooms?floor_id=${floorId}&limit=${limit}&offset=${offset}`);

export const createRoom = (body: { floor_id: string; code: string; name: string; room_type?: string }) =>
  apiFetch<Room>("/rooms", { method: "POST", body: JSON.stringify(body) });

export const getRoom = (id: string) => apiFetch<Room>(`/rooms/${id}`);

export const updateRoom = (id: string, body: { name?: string; room_type?: string }, ifMatch: number) =>
  apiFetch<Room>(`/rooms/${id}`, { method: "PATCH", body: JSON.stringify(body), ifMatch });
