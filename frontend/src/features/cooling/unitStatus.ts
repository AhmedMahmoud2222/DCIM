/** Cooling-unit marker fills. The label drawn on the marker is the fixed `UNIT_LABEL_COLOUR`, so every fill is a fixed
 * hex that reaches 4.5:1 against it (unitStatus.test.ts). Theme tokens are not used here: they change per theme
 * while the label does not, which let `online` fall to 3.15:1 and `unknown` to 2.28:1 in the Normal theme. */
export const UNIT_LABEL_COLOUR = "#f8fafc";

export const UNIT_STATUS: Record<string, { colour: string; glyph: string }> = {
  online: { colour: "#15803d", glyph: "▶" },
  standby: { colour: "#2563eb", glyph: "❚❚" },
  offline: { colour: "#5b6b82", glyph: "■" },
  fault: { colour: "#dc2626", glyph: "✕" },
  unknown: { colour: "#475569", glyph: "?" },
};
