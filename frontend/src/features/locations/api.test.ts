import { beforeEach, describe, expect, it, vi } from "vitest";

import * as client from "@/lib/apiClient";
import {
  HIERARCHY_MAX_LIMIT,
  HIERARCHY_PAGE_SIZE,
  listBuildings,
  listCities,
  listCountries,
  listFloors,
  listOrganizations,
  listRooms,
  listSites,
} from "@/features/locations/api";

vi.mock("@/lib/apiClient", async (importOriginal) => {
  const actual = await importOriginal<typeof client>();
  return { ...actual, apiFetch: vi.fn() };
});

function lastPath(): string {
  const calls = vi.mocked(client.apiFetch).mock.calls;
  return String(calls[calls.length - 1]?.[0] ?? "");
}

function limitOf(path: string): number {
  const match = /[?&]limit=(\d+)/.exec(path);
  return match ? Number(match[1]) : Number.NaN;
}

/** Guards the client/server pagination contract. The backend list endpoints accept
 * `limit` at most `MAX_LIMIT = 200` (backend/app/api/pagination.py, `Query(50, ge=1, le=MAX_LIMIT)`);
 * a larger value is a 422, which is exactly the regression this suite pins down. */
describe("location hierarchy api pagination contract", () => {
  beforeEach(() => {
    vi.mocked(client.apiFetch).mockReset();
    vi.mocked(client.apiFetch).mockResolvedValue({
      items: [],
      total: 0,
      limit: HIERARCHY_PAGE_SIZE,
      offset: 0,
    } as never);
  });

  it("pins the client page size to the backend's documented maximum", () => {
    expect(HIERARCHY_MAX_LIMIT).toBe(200);
    expect(HIERARCHY_PAGE_SIZE).toBe(HIERARCHY_MAX_LIMIT);
  });

  it("never requests a limit above the backend maximum, for any level", async () => {
    const calls: Array<() => Promise<unknown>> = [
      () => listOrganizations(),
      () => listCountries("org-1"),
      () => listCities("country-1"),
      () => listSites("city-1"),
      () => listBuildings("site-1"),
      () => listFloors("building-1"),
      () => listRooms("floor-1"),
    ];

    for (const call of calls) {
      await call();
      const path = lastPath();
      expect(limitOf(path)).toBeLessThanOrEqual(HIERARCHY_MAX_LIMIT);
      expect(path).toContain(`limit=${HIERARCHY_PAGE_SIZE}`);
    }
  });

  it("uses offset paging so lists beyond a single page stay reachable", async () => {
    await listOrganizations(HIERARCHY_PAGE_SIZE, 200);
    expect(lastPath()).toContain("offset=200");
  });
});
