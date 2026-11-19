import { BasemapState } from "@/features/dashboard/basemap/useBasemap";

/** Human name of a basemap dataset for the provenance caption; unknown datasets show their id. */
function datasetLabel(datasetId: string) {
  return datasetId === "natural-earth-admin0-50m" ? "Natural Earth 50m" : datasetId;
}

function basemapCaption(basemap: BasemapState) {
  if (basemap.status === "loading") return "Basemap: loading; the graticule is shown meanwhile.";
  if (basemap.status === "unavailable") return "Basemap: unavailable; only the graticule is drawn.";
  const { datasetId, version, source } = basemap.result;
  return `Basemap: ${datasetLabel(datasetId)} ${version} (${source === "override" ? "custom" : "bundled"})`;
}

function basemapNote(basemap: BasemapState) {
  if (basemap.status === "ready") return basemap.result.note;
  return basemap.status === "unavailable" ? basemap.note : undefined;
}

export interface BasemapProvenanceProps {
  basemap: BasemapState;
  /** Whether precise site coordinates are withheld from this caller. */
  withheld: boolean;
}

/** The map help text, the basemap provenance caption and any basemap notice shown under the map. */
export function BasemapProvenance({ basemap, withheld }: BasemapProvenanceProps) {
  const note = basemapNote(basemap);
  return (
    <>
      <p id="geography-map-help" className="text-xs text-fg-secondary">
        Graticule every 30°. Country borders come from a basemap packaged with the application; no map tiles or external services are used.{" "}
        {withheld ? "Countries with authorized sites are highlighted." : "Sites are placed from their recorded latitude and longitude only."}
      </p>
      <p data-testid="basemap-caption" className="text-xs text-fg-secondary">
        {basemapCaption(basemap)}
      </p>
      {/* Always mounted, so a note that arrives after the first render is announced. */}
      <div role="status" data-testid="basemap-note-region">
        {note && (
          <p data-testid="basemap-note" className="rounded-sm border border-tone-caution-fg bg-tone-caution-bg px-3 py-2 text-xs text-tone-caution-fg">
            <span className="font-semibold">Basemap notice: </span>
            {note}
          </p>
        )}
      </div>
    </>
  );
}
