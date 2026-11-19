import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { projectBasemap } from "@/features/dashboard/basemap/projection";
import { BasemapState, useBasemap } from "@/features/dashboard/basemap/useBasemap";
import { Access, DASHBOARD_PERMISSIONS } from "@/features/dashboard/dashboardModel";
import { DashboardSection } from "@/features/dashboard/DashboardSection";
import { placeOf, siteLabel } from "@/features/dashboard/geographyLabels";
import { GeographyMap } from "@/features/dashboard/GeographyMap";
import { fitView, GeographyIndex, hasCoordinates, indexGeography, INITIAL_VIEW, MapView, placedSitesUnder, Point, project } from "@/features/dashboard/geographyModel";
import { GeographyTree, Selection } from "@/features/dashboard/GeographyTree";
import { getGeography } from "@/features/power/api";
import type { Geography, GeographySite } from "@/types";

export const WITHHELD_MESSAGE =
  "Precise site coordinates are withheld for your access level. Countries with authorized sites are highlighted.";

function SiteCard({ index, site, onClose }: { index: GeographyIndex; site: GeographySite; onClose: () => void }) {
  const coordinates = index.withheld
    ? "Coordinates withheld for your access level"
    : hasCoordinates(site)
      ? `${site.latitude.toFixed(4)}, ${site.longitude.toFixed(4)}`
      : "No coordinates recorded";
  return (
    <div data-testid="selected-site" className="rounded-sm border border-line-subtle bg-surface-page p-3 text-sm">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0 [overflow-wrap:anywhere]">
          <p className="font-semibold text-fg-primary">{siteLabel(site)}</p>
          <p className="text-xs text-fg-secondary">{placeOf(index, site) || "Location not in the authorized list"}</p>
          <p className="text-xs tabular-nums text-fg-secondary">{coordinates}</p>
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

function GeographyBody({ data, basemap }: { data: Geography; basemap: BasemapState }) {
  const index = useMemo(() => indexGeography(data), [data]);
  const projected = basemap.status === "ready" ? projectBasemap(basemap.result.basemap) : null;
  const highlighted = useMemo(
    () => new Set(data.countries.flatMap((country) => (country.iso_code ? [country.iso_code.toUpperCase()] : []))),
    [data.countries],
  );
  const [selection, setSelection] = useState<Selection>({});
  const [view, setView] = useState<MapView>(INITIAL_VIEW);
  const selectedSite = selection.siteId ? data.sites.find((s) => s.id === selection.siteId) : undefined;

  /** The corners of a country's polygon bounds, when the basemap has that country. */
  function countryCorners(countryId?: string): Point[] {
    const iso2 = countryId ? index.countryById.get(countryId)?.iso_code?.toUpperCase() : undefined;
    const shape = iso2 ? projected?.byIso2.get(iso2) : undefined;
    return shape ? [{ x: shape.bounds.minX, y: shape.bounds.minY }, { x: shape.bounds.maxX, y: shape.bounds.maxY }] : [];
  }

  function focusOn(next: Selection) {
    setSelection(next);
    if (!next.countryId && !next.cityId) {
      setView(INITIAL_VIEW);
      return;
    }
    // Placed sites decide the focus; without any (coordinates withheld or not recorded) the country's shape does.
    const placed = placedSitesUnder(index, next).map(project);
    setView(fitView(placed.length > 0 ? placed : countryCorners(next.countryId)));
  }

  function selectSite(site: GeographySite) {
    const city = index.cityById.get(site.city_id);
    setSelection({ countryId: city?.country_id, cityId: site.city_id, siteId: site.id });
    if (!index.withheld && hasCoordinates(site)) setView((current) => (current.zoomIndex >= 2 ? current : fitView([project(site)])));
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
        Authorized locations only: what your account may read.
        {!index.withheld && ` ${data.totals.sites_with_coordinates ?? 0} placed on the map, ${data.totals.sites_without_coordinates ?? 0} without coordinates.`}
      </p>
      {index.withheld && (
        <p data-testid="coordinates-withheld" className="rounded-sm border border-line-control bg-surface-page px-3 py-2 text-xs text-fg-primary">
          {WITHHELD_MESSAGE}
        </p>
      )}
      {data.sites_truncated && (
        <p data-testid="geography-truncated" className="rounded-sm border border-tone-caution-fg bg-tone-caution-bg px-3 py-2 text-xs text-tone-caution-fg">
          The site list was cut at {data.sites.length} of {data.totals.sites} sites. The map and the list below are incomplete; the counts above are exact.
        </p>
      )}
      <GeographyMap
        index={index}
        view={view}
        basemap={basemap}
        projected={projected}
        highlighted={highlighted}
        selectedIso2={selection.countryId ? index.countryById.get(selection.countryId)?.iso_code?.toUpperCase() : undefined}
        selectedSiteId={selection.siteId}
        onView={setView}
        onSelectSite={selectSite}
      />
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
  const basemap = useBasemap(access === "granted");
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
      {(data) => <GeographyBody data={data} basemap={basemap} />}
    </DashboardSection>
  );
}
