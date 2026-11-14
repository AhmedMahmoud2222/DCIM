import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { getLifecycleSettings } from "@/features/managed-assets/api";
import { LifecycleSettingsResponse, LifecycleStateConfig } from "@/types";

export const DEFAULT_LIFECYCLE_STATES: LifecycleStateConfig[] = [
  {
    status: "planned",
    label: "Planned",
    description: "Asset is planned or ordered and awaiting physical installation.",
    badge_color: "neutral",
    display_order: 1,
    allowed_transitions: ["installed", "removed"],
  },
  {
    status: "installed",
    label: "Installed",
    description: "Asset is physically installed in its location or rack.",
    badge_color: "info",
    display_order: 2,
    allowed_transitions: ["active", "maintenance", "decommissioned"],
  },
  {
    status: "active",
    label: "Active",
    description: "Asset is commissioned and in production service.",
    badge_color: "ok",
    display_order: 3,
    allowed_transitions: ["maintenance", "decommissioned"],
  },
  {
    status: "maintenance",
    label: "Maintenance",
    description: "Asset is undergoing scheduled or corrective maintenance.",
    badge_color: "caution",
    display_order: 4,
    allowed_transitions: ["active", "decommissioned"],
  },
  {
    status: "decommissioned",
    label: "Decommissioned",
    description: "Asset is retired from service and awaiting physical removal.",
    badge_color: "serious",
    display_order: 5,
    allowed_transitions: ["removed"],
  },
  {
    status: "removed",
    label: "Removed",
    description: "Asset has been physically removed from the facility.",
    badge_color: "danger",
    display_order: 6,
    allowed_transitions: [],
  },
];

export const DEFAULT_LIFECYCLE_SETTINGS: LifecycleSettingsResponse = {
  states: DEFAULT_LIFECYCLE_STATES,
  mandatory_allowed_transitions: {
    planned: ["installed", "removed"],
    installed: ["active", "maintenance", "decommissioned"],
    active: ["maintenance", "decommissioned"],
    maintenance: ["active", "decommissioned"],
    decommissioned: ["removed"],
    removed: [],
  },
  legacy_states: ["reserved"],
  badge_colors: ["neutral", "info", "ok", "caution", "serious", "danger"],
};

export const LIFECYCLE_SETTINGS_QUERY_KEY = ["lifecycle-settings"] as const;

export function useLifecycleSettings() {
  const query = useQuery<LifecycleSettingsResponse>({
    queryKey: LIFECYCLE_SETTINGS_QUERY_KEY,
    queryFn: async () => {
      try {
        const result = await getLifecycleSettings();
        if (result && Array.isArray(result.states) && result.states.length > 0) {
          return result;
        }
      } catch {
        // Fall back to default lifecycle settings when offline or in unit tests that do not mock /settings/lifecycle.
      }
      return DEFAULT_LIFECYCLE_SETTINGS;
    },
    staleTime: 30_000,
  });

  const settings = query.data ?? DEFAULT_LIFECYCLE_SETTINGS;

  const sortedStates = useMemo(
    () => [...settings.states].sort((a, b) => a.display_order - b.display_order),
    [settings.states],
  );

  const byStatus = useMemo(() => {
    const map = new Map<string, LifecycleStateConfig>();
    for (const item of sortedStates) {
      map.set(item.status, item);
    }
    return map;
  }, [sortedStates]);

  const permittedTargets = (fromStatus: string): LifecycleStateConfig[] => {
    if (fromStatus === "reserved") return [];
    const current = byStatus.get(fromStatus);
    if (!current) return [];
    const allowedSet = new Set(
      current.allowed_transitions.filter((target) => target !== "reserved"),
    );
    return sortedStates.filter((candidate) => allowedSet.has(candidate.status));
  };

  return {
    query,
    settings,
    states: sortedStates,
    stateByStatus: (status: string) => byStatus.get(status),
    permittedTargets,
  };
}
