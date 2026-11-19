import { KeyboardEvent, useMemo } from "react";

import { ProjectedBasemap } from "@/features/dashboard/basemap/projection";
import { BasemapState } from "@/features/dashboard/basemap/useBasemap";
import { BasemapProvenance } from "@/features/dashboard/BasemapProvenance";
import { CountryLayer } from "@/features/dashboard/CountryLayer";
import { placeOf, siteLabel } from "@/features/dashboard/geographyLabels";
import {
  Cluster,
  clusterSites,
  fitView,
  GeographyIndex,
  INITIAL_VIEW,
  MAP_HEIGHT,
  MAP_WIDTH,
  MapView,
  panView,
  project,
  viewBox,
  ZOOM_LEVELS,
  zoomOf,
  zoomView,
} from "@/features/dashboard/geographyModel";
import type { GeographySite } from "@/types";

const LONGITUDES = Array.from({ length: 13 }, (_, i) => i * 30);
const LATITUDES = Array.from({ length: 7 }, (_, i) => i * 30);

export interface GeographyMapProps {
  index: GeographyIndex;
  view: MapView;
  basemap: BasemapState;
  projected: ProjectedBasemap | null;
  highlighted: ReadonlySet<string>;
  selectedIso2?: string;
  selectedSiteId?: string;
  onView: (view: MapView) => void;
  onSelectSite: (site: GeographySite) => void;
}

/** The zoom controls, the SVG map with its basemap, graticule and site markers, and the provenance caption. */
export function GeographyMap({ index, view, basemap, projected, highlighted, selectedIso2, selectedSiteId, onView, onSelectSite }: GeographyMapProps) {
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
          Zoom {zoom}× · {index.withheld ? "site markers withheld" : `${clusters.length} ${clusters.length === 1 ? "marker" : "markers"}`}
        </p>
      </div>
      <div
        tabIndex={0}
        role="group"
        aria-label={index.withheld ? "Site map. Arrow keys pan, plus and minus zoom." : "Site map. Arrow keys pan, plus and minus zoom; Tab reaches the markers."}
        aria-describedby="geography-map-help"
        onKeyDown={onMapKey}
        className="relative overflow-hidden rounded-sm border border-line-subtle bg-surface-inset"
      >
        <svg viewBox={viewBox(view)} className="block h-auto w-full" style={{ aspectRatio: `${MAP_WIDTH} / ${MAP_HEIGHT}` }} preserveAspectRatio="xMidYMid meet">
          {projected && <CountryLayer shapes={projected.shapes} highlighted={highlighted} selectedIso2={selectedIso2} />}
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
        {!index.withheld && index.placed.length === 0 && (
          <p className="absolute inset-0 flex items-center justify-center p-4 text-center text-sm text-fg-secondary">
            No authorized site has coordinates, so nothing is placed on the map.
          </p>
        )}
      </div>
      <BasemapProvenance basemap={basemap} withheld={index.withheld} />
    </div>
  );
}
