import { useQuery } from "@tanstack/react-query";
import { Link, useParams } from "react-router-dom";

import { apiFetch } from "@/lib/apiClient";
import { Page, Room, Site } from "@/types";

export function SiteDetailPage() {
  const { siteId } = useParams<{ siteId: string }>();
  const site = useQuery({ queryKey: ["site", siteId], queryFn: () => apiFetch<Site>(`/sites/${siteId}`), enabled: !!siteId });
  const rooms = useQuery({ queryKey: ["rooms", "site", siteId], queryFn: () => apiFetch<Page<Room>>(`/rooms?site_id=${siteId}&limit=200`), enabled: !!siteId });
  if (site.isLoading) return <p className="text-sm text-slate-400">Loading site…</p>;
  if (site.isError) return <p className="text-sm text-red-400">Unable to load this site.</p>;
  return <div><Link to="/infrastructure" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">← Infrastructure</Link>
    <h1 className="text-lg font-semibold">{site.data?.name}</h1><p className="mb-6 text-sm text-slate-400">{site.data?.code} · {site.data?.timezone}</p>
    <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Rooms</h2>
    <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">{rooms.data?.items.map((room) => <Link key={room.id} to={`/floor-plans/room/${room.id}`} className="rounded border border-slate-800 bg-slate-900 p-4 hover:border-blue-700"><p className="font-medium">{room.name}</p><p className="text-xs text-slate-500">{room.code} · {room.room_type}</p><p className="mt-3 text-sm text-blue-400">Open floor plan and racks →</p></Link>)}{rooms.data?.items.length === 0 && <p className="text-sm text-slate-500">No rooms have been configured for this site.</p>}</div>
  </div>;
}
