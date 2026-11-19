import { memo } from "react";

import { CountryShape } from "@/features/dashboard/basemap/projection";

export interface CountryLayerProps {
  shapes: CountryShape[];
  highlighted: ReadonlySet<string>;
  selectedIso2?: string;
}

/** Country polygons. Decorative: the list below is the accessible equivalent. Memoized on its props, which do
 * not change when the view pans or zooms, so the large path strings are not rebuilt on every view change. The
 * country chosen in the drill-down gets a heavier outline. */
export const CountryLayer = memo(function CountryLayer({ shapes, highlighted, selectedIso2 }: CountryLayerProps) {
  const isLit = (shape: CountryShape) => shape.iso2 !== null && highlighted.has(shape.iso2);
  const path = (shape: CountryShape, lit: boolean) => (
    <path
      key={shape.key}
      d={shape.d}
      data-testid="basemap-country"
      data-iso2={shape.iso2 ?? undefined}
      data-highlighted={lit ? "true" : undefined}
      data-selected={shape.iso2 !== null && shape.iso2 === selectedIso2 ? "true" : undefined}
      fillRule="evenodd"
      vectorEffect="non-scaling-stroke"
      strokeWidth={shape.iso2 !== null && shape.iso2 === selectedIso2 ? 2 : lit ? 1 : 0.5}
      strokeLinejoin="round"
    />
  );
  return (
    <g aria-hidden="true" data-testid="basemap-layer">
      <g className="fill-surface-hover stroke-line-control">{shapes.filter((shape) => !isLit(shape)).map((shape) => path(shape, false))}</g>
      <g className="fill-tone-info-fg/30 stroke-tone-info-fg">{shapes.filter(isLit).map((shape) => path(shape, true))}</g>
    </g>
  );
});
