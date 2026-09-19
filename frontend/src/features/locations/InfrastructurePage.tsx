import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { apiFetch } from "@/lib/apiClient";
import { Page, Room, Site } from "@/types";

/** Read-only operational navigator. It consumes the authoritative location APIs;
 * this is not a second inventory model. */
export function InfrastructurePage() {
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => apiFetch<Page<Site>>("/sites?limit=200") });
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: () => apiFetch<Page<Room>>("/rooms?limit=200") });
  return <div>
    <h1 className="mb-1 text-lg font-semibold">Infrastructure</h1>
    <p className="mb-6 text-sm text-slate-400">Authoritative location and inventory navigation. Select a room for its floor plan and racks.</p>
    {sites.isLoading || rooms.isLoading ? <p className="text-sm text-slate-400">Loading infrastructure…</p> : <>
      <section className="mb-6"><h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Sites</h2><div className="grid gap-3 md:grid-cols-2">{sites.data?.items.map((site) => <Link key={site.id} to={`/sites/${site.id}`} className="rounded border border-slate-800 bg-slate-900 p-4 hover:border-blue-700"><p className="font-medium">{site.name}</p><p className="text-xs text-slate-500">{site.code} · {site.timezone}</p><p className="mt-3 text-sm text-blue-400">Open site →</p></Link>)}{sites.data?.items.length === 0 && <p className="text-sm text-slate-500">No sites yet.</p>}</div></section>
      <section><h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Rooms</h2><div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">{rooms.data?.items.map((room) => <Link key={room.id} to={`/floor-plans/room/${room.id}`} className="rounded border border-slate-800 bg-slate-900 p-4 hover:border-blue-700"><p className="font-medium">{room.name}</p><p className="text-xs text-slate-500">{room.code} · {room.room_type}</p><p className="mt-3 text-sm text-blue-400">Floor plan / racks →</p></Link>)}{rooms.data?.items.length === 0 && <p className="text-sm text-slate-500">No rooms yet.</p>}</div></section>
    </>}
  </div>;
}
