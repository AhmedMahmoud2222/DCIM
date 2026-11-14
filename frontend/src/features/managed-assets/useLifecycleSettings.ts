import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { getLifecycleSettings } from "@/features/managed-assets/api";
import {
  DEFAULT_LIFECYCLE_SETTINGS,
  DEFAULT_LIFECYCLE_STATES,
} from "@/features/managed-assets/lifecycleDefaults";
import { LifecycleSettingsResponse, LifecycleStateConfig } from "@/types";

export { DEFAULT_LIFECYCLE_SETTINGS, DEFAULT_LIFECYCLE_STATES };

export const LIFECYCLE_SETTINGS_QUERY_KEY = ["lifecycle-settings"] as const;

// Distinct object identity marks "settings failed to load": defaults are used for display only and are
// never treated as authoritative for transition controls.
const FALLBACK_LIFECYCLE_SETTINGS: LifecycleSettingsResponse = { ...DEFAULT_LIFECYCLE_SETTINGS };

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
      return FALLBACK_LIFECYCLE_SETTINGS;
    },
    staleTime: 30_000,
  });

  const settings = query.data ?? DEFAULT_LIFECYCLE_SETTINGS;
  const isAuthoritative = query.data !== undefined && query.data !== FALLBACK_LIFECYCLE_SETTINGS;

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
    if (!isAuthoritative) return [];
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
    isAuthoritative,
    states: sortedStates,
    stateByStatus: (status: string) => byStatus.get(status),
    permittedTargets,
  };
}
