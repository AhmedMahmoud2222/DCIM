import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ThermalBandChip } from "./ThermalBandChip";
import { BAND_META, type ThermalBand } from "./thermalState";

describe("ThermalBandChip", () => {
  it.each(Object.keys(BAND_META) as ThermalBand[])("renders the %s band with label, screen-reader prefix, glyph and reason", (band) => {
    const { container } = render(<ThermalBandChip band={band} reason="because" />);
    const chip = screen.getByTitle("because");
    expect(chip).toHaveTextContent(`Thermal state: ${BAND_META[band].label}`);
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    expect(chip.className).toContain(band === "normal" ? "tone-ok" : band === "caution" ? "tone-caution" : band === "warning" ? "tone-serious" : band === "critical" ? "tone-danger" : "tone-unknown");
  });

  it("draws a different glyph shape for each band so colour is never the only cue", () => {
    const shapes = (["normal", "caution", "warning", "critical", "unknown"] as ThermalBand[]).map((band) => {
      const { container, unmount } = render(<ThermalBandChip band={band} />);
      const html = container.querySelector("svg")!.innerHTML;
      unmount();
      return html;
    });
    expect(new Set(shapes).size).toBe(5);
  });
});
