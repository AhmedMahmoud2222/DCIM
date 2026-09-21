import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { EmptyState, PageHeader, SectionTitle } from "@/components/ui/ProductUi";
import { apiFetch } from "@/lib/apiClient";
import { Page, Room, Site } from "@/types";

/** Navigator over authoritative locations, not a second inventory model. */
export function InfrastructurePage() {
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => apiFetch<Page<Site>>("/sites?limit=200") });
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: () => apiFetch<Page<Room>>("/rooms?limit=200") });
  const loading = sites.isLoading || rooms.isLoading;
  const error = sites.error ?? rooms.error;
  return <div className="page"><PageHeader eyebrow="Infrastructure" title="Sites, rooms, and racks" description="Navigate the physical hierarchy from a site to its operational room, rack, and equipment context." />
    {loading && <div className="surface-muted p-5 text-sm text-slate-400">Loading infrastructure hierarchy…</div>}{error && <div className="rounded-xl border border-rose-500/30 bg-rose-500/10 p-4 text-sm text-rose-200">{(error as Error).message}</div>}
    {!loading && !error && <><section><SectionTitle title="Sites" detail="Start with the data center or operational location." /><div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">{sites.data?.items.map((site) => <Link key={site.id} to={`/sites/${site.id}`} className="surface group p-5 transition hover:border-indigo-400/50 hover:bg-slate-800/70"><p className="text-base font-semibold text-slate-100">{site.name}</p><p className="mt-1 text-xs text-slate-500">{site.code} · {site.timezone}</p><p className="mt-5 text-sm text-indigo-300 transition group-hover:text-indigo-200">Open site hierarchy ›</p></Link>)}{sites.data?.items.length === 0 && <EmptyState title="No sites configured" detail="Create a site through the supported infrastructure workflow to begin modeling a location." />}</div></section>
    <section className="border-t border-slate-800 pt-6"><SectionTitle title="Rooms" detail="Open a room to view its floor plan and placed racks." /><div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-3">{rooms.data?.items.map((room) => <Link key={room.id} to={`/floor-plans/room/${room.id}`} className="surface group p-5 transition hover:border-indigo-400/50 hover:bg-slate-800/70"><p className="text-base font-semibold text-slate-100">{room.name}</p><p className="mt-1 text-xs text-slate-500">{room.code} · {room.room_type.replace(/_/g, " ")}</p><p className="mt-5 text-sm text-indigo-300 transition group-hover:text-indigo-200">Open spatial view ›</p></Link>)}{rooms.data?.items.length === 0 && <EmptyState title="No rooms configured" detail="Rooms appear here as they are added to the authoritative location hierarchy." />}</div></section></>}
  </div>;
}
