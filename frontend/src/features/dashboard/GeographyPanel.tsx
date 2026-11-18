import { useQuery } from "@tanstack/react-query";
import { KeyboardEvent, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { Access, DASHBOARD_PERMISSIONS } from "@/features/dashboard/dashboardModel";
import { DashboardSection } from "@/features/dashboard/DashboardSection";
import {
  Cluster,
  clusterSites,
  fitView,
  GeographyIndex,
  hasCoordinates,
  indexGeography,
  INITIAL_VIEW,
  MAP_HEIGHT,
  MAP_WIDTH,
  MapView,
  panView,
  placedSitesUnder,
  project,
  viewBox,
  ZOOM_LEVELS,
  zoomOf,
  zoomView,
} from "@/features/dashboard/geographyModel";
import { getGeography } from "@/features/power/api";
import type { Geography, GeographySite } from "@/types";

interface Selection {
  countryId?: string;
  cityId?: string;
  siteId?: string;
}

const LONGITUDES = Array.from({ length: 13 }, (_, i) => i * 30);
const LATITUDES = Array.from({ length: 7 }, (_, i) => i * 30);

function siteLabel(site: GeographySite) {
  return `${site.name} (${site.code})`;
}

function placeOf(index: GeographyIndex, site: GeographySite) {
  const city = index.cityById.get(site.city_id);
  const country = city ? index.countryById.get(city.country_id) : undefined;
  return [city?.name, country?.name].filter(Boolean).join(", ");
}

function GeographyMap({
  index,
  view,
  selectedSiteId,
  onView,
  onSelectSite,
}: {
  index: GeographyIndex;
  view: MapView;
  selectedSiteId?: string;
  onView: (view: MapView) => void;
  onSelectSite: (site: GeographySite) => void;
}) {
  const zoom = zoomOf(view);
  const clusters = useMemo(() => clusterSites(index.placed, zoom), [index.placed, zoom]);
  // Every size is in map units, so it is divided by the zoom to stay the same on screen.
  const unit = 1 / zoom;

  function activate(cluster: Cluster) {
    if (cluster.sites.length === 1) onSelectSite(cluster.sites[0]);
    else if (view.zoomIndex < ZOOM_LEVELS.length - 1) onView(zoomView(view, 1, { x: cluster.x, y: cluster.y }));
    else onView(fitView(cluster.sites.map(project)));
  }

  function onMapKey(event: KeyboardEvent<HTMLDivElement>) {
    if (event.target !== event.currentTarget) return;
    const moves: Record<string, [number, number]> = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (moves[event.key]) {
      event.preventDefault();
      onView(panView(view, ...moves[event.key]));
    } else if (event.key === "+" || event.key === "=") {
      event.preventDefault();
      onView(zoomView(view, 1));
    } else if (event.key === "-") {
      event.preventDefault();
      onView(zoomView(view, -1));
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex gap-1" role="group" aria-label="Map zoom">
          {[
            { label: "Zoom in", text: "+", disabled: view.zoomIndex >= ZOOM_LEVELS.length - 1, next: () => zoomView(view, 1) },
            { label: "Zoom out", text: "−", disabled: view.zoomIndex === 0, next: () => zoomView(view, -1) },
            { label: "Reset view", text: "Reset", disabled: false, next: () => INITIAL_VIEW },
          ].map((control) => (
            <button
              key={control.label}
              type="button"
              aria-label={control.label}
              disabled={control.disabled}
              onClick={() => onView(control.next())}
              className="min-h-8 min-w-8 rounded-sm border border-line-control bg-surface-panel px-2 text-sm text-fg-primary hover:bg-surface-hover disabled:opacity-50"
            >
              {control.text}
            </button>
          ))}
        </div>
        <p className="text-xs text-fg-secondary" role="status" data-testid="map-status">
          Zoom {zoom}× · {clusters.length} {clusters.length === 1 ? "marker" : "markers"}
        </p>
      </div>
      <div
        tabIndex={0}
        role="group"
        aria-label="Site map. Arrow keys pan, plus and minus zoom; Tab reaches the markers."
        aria-describedby="geography-map-help"
        onKeyDown={onMapKey}
        className="relative overflow-hidden rounded-sm border border-line-subtle bg-surface-inset"
      >
        <svg viewBox={viewBox(view)} className="block h-auto w-full" style={{ aspectRatio: `${MAP_WIDTH} / ${MAP_HEIGHT}` }} preserveAspectRatio="xMidYMid meet">
          <g aria-hidden="true" className="stroke-line-subtle" fill="none">
            {LONGITUDES.map((x) => (
              <line key={`lon${x}`} x1={x} y1={0} x2={x} y2={MAP_HEIGHT} vectorEffect="non-scaling-stroke" strokeWidth={x === 180 ? 1.5 : 1} />
            ))}
            {LATITUDES.map((y) => (
              <line key={`lat${y}`} x1={0} y1={y} x2={MAP_WIDTH} y2={y} vectorEffect="non-scaling-stroke" strokeWidth={y === 90 ? 1.5 : 1} />
            ))}
          </g>
          {clusters.map((cluster) => {
            const single = cluster.sites.length === 1;
            const site = cluster.sites[0];
            const label = single
              ? `${siteLabel(site)}, ${placeOf(index, site)}`
              : `Cluster of ${cluster.sites.length} sites. Press Enter to zoom in.`;
            const selected = single && site.id === selectedSiteId;
            const r = single ? 3.5 * unit : (7 + Math.log2(cluster.sites.length) * 1.5) * unit;
            return (
              <g
                key={cluster.key}
                role="button"
                tabIndex={0}
                aria-label={label}
                aria-pressed={single ? selected : undefined}
                data-testid={single ? "map-site" : "map-cluster"}
                onClick={() => activate(cluster)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    activate(cluster);
                  }
                }}
                className="cursor-pointer focus-visible:outline-2 focus-visible:outline-focus [&:focus-visible>circle]:stroke-focus"
              >
                <title>{single ? `${siteLabel(site)} · ${placeOf(index, site)}` : `${cluster.sites.length} sites`}</title>
                <circle
                  cx={cluster.x}
                  cy={cluster.y}
                  r={r}
                  vectorEffect="non-scaling-stroke"
                  strokeWidth={selected ? 3 : 1.5}
                  className={single ? (selected ? "fill-tone-attention-fg stroke-fg-primary" : "fill-tone-info-fg stroke-surface-panel") : "fill-tone-info-bg stroke-tone-info-fg"}
                />
                {!single && (
                  <text
                    x={cluster.x}
                    y={cluster.y}
                    dy="0.35em"
                    textAnchor="middle"
                    fontSize={7 * unit}
                    className="pointer-events-none fill-tone-info-fg font-semibold"
                  >
                    {cluster.sites.length}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
        {index.placed.length === 0 && (
          <p className="absolute inset-0 flex items-center justify-center p-4 text-center text-sm text-fg-secondary">
            No authorized site has coordinates, so nothing is placed on the map.
          </p>
        )}
      </div>
      <p id="geography-map-help" className="text-xs text-fg-secondary">
        Graticule every 30°. Sites are placed from their recorded latitude and longitude only; no map tiles or borders are drawn.
      </p>
    </div>
  );
}

function SiteCard({ index, site, onClose }: { index: GeographyIndex; site: GeographySite; onClose: () => void }) {
  return (
    <div data-testid="selected-site" className="rounded-sm border border-line-subtle bg-surface-page p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 [overflow-wrap:anywhere]">
          <p className="font-semibold text-fg-primary">{siteLabel(site)}</p>
          <p className="text-xs text-fg-secondary">{placeOf(index, site) || "Location not in the authorized list"}</p>
          <p className="text-xs tabular-nums text-fg-secondary">
            {hasCoordinates(site) ? `${site.latitude.toFixed(4)}, ${site.longitude.toFixed(4)}` : "No coordinates recorded"}
          </p>
        </div>
        <button type="button" onClick={onClose} className="min-h-8 rounded-sm border border-line-control px-2 text-xs text-fg-primary hover:bg-surface-hover">
          Close
        </button>
      </div>
      <Link className="mt-2 inline-block text-xs text-link underline underline-offset-2" to={`/sites/${site.id}`}>
        Open site {site.code} →
      </Link>
    </div>
  );
}

const linkButton = "rounded-sm text-left text-link underline underline-offset-2 hover:text-fg-primary";

function GeographyTree({
  index,
  selection,
  onCountry,
  onCity,
  onSite,
}: {
  index: GeographyIndex;
  selection: Selection;
  onCountry: (id?: string) => void;
  onCity: (id: string) => void;
  onSite: (site: GeographySite) => void;
}) {
  const country = selection.countryId ? index.countryById.get(selection.countryId) : undefined;
  const city = selection.cityId ? index.cityById.get(selection.cityId) : undefined;
  return (
    <div className="space-y-3 text-sm">
      <nav aria-label="Location drill-down">
        <ol className="flex flex-wrap items-center gap-1 text-xs">
          <li>
            {country ? (
              <button type="button" className={linkButton} onClick={() => onCountry(undefined)}>
                All countries
              </button>
            ) : (
              <span aria-current="location" className="font-semibold text-fg-primary">All countries</span>
            )}
          </li>
          {country && (
            <li className="before:mr-1 before:text-fg-secondary before:content-['›']">
              {city ? (
                <button type="button" className={linkButton} onClick={() => onCountry(country.id)}>
                  {country.name}
                </button>
              ) : (
                <span aria-current="location" className="font-semibold text-fg-primary">{country.name}</span>
              )}
            </li>
          )}
          {city && (
            <li className="before:mr-1 before:text-fg-secondary before:content-['›']">
              <span aria-current="location" className="font-semibold text-fg-primary">{city.name}</span>
            </li>
          )}
        </ol>
      </nav>

      <div role="region" aria-label="Authorized sites by country and city" tabIndex={0} className="relative max-h-72 overflow-y-auto rounded-sm border border-line-subtle p-2">
        <ul className="space-y-1" data-testid="geography-list">
          {index.countries.map((c) => {
            const open = c.id === selection.countryId;
            const cities = index.citiesByCountry.get(c.id) ?? [];
            return (
              <li key={c.id}>
                <button type="button" aria-expanded={open} className={linkButton} onClick={() => onCountry(open && !selection.cityId ? undefined : c.id)}>
                  {c.name}
                </button>{" "}
                <span className="text-xs text-fg-secondary">
                  {c.city_count} {c.city_count === 1 ? "city" : "cities"}, {c.site_count} {c.site_count === 1 ? "site" : "sites"}
                </span>
                {open && (
                  <ul className="ml-4 mt-1 space-y-1 border-l border-line-subtle pl-2">
                    {cities.length === 0 && <li className="text-xs text-fg-secondary">No authorized city.</li>}
                    {cities.map((ci) => {
                      const cityOpen = ci.id === selection.cityId;
                      const sites = index.sitesByCity.get(ci.id) ?? [];
                      return (
                        <li key={ci.id}>
                          <button type="button" aria-expanded={cityOpen} className={linkButton} onClick={() => (cityOpen ? onCountry(c.id) : onCity(ci.id))}>
                            {ci.name}
                          </button>{" "}
                          <span className="text-xs text-fg-secondary">
                            {ci.site_count} {ci.site_count === 1 ? "site" : "sites"}
                          </span>
                          {cityOpen && (
                            <ul className="ml-4 mt-1 space-y-1 border-l border-line-subtle pl-2">
                              {sites.length === 0 && <li className="text-xs text-fg-secondary">No site in the returned list.</li>}
                              {sites.map((s) => (
                                <li key={s.id} className="flex flex-wrap items-center gap-x-2">
                                  <button type="button" className={linkButton} aria-pressed={s.id === selection.siteId} onClick={() => onSite(s)}>
                                    {siteLabel(s)}
                                  </button>
                                  {!hasCoordinates(s) && <span className="text-xs text-fg-secondary">no coordinates</span>}
                                </li>
                              ))}
                            </ul>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </li>
            );
          })}
        </ul>
      </div>

      <section aria-labelledby="sites-without-coordinates" data-testid="sites-without-coordinates">
        <h3 id="sites-without-coordinates" className="text-xs font-semibold uppercase tracking-wide text-fg-secondary">
          Sites without coordinates ({index.unplaced.length})
        </h3>
        {index.unplaced.length === 0 ? (
          <p className="mt-1 text-xs text-fg-secondary">Every listed site has coordinates.</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {index.unplaced.map((s) => (
              <li key={s.id} className="flex flex-wrap items-center gap-x-2">
                <Link className="text-link underline underline-offset-2" to={`/sites/${s.id}`}>
                  {siteLabel(s)}
                </Link>
                <span className="text-xs text-fg-secondary">{placeOf(index, s)}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function GeographyBody({ data }: { data: Geography }) {
  const index = useMemo(() => indexGeography(data), [data]);
  const [selection, setSelection] = useState<Selection>({});
  const [view, setView] = useState<MapView>(INITIAL_VIEW);
  const selectedSite = selection.siteId ? data.sites.find((s) => s.id === selection.siteId) : undefined;

  function focusOn(next: Selection) {
    setSelection(next);
    setView(next.countryId || next.cityId ? fitView(placedSitesUnder(index, next).map(project)) : INITIAL_VIEW);
  }

  function selectSite(site: GeographySite) {
    const city = index.cityById.get(site.city_id);
    setSelection({ countryId: city?.country_id, cityId: site.city_id, siteId: site.id });
    if (hasCoordinates(site)) setView((current) => (current.zoomIndex >= 2 ? current : fitView([project(site)])));
  }

  return (
    <div className="space-y-3">
      <dl className="grid grid-cols-3 gap-2">
        {[
          { label: "Countries", value: data.totals.countries },
          { label: "Cities", value: data.totals.cities },
          { label: "Sites", value: data.totals.sites },
        ].map((stat) => (
          <div key={stat.label} className="rounded-sm border border-line-subtle bg-surface-page px-2 py-1.5">
            <dt className="text-xs text-fg-secondary">{stat.label}</dt>
            <dd className="text-xl font-semibold tabular-nums text-fg-primary">{stat.value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-xs text-fg-secondary">
        Authorized locations only: what your account may read. {data.totals.sites_with_coordinates} placed on the map, {data.totals.sites_without_coordinates} without coordinates.
      </p>
      {data.sites_truncated && (
        <p data-testid="geography-truncated" className="rounded-sm border border-tone-caution-fg bg-tone-caution-bg px-3 py-2 text-xs text-tone-caution-fg">
          The site list was cut at {data.sites.length} of {data.totals.sites} sites. The map and the list below are incomplete; the counts above are exact.
        </p>
      )}
      <GeographyMap index={index} view={view} selectedSiteId={selection.siteId} onView={setView} onSelectSite={selectSite} />
      {selectedSite && <SiteCard index={index} site={selectedSite} onClose={() => setSelection(({ countryId, cityId }) => ({ countryId, cityId }))} />}
      <GeographyTree
        index={index}
        selection={selection}
        onCountry={(countryId) => focusOn({ countryId })}
        onCity={(cityId) => focusOn({ countryId: index.cityById.get(cityId)?.country_id, cityId })}
        onSite={selectSite}
      />
    </div>
  );
}

export function GeographyPanel({ access, className = "" }: { access: Access; className?: string }) {
  const query = useQuery({
    queryKey: ["dashboard", "geography"],
    queryFn: async () => {
      const data = await getGeography();
      // A response without totals or lists is a failure, never an empty map.
      if (!data || typeof data.totals !== "object" || data.totals === null || ![data.countries, data.cities, data.sites].every(Array.isArray)) {
        throw new Error("The geography response was not in the expected shape.");
      }
      return data;
    },
    enabled: access === "granted",
  });
  return (
    <DashboardSection
      id="geography"
      title="Geographic coverage"
      what="geographic coverage"
      permission={DASHBOARD_PERMISSIONS.geography}
      access={access}
      query={query}
      className={className}
      isEmpty={(data) => data.totals.countries === 0 && data.totals.sites === 0 && data.sites.length === 0}
      emptyText="No authorized country, city or site is recorded."
    >
      {(data) => <GeographyBody data={data} />}
    </DashboardSection>
  );
}
