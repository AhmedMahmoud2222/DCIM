import { describe, expect, it } from "vitest";

import {
  clampView,
  clusterSites,
  fitView,
  hasCoordinates,
  indexGeography,
  INITIAL_VIEW,
  panView,
  placedSitesUnder,
  PlacedSite,
  project,
  viewBox,
  ZOOM_LEVELS,
  zoomView,
} from "@/features/dashboard/geographyModel";
import type { Geography, GeographySite } from "@/types";

const site = (id: string, latitude: number | null, longitude: number | null, city_id = "city"): GeographySite => ({
  id,
  code: id.toUpperCase(),
  name: `Site ${id}`,
  city_id,
  latitude,
  longitude,
});

describe("projection", () => {
  it("is equirectangular on a 360 x 180 plane", () => {
    expect(project(site("a", 0, 0) as PlacedSite)).toEqual({ x: 180, y: 90 });
    expect(project(site("b", 90, -180) as PlacedSite)).toEqual({ x: 0, y: 0 });
    expect(project(site("c", -90, 180) as PlacedSite)).toEqual({ x: 360, y: 180 });
  });

  it("places only sites with both coordinates in range", () => {
    expect(hasCoordinates(site("a", 25.2, 55.3))).toBe(true);
    expect(hasCoordinates(site("b", null, 55.3))).toBe(false);
    expect(hasCoordinates(site("c", 25.2, null))).toBe(false);
    expect(hasCoordinates(site("d", 91, 0))).toBe(false);
    expect(hasCoordinates(site("e", 0, 181))).toBe(false);
    expect(hasCoordinates(site("f", Number.NaN, 0))).toBe(false);
  });
});

describe("view", () => {
  it("starts on the whole map", () => {
    expect(viewBox(INITIAL_VIEW)).toBe("0 0 360 180");
  });

  it("zooms by levels and never past the ends", () => {
    expect(zoomView(INITIAL_VIEW, -1).zoomIndex).toBe(0);
    let view = INITIAL_VIEW;
    for (let i = 0; i < 20; i += 1) view = zoomView(view, 1);
    expect(view.zoomIndex).toBe(ZOOM_LEVELS.length - 1);
  });

  it("pans inside the map only", () => {
    expect(panView(INITIAL_VIEW, 1, 0)).toEqual(INITIAL_VIEW);
    const zoomed = zoomView(INITIAL_VIEW, 1);
    expect(panView(zoomed, 1, 0).cx).toBeGreaterThan(zoomed.cx);
    expect(clampView({ zoomIndex: 1, cx: 1000, cy: -50 })).toEqual({ zoomIndex: 1, cx: 270, cy: 45 });
  });

  it("fits a group of points, and a single point at a close zoom", () => {
    const near = [project(site("a", 25, 55) as PlacedSite), project(site("b", 24, 54) as PlacedSite)];
    const view = fitView(near);
    expect(view.zoomIndex).toBeGreaterThanOrEqual(3);
    expect(fitView([])).toEqual(INITIAL_VIEW);
  });
});

describe("clustering", () => {
  const dubai = site("dxb", 25.2, 55.3) as PlacedSite;
  const abuDhabi = site("auh", 24.45, 54.38) as PlacedSite;
  const london = site("lon", 51.5, -0.12) as PlacedSite;

  it("merges nearby sites at a low zoom and splits them as the zoom grows", () => {
    const low = clusterSites([dubai, abuDhabi, london], 1);
    expect(low.map((c) => c.sites.length).sort()).toEqual([1, 2]);
    const high = clusterSites([dubai, abuDhabi, london], 32);
    expect(high).toHaveLength(3);
    expect(high.every((c) => c.key.startsWith("site:"))).toBe(true);
  });

  it("places a cluster at the mean of its sites", () => {
    const [cluster] = clusterSites([dubai, abuDhabi], 1);
    expect(cluster.x).toBeCloseTo((project(dubai).x + project(abuDhabi).x) / 2);
  });
});

describe("indexGeography", () => {
  const geography: Geography = {
    totals: { countries: 2, cities: 2, sites: 3, sites_with_coordinates: 2, sites_without_coordinates: 1 },
    countries: [
      { id: "ae", name: "UAE", iso_code: "AE", city_count: 1, site_count: 2 },
      { id: "uk", name: "UK", iso_code: "GB", city_count: 1, site_count: 1 },
    ],
    cities: [
      { id: "dxb", country_id: "ae", name: "Dubai", site_count: 2 },
      { id: "lon", country_id: "uk", name: "London", site_count: 1 },
    ],
    sites: [site("s1", 25.2, 55.3, "dxb"), site("s2", null, null, "dxb"), site("s3", 51.5, -0.1, "lon")],
    sites_truncated: false,
    as_of: "2026-11-18T10:00:00Z",
  };

  it("splits placed and unplaced sites and groups by country and city", () => {
    const index = indexGeography(geography);
    expect(index.placed.map((s) => s.id)).toEqual(["s1", "s3"]);
    expect(index.unplaced.map((s) => s.id)).toEqual(["s2"]);
    expect(index.citiesByCountry.get("ae")?.map((c) => c.name)).toEqual(["Dubai"]);
    expect(index.sitesByCity.get("dxb")?.map((s) => s.id)).toEqual(["s1", "s2"]);
  });

  it("finds the placed sites under a drill-down selection", () => {
    const index = indexGeography(geography);
    expect(placedSitesUnder(index, { countryId: "ae" }).map((s) => s.id)).toEqual(["s1"]);
    expect(placedSitesUnder(index, { cityId: "lon" }).map((s) => s.id)).toEqual(["s3"]);
    expect(placedSitesUnder(index, {}).map((s) => s.id)).toEqual(["s1", "s3"]);
  });
});
