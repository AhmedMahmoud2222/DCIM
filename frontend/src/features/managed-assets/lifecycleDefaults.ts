import { LifecycleSettingsResponse, LifecycleStateConfig } from "@/types";

export const DEFAULT_LIFECYCLE_STATES: LifecycleStateConfig[] = [
  {
    status: "planned",
    label: "Planned",
    description: "Equipment is planned or procured and awaiting physical installation.",
    badge_color: "neutral",
    display_order: 1,
    allowed_transitions: ["installed", "removed"],
  },
  {
    status: "installed",
    label: "Installed",
    description: "Equipment is physically mounted or placed and ready for commissioning.",
    badge_color: "info",
    display_order: 2,
    allowed_transitions: ["active", "maintenance", "removed"],
  },
  {
    status: "active",
    label: "Active",
    description: "Equipment is commissioned and in production service.",
    badge_color: "ok",
    display_order: 3,
    allowed_transitions: ["maintenance", "decommissioned"],
  },
  {
    status: "maintenance",
    label: "Maintenance",
    description: "Equipment is undergoing scheduled or corrective maintenance.",
    badge_color: "caution",
    display_order: 4,
    allowed_transitions: ["active", "decommissioned"],
  },
  {
    status: "decommissioned",
    label: "Decommissioned",
    description: "Equipment is permanently retired from service and awaiting physical removal.",
    badge_color: "serious",
    display_order: 5,
    allowed_transitions: ["removed"],
  },
  {
    status: "removed",
    label: "Removed",
    description: "Equipment has been physically removed from the facility.",
    badge_color: "danger",
    display_order: 6,
    allowed_transitions: [],
  },
];

export const DEFAULT_LIFECYCLE_SETTINGS: LifecycleSettingsResponse = {
  states: DEFAULT_LIFECYCLE_STATES,
  mandatory_allowed_transitions: {
    planned: ["installed", "removed"],
    installed: ["active", "maintenance", "removed"],
    active: ["maintenance", "decommissioned"],
    maintenance: ["active", "decommissioned"],
    decommissioned: ["removed"],
    removed: [],
  },
  legacy_states: ["reserved"],
  badge_colors: ["neutral", "info", "ok", "caution", "serious", "danger"],
  version: 1,
};
