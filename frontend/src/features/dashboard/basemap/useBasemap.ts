import { useQuery } from "@tanstack/react-query";

import { BasemapResult, loadBasemap } from "@/features/dashboard/basemap/loadBasemap";

export type BasemapState =
  | { status: "loading" }
  | { status: "ready"; result: Extract<BasemapResult, { basemap: object }> }
  | { status: "unavailable"; note?: string };

/** The active basemap, loaded once per session. It never blocks the dashboard: callers draw the graticule and
 * the markers while it is loading, and keep doing so if it is unavailable. */
export function useBasemap(enabled = true): BasemapState {
  const query = useQuery({
    queryKey: ["basemap"],
    queryFn: () => loadBasemap(),
    enabled,
    staleTime: Infinity,
    gcTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchOnMount: false,
  });
  if (query.isPending) return { status: "loading" };
  const result = query.data;
  if (!result || result.basemap === null) return { status: "unavailable", note: result?.note };
  return { status: "ready", result };
}
