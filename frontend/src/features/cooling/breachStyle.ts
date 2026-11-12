/** Shared by the heat layer and the legend swatches so the legend always shows what the map draws. */
export const BREACH_STYLE = {
  warning: { hatch: "#fb923c", hatchSize: 6, hatchWidth: 2, hatchAngle: 45, outline: "#fed7aa", outlineWidth: 1.5, dash: "4 2", label: "Warning" },
  critical: { hatch: "#fecaca", hatchSize: 4, hatchWidth: 2.2, hatchAngle: -45, outline: "#fee2e2", outlineWidth: 2, dash: undefined, label: "Critical" },
} as const;
