import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { loadBasemap } from "@/features/dashboard/basemap/loadBasemap";
import { tinyBasemap } from "@/features/dashboard/basemap/testFixtures";
import { GeographyPanel, WITHHELD_MESSAGE } from "@/features/dashboard/GeographyPanel";
import { getGeography } from "@/features/power/api";
import { renderWithProviders } from "@/test/renderWithProviders";
import type { Geography } from "@/types";

vi.mock("@/features/power/api");
vi.mock("@/features/dashboard/basemap/loadBasemap", () => ({ loadBasemap: vi.fn() }));

const BASEMAP = tinyBasemap();
const BUNDLED = { source: "bundled", datasetId: BASEMAP.dataset_id, version: BASEMAP.version, basemap: BASEMAP } as const;

const GEOGRAPHY: Geography = {
  coordinates: "included",
  // The totals are the server's exact authorized counts; they deliberately differ from the list lengths.
  totals: { countries: 5, cities: 9, sites: 40, sites_with_coordinates: 3, sites_without_coordinates: 1 },
  countries: [
    { id: "ae", name: "UAE", iso_code: "AE", city_count: 2, site_count: 3 },
    { id: "uk", name: "United Kingdom", iso_code: "GB", city_count: 1, site_count: 1 },
  ],
  cities: [
    { id: "dxb", country_id: "ae", name: "Dubai", site_count: 2 },
    { id: "auh", country_id: "ae", name: "Abu Dhabi", site_count: 1 },
    { id: "lon", country_id: "uk", name: "London", site_count: 1 },
  ],
  sites: [
    { id: "s1", code: "DXB1", name: "Dubai One", city_id: "dxb", latitude: 25.2, longitude: 55.3 },
    { id: "s2", code: "DXB2", name: "Dubai Two", city_id: "dxb", latitude: null, longitude: null },
    { id: "s3", code: "AUH1", name: "Abu Dhabi One", city_id: "auh", latitude: 24.45, longitude: 54.38 },
    { id: "s4", code: "LON1", name: "London One", city_id: "lon", latitude: 51.5, longitude: -0.12 },
  ],
  sites_truncated: false,
  as_of: "2026-11-18T10:00:00Z",
};

const panel = () => document.querySelector('[data-section="geography"]') as HTMLElement;
const svg = () => panel().querySelector("svg") as SVGSVGElement;
const ready = () => waitFor(() => expect(panel()).toHaveAttribute("data-state", "ready"));
const countries = () => Array.from(panel().querySelectorAll('[data-testid="basemap-country"]'));
const basemapReady = () => waitFor(() => expect(countries().length).toBeGreaterThan(0));

/** What a restricted caller receives: no coordinates and no with/without-coordinates totals. */
const WITHHELD: Geography = {
  ...GEOGRAPHY,
  coordinates: "withheld",
  totals: { ...GEOGRAPHY.totals, sites_with_coordinates: null, sites_without_coordinates: null },
  sites: GEOGRAPHY.sites.map((s) => ({ ...s, latitude: null, longitude: null })),
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getGeography).mockResolvedValue(GEOGRAPHY);
  vi.mocked(loadBasemap).mockResolvedValue(BUNDLED);
});

describe("Geographic coverage panel", () => {
  it("shows the authorized country, city and site totals from the server", async () => {
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    const stats = within(panel()).getAllByRole("definition").map((dd) => dd.textContent);
    expect(stats).toEqual(["5", "9", "40"]);
    expect(panel()).toHaveTextContent("Authorized locations only");
  });

  it("places only sites with coordinates and clusters nearby sites with a count bubble", async () => {
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    expect(within(panel()).getAllByTestId("map-site")).toHaveLength(1);
    const [cluster] = within(panel()).getAllByTestId("map-cluster");
    expect(cluster).toHaveAccessibleName("Cluster of 2 sites. Press Enter to zoom in.");
    expect(cluster).toHaveTextContent("2");
    expect(svg().getAttribute("viewBox")).toBe("0 0 360 180");
  });

  it("zooms into a cluster with Enter and splits it as the zoom grows", async () => {
    const user = userEvent.setup();
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    within(panel()).getByTestId("map-cluster").focus();
    await user.keyboard("{Enter}");
    expect(screen.getByTestId("map-status")).toHaveTextContent("Zoom 2×");
    for (let i = 0; i < 3; i += 1) await user.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(screen.getByTestId("map-status")).toHaveTextContent("Zoom 16×");
    expect(within(panel()).queryAllByTestId("map-cluster")).toHaveLength(0);
    expect(within(panel()).getAllByTestId("map-site")).toHaveLength(3);

    await user.click(screen.getByRole("button", { name: "Reset view" }));
    expect(svg().getAttribute("viewBox")).toBe("0 0 360 180");
    expect(screen.getByRole("button", { name: "Zoom out" })).toBeDisabled();
  });

  it("pans with the arrow keys and zooms with plus and minus when the map has focus", async () => {
    const user = userEvent.setup();
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    const map = screen.getByRole("group", { name: /Site map/ });
    map.focus();
    await user.keyboard("+");
    const zoomed = svg().getAttribute("viewBox");
    expect(zoomed).toBe("90 45 180 90");
    await user.keyboard("{ArrowRight}");
    expect(svg().getAttribute("viewBox")).not.toBe(zoomed);
    await user.keyboard("-");
    expect(svg().getAttribute("viewBox")).toBe("0 0 360 180");
  });

  it("selects a site marker and offers its site page", async () => {
    const user = userEvent.setup();
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    await user.click(within(panel()).getByTestId("map-site"));
    const card = screen.getByTestId("selected-site");
    expect(card).toHaveTextContent("London One (LON1)");
    expect(card).toHaveTextContent("London, United Kingdom");
    expect(within(card).getByRole("link", { name: "Open site LON1 →" })).toHaveAttribute("href", "/sites/s4");
    await user.click(within(card).getByRole("button", { name: "Close" }));
    expect(screen.queryByTestId("selected-site")).not.toBeInTheDocument();
  });

  it("drills down country → city → site in the list, with a breadcrumb, and focuses the map", async () => {
    const user = userEvent.setup();
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    const list = screen.getByTestId("geography-list");
    await user.click(within(list).getByRole("button", { name: "UAE" }));
    expect(within(list).getByRole("button", { name: "UAE" })).toHaveAttribute("aria-expanded", "true");
    const crumbs = screen.getByRole("navigation", { name: "Location drill-down" });
    expect(within(crumbs).getByText("UAE")).toHaveAttribute("aria-current", "location");
    expect(svg().getAttribute("viewBox")).not.toBe("0 0 360 180");

    await user.click(within(list).getByRole("button", { name: "Dubai" }));
    expect(within(crumbs).getByText("Dubai")).toHaveAttribute("aria-current", "location");
    expect(within(list).getByText("no coordinates")).toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "Dubai One (DXB1)" }));
    expect(within(screen.getByTestId("selected-site")).getByRole("link", { name: "Open site DXB1 →" })).toHaveAttribute("href", "/sites/s1");

    await user.click(within(crumbs).getByRole("button", { name: "All countries" }));
    expect(within(list).getByRole("button", { name: "UAE" })).toHaveAttribute("aria-expanded", "false");
    expect(svg().getAttribute("viewBox")).toBe("0 0 360 180");
  });

  it("always lists the sites without coordinates in a labelled group with links", async () => {
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    const group = screen.getByRole("region", { name: /Sites without coordinates/ });
    expect(group).toHaveTextContent("Sites without coordinates (1)");
    expect(within(group).getByRole("link", { name: "Dubai Two (DXB2)" })).toHaveAttribute("href", "/sites/s2");
    expect(within(group).getByText("Dubai, UAE")).toBeInTheDocument();
  });

  it("says so when no site has coordinates, and still lists every site", async () => {
    vi.mocked(getGeography).mockResolvedValue({
      ...GEOGRAPHY,
      sites: GEOGRAPHY.sites.map((s) => ({ ...s, latitude: null, longitude: null })),
    });
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    expect(panel()).toHaveTextContent("No authorized site has coordinates");
    expect(within(panel()).queryAllByTestId("map-site")).toHaveLength(0);
    expect(within(screen.getByRole("region", { name: /Sites without coordinates/ })).getAllByRole("link")).toHaveLength(4);
  });

  it("says plainly when the site list was truncated", async () => {
    vi.mocked(getGeography).mockResolvedValue({ ...GEOGRAPHY, sites_truncated: true });
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    expect(screen.getByTestId("geography-truncated")).toHaveTextContent("The site list was cut at 4 of 40 sites.");
  });

  it("is restricted without a request when location:read is missing", () => {
    renderWithProviders(<GeographyPanel access="denied" />);
    expect(panel()).toHaveAttribute("data-state", "restricted");
    expect(panel()).toHaveTextContent("location:read");
    expect(getGeography).not.toHaveBeenCalled();
  });
});

describe("Geographic coverage panel basemap", () => {
  it("draws every basemap country beneath the markers and highlights the countries with authorized sites", async () => {
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    await basemapReady();

    expect(countries()).toHaveLength(4);
    const lit = countries().filter((path) => path.getAttribute("data-highlighted") === "true").map((path) => path.getAttribute("data-iso2"));
    expect(lit.sort()).toEqual(["AE", "GB"]);
    const layer = panel().querySelector('[data-testid="basemap-layer"]') as SVGGElement;
    expect(layer).toHaveAttribute("aria-hidden", "true");
    expect(svg().firstElementChild).toBe(layer);
    expect(screen.getByTestId("basemap-caption")).toHaveTextContent("Basemap: Natural Earth 50m 5.1.2-r1 (bundled)");
    expect(screen.queryByTestId("basemap-note")).not.toBeInTheDocument();
    // Included mode keeps the markers and clusters as before.
    expect(within(panel()).getAllByTestId("map-site")).toHaveLength(1);
    expect(within(panel()).getAllByTestId("map-cluster")).toHaveLength(1);
  });

  it("does not rebuild the country paths when the view pans or zooms", async () => {
    const user = userEvent.setup();
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    await basemapReady();
    const before = countries();
    await user.click(screen.getByRole("button", { name: "Zoom in" }));
    expect(svg().getAttribute("viewBox")).toBe("90 45 180 90");
    const after = countries();
    expect(after).toHaveLength(before.length);
    after.forEach((path, i) => expect(path).toBe(before[i]));
  });

  it("matches lowercase API iso codes and never highlights entities without an iso2", async () => {
    vi.mocked(getGeography).mockResolvedValue({ ...GEOGRAPHY, countries: [{ ...GEOGRAPHY.countries[0], iso_code: "ae" }, { ...GEOGRAPHY.countries[1], iso_code: null }] });
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    await basemapReady();
    const lit = countries().filter((path) => path.getAttribute("data-highlighted") === "true");
    expect(lit.map((path) => path.getAttribute("data-iso2"))).toEqual(["AE"]);
  });

  it("shows the rejection note of a refused custom basemap as a status line", async () => {
    vi.mocked(loadBasemap).mockResolvedValue({
      ...BUNDLED,
      note: "Custom basemap 5.2.0 was rejected (checksum mismatch); using bundled basemap 5.1.2-r1.",
    });
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    const note = await screen.findByTestId("basemap-note");
    expect(note.parentElement).toHaveAttribute("role", "status");
    expect(note).toHaveTextContent("Basemap notice:");
    expect(note).toHaveTextContent("Custom basemap 5.2.0 was rejected (checksum mismatch); using bundled basemap 5.1.2-r1.");
  });

  it("captions an override as custom", async () => {
    vi.mocked(loadBasemap).mockResolvedValue({ ...BUNDLED, source: "override", datasetId: "custom-borders", version: "5.2.0" });
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    await waitFor(() => expect(screen.getByTestId("basemap-caption")).toHaveTextContent("Basemap: custom-borders 5.2.0 (custom)"));
  });

  it("keeps the graticule and the markers while the basemap is loading or unavailable", async () => {
    vi.mocked(loadBasemap).mockReturnValue(new Promise(() => undefined));
    const { unmount } = renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    expect(screen.getByTestId("basemap-caption")).toHaveTextContent("Basemap: loading");
    expect(within(panel()).getAllByTestId("map-site")).toHaveLength(1);
    expect(svg().querySelectorAll("line").length).toBe(20);
    unmount();

    vi.mocked(loadBasemap).mockResolvedValue({
      source: "unavailable",
      datasetId: null,
      version: null,
      basemap: null,
      note: "The bundled basemap is unavailable, so only the graticule is drawn.",
    });
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    await waitFor(() => expect(screen.getByTestId("basemap-caption")).toHaveTextContent("Basemap: unavailable; only the graticule is drawn."));
    expect(countries()).toHaveLength(0);
    expect(svg().querySelectorAll("line").length).toBe(20);
    expect(within(panel()).getAllByTestId("map-site")).toHaveLength(1);
    expect(screen.getByTestId("basemap-note")).toHaveTextContent("only the graticule is drawn");
  });

  it("does not load the basemap without location:read", () => {
    renderWithProviders(<GeographyPanel access="denied" />);
    expect(loadBasemap).not.toHaveBeenCalled();
  });
});

describe("Geographic coverage panel with coordinates withheld", () => {
  beforeEach(() => {
    vi.mocked(getGeography).mockResolvedValue(WITHHELD);
  });

  it("draws no marker or cluster, says why, and still highlights the countries", async () => {
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    await basemapReady();

    expect(within(panel()).queryAllByTestId("map-site")).toHaveLength(0);
    expect(within(panel()).queryAllByTestId("map-cluster")).toHaveLength(0);
    expect(screen.getByTestId("coordinates-withheld")).toHaveTextContent(WITHHELD_MESSAGE);
    expect(screen.getByTestId("map-status")).toHaveTextContent("site markers withheld");
    expect(panel()).not.toHaveTextContent("No authorized site has coordinates");
    expect(panel()).not.toHaveTextContent(/\d+ placed on the map/);
    expect(countries().filter((path) => path.getAttribute("data-highlighted") === "true")).toHaveLength(2);
  });

  it("never places a marker in withheld mode, even if a row still carried coordinates", async () => {
    vi.mocked(getGeography).mockResolvedValue({ ...WITHHELD, sites: GEOGRAPHY.sites });
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    expect(within(panel()).queryAllByTestId("map-site")).toHaveLength(0);
    expect(within(panel()).queryAllByTestId("map-cluster")).toHaveLength(0);
  });

  it("replaces the sites-without-coordinates group with the withheld message", async () => {
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();

    expect(screen.queryByTestId("sites-without-coordinates")).not.toBeInTheDocument();
    expect(panel()).not.toHaveTextContent("Sites without coordinates");
    expect(screen.getByTestId("coordinates-withheld-list")).toHaveTextContent("Withheld for your access level, so no site is placed on the map.");
    expect(screen.getByTestId("coordinates-withheld")).toHaveTextContent(WITHHELD_MESSAGE);
  });

  it("keeps the complete list, and a country click focuses the map on that country's shape", async () => {
    const user = userEvent.setup();
    renderWithProviders(<GeographyPanel access="granted" />);
    await ready();
    await basemapReady();

    const list = screen.getByTestId("geography-list");
    await user.click(within(list).getByRole("button", { name: "UAE" }));
    // The UAE box is about 5 x 3.5 degrees, which fits the closest zoom.
    expect(screen.getByTestId("map-status")).toHaveTextContent("Zoom 32×");
    expect(panel().querySelector('[data-selected="true"]')).toHaveAttribute("data-iso2", "AE");
    const [x, y, w, h] = (svg().getAttribute("viewBox") ?? "").split(" ").map(Number);
    expect(x + w / 2).toBeCloseTo(233.95);
    expect(y + h / 2).toBeCloseTo(65.65);

    await user.click(within(list).getByRole("button", { name: "Dubai" }));
    expect(within(list).queryByText("no coordinates")).not.toBeInTheDocument();
    await user.click(within(list).getByRole("button", { name: "Dubai Two (DXB2)" }));
    expect(screen.getByTestId("selected-site")).toHaveTextContent("Coordinates withheld for your access level");

    await user.click(within(list).getByRole("button", { name: "United Kingdom" }));
    expect(screen.getByTestId("map-status")).toHaveTextContent("Zoom 8×");
    const [ux, , uw] = (svg().getAttribute("viewBox") ?? "").split(" ").map(Number);
    expect(ux + uw / 2).toBeCloseTo(177);
  });
});
