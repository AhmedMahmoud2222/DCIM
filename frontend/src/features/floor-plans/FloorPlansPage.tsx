import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { listRooms } from "@/features/racks/api";

export function FloorPlansPage() {
  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms });

  return (
    <div>
      <h1 className="mb-4 text-lg font-semibold">Floor Plans</h1>
      <p className="mb-6 max-w-2xl text-sm text-slate-400">
        Floor plans are per-room, with a revision history — activating a new revision automatically supersedes the
        room's previous one. Select a room to view its 2D layout, manage floor plan revisions, and review imported
        geometry.
      </p>

      {roomsQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
      {roomsQuery.error && <p className="text-sm text-red-400">{(roomsQuery.error as Error).message}</p>}

      {roomsQuery.data && (
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-800 text-left text-slate-400">
              <th className="pb-2">Room</th>
              <th className="pb-2">Code</th>
              <th className="pb-2">Type</th>
            </tr>
          </thead>
          <tbody>
            {roomsQuery.data.items.map((room) => (
              <tr key={room.id} className="border-b border-slate-900 hover:bg-slate-900/50">
                <td className="py-2">
                  <Link to={`/floor-plans/room/${room.id}`} className="font-medium text-blue-400 hover:underline">
                    {room.name}
                  </Link>
                </td>
                <td className="py-2 font-mono text-slate-300">{room.code}</td>
                <td className="py-2 text-slate-400">{room.room_type}</td>
              </tr>
            ))}
            {roomsQuery.data.items.length === 0 && (
              <tr>
                <td colSpan={3} className="py-6 text-center text-slate-500">
                  No rooms yet — create one under Locations first.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
