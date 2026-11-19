import type { Geography, GeographyCity, GeographyCountry, GeographySite } from "@/types";

/** Equirectangular projection onto a 360 x 180 plane: x = longitude + 180, y = 90 - latitude. Country
 * shapes come from the locally packaged basemap (basemap/); sites are placed from the API's coordinates. */
export const MAP_WIDTH = 360;
export const MAP_HEIGHT = 180;
export const ZOOM_LEVELS = [1, 2, 4, 8, 16, 32] as const;
/** Grid cell for clustering, in map units at zoom 1. It shrinks as the zoom grows, so clusters split. */
const CELL_AT_ZOOM_1 = 18;

export interface Point {
  x: number;
  y: number;
}

export interface MapView {
  /** Index into ZOOM_LEVELS. */
  zoomIndex: number;
  cx: number;
  cy: number;
}

export const INITIAL_VIEW: MapView = { zoomIndex: 0, cx: MAP_WIDTH / 2, cy: MAP_HEIGHT / 2 };

export interface PlacedSite extends GeographySite {
  latitude: number;
  longitude: number;
}

export function hasCoordinates(site: GeographySite): site is PlacedSite {
  return (
    typeof site.latitude === "number" &&
    typeof site.longitude === "number" &&
    Number.isFinite(site.latitude) &&
    Number.isFinite(site.longitude) &&
    Math.abs(site.latitude) <= 90 &&
    Math.abs(site.longitude) <= 180
  );
}

export function project(site: PlacedSite): Point {
  return { x: site.longitude + 180, y: 90 - site.latitude };
}

export function zoomOf(view: MapView): number {
  return ZOOM_LEVELS[view.zoomIndex];
}

/** Keeps the visible window inside the map. */
export function clampView(view: MapView): MapView {
  const zoomIndex = Math.min(ZOOM_LEVELS.length - 1, Math.max(0, view.zoomIndex));
  const zoom = ZOOM_LEVELS[zoomIndex];
  const halfW = MAP_WIDTH / zoom / 2;
  const halfH = MAP_HEIGHT / zoom / 2;
  return {
    zoomIndex,
    cx: Math.min(MAP_WIDTH - halfW, Math.max(halfW, view.cx)),
    cy: Math.min(MAP_HEIGHT - halfH, Math.max(halfH, view.cy)),
  };
}

export function viewBox(view: MapView): string {
  const zoom = zoomOf(view);
  const w = MAP_WIDTH / zoom;
  const h = MAP_HEIGHT / zoom;
  return `${view.cx - w / 2} ${view.cy - h / 2} ${w} ${h}`;
}

/** Moves the view by a fraction of the visible window. */
export function panView(view: MapView, dx: number, dy: number): MapView {
  const zoom = zoomOf(view);
  return clampView({ ...view, cx: view.cx + (dx * MAP_WIDTH) / zoom / 4, cy: view.cy + (dy * MAP_HEIGHT) / zoom / 4 });
}

export function zoomView(view: MapView, delta: number, center?: Point): MapView {
  return clampView({ zoomIndex: view.zoomIndex + delta, cx: center?.x ?? view.cx, cy: center?.y ?? view.cy });
}

/** The closest view that shows every point, or the initial view when there is none. */
export function fitView(points: ReadonlyArray<Point>): MapView {
  if (points.length === 0) return INITIAL_VIEW;
  const xs = points.map((p) => p.x);
  const ys = points.map((p) => p.y);
  const minX = Math.min(...xs);
  const maxX = Math.max(...xs);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  // A margin keeps markers off the edge; a single point gets a city-scale window.
  const w = Math.max(maxX - minX, 4) * 1.3;
  const h = Math.max(maxY - minY, 2) * 1.3;
  let zoomIndex = 0;
  for (let i = ZOOM_LEVELS.length - 1; i >= 0; i -= 1) {
    if (MAP_WIDTH / ZOOM_LEVELS[i] >= w && MAP_HEIGHT / ZOOM_LEVELS[i] >= h) {
      zoomIndex = i;
      break;
    }
  }
  return clampView({ zoomIndex, cx: (minX + maxX) / 2, cy: (minY + maxY) / 2 });
}

export interface Cluster {
  key: string;
  x: number;
  y: number;
  sites: PlacedSite[];
}

/** Groups sites into grid cells whose size depends on the zoom level. A cluster sits at the mean
 * position of its sites. Output order is stable: by cell row, then column. */
export function clusterSites(sites: ReadonlyArray<PlacedSite>, zoom: number): Cluster[] {
  const cell = CELL_AT_ZOOM_1 / zoom;
  const cells = new Map<string, { col: number; row: number; sites: PlacedSite[] }>();
  for (const site of sites) {
    const { x, y } = project(site);
    const col = Math.floor(x / cell);
    const row = Math.floor(y / cell);
    const key = `${col}:${row}`;
    const entry = cells.get(key);
    if (entry) entry.sites.push(site);
    else cells.set(key, { col, row, sites: [site] });
  }
  return Array.from(cells.entries())
    .sort(([, a], [, b]) => a.row - b.row || a.col - b.col)
    .map(([key, entry]) => {
      const points = entry.sites.map(project);
      return {
        key: entry.sites.length === 1 ? `site:${entry.sites[0].id}` : `cell:${key}`,
        x: points.reduce((sum, p) => sum + p.x, 0) / points.length,
        y: points.reduce((sum, p) => sum + p.y, 0) / points.length,
        sites: entry.sites,
      };
    });
}

/** Country → city → site tree in the API's order, with lookups for the drill-down. */
export interface GeographyIndex {
  countries: GeographyCountry[];
  citiesByCountry: Map<string, GeographyCity[]>;
  sitesByCity: Map<string, GeographySite[]>;
  cityById: Map<string, GeographyCity>;
  countryById: Map<string, GeographyCountry>;
  /** True unless the API said "included": withheld coordinates are never placed, whatever the site rows hold. */
  withheld: boolean;
  placed: PlacedSite[];
  unplaced: GeographySite[];
}

export function indexGeography(geography: Geography): GeographyIndex {
  const withheld = geography.coordinates !== "included";
  const citiesByCountry = new Map<string, GeographyCity[]>();
  const sitesByCity = new Map<string, GeographySite[]>();
  for (const city of geography.cities) {
    const list = citiesByCountry.get(city.country_id);
    if (list) list.push(city);
    else citiesByCountry.set(city.country_id, [city]);
  }
  for (const site of geography.sites) {
    const list = sitesByCity.get(site.city_id);
    if (list) list.push(site);
    else sitesByCity.set(site.city_id, [site]);
  }
  return {
    countries: geography.countries,
    citiesByCountry,
    sitesByCity,
    cityById: new Map(geography.cities.map((city) => [city.id, city])),
    countryById: new Map(geography.countries.map((country) => [country.id, country])),
    withheld,
    placed: withheld ? [] : geography.sites.filter(hasCoordinates),
    unplaced: withheld ? [] : geography.sites.filter((site) => !hasCoordinates(site)),
  };
}

/** The placed sites under a country or a city, for focusing the map on a drill-down selection. */
export function placedSitesUnder(index: GeographyIndex, selection: { countryId?: string; cityId?: string }): PlacedSite[] {
  if (index.withheld) return [];
  if (selection.cityId) return (index.sitesByCity.get(selection.cityId) ?? []).filter(hasCoordinates);
  if (selection.countryId) {
    const cities = index.citiesByCountry.get(selection.countryId) ?? [];
    return cities.flatMap((city) => (index.sitesByCity.get(city.id) ?? []).filter(hasCoordinates));
  }
  return index.placed;
}
