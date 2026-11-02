import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";

import { DataTable, EmptyRow, RowCount, TD, TH, TR } from "@/components/data/DataTable";
import { QueryStatus } from "@/components/data/QueryStatus";
import { PageHeader } from "@/components/layout/PageHeader";
import { listRooms } from "@/features/racks/api";

export function FloorPlansPage() {
  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms });
  const data = roomsQuery.data;

  return (
    <div>
      <PageHeader
        title="Floor Plans"
        description="Floor plans are per-room, with a revision history — activating a new revision automatically supersedes the room's previous one. Select a room to view its 2D layout, manage floor plan revisions, and review imported geometry."
      />

      <QueryStatus query={roomsQuery} what="rooms" permission="location:read" columns={3} />

      {data && (
        <>
          <RowCount shown={data.items.length} total={data.total} noun="rooms" />
          <DataTable label="Rooms" minWidthClass="min-w-[28rem]">
            <thead>
              <tr className="border-b border-line-subtle bg-surface-panel">
                <th scope="col" className={TH}>Room</th>
                <th scope="col" className={TH}>Code</th>
                <th scope="col" className={TH}>Type</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((room) => (
                <tr key={room.id} className={TR}>
                  <td className={TD}>
                    <Link to={`/floor-plans/room/${room.id}`} className="font-medium text-link hover:underline">
                      {room.name}
                    </Link>
                  </td>
                  <td className={`${TD} font-mono text-fg-secondary`}>{room.code}</td>
                  <td className={`${TD} text-fg-secondary`}>{room.room_type}</td>
                </tr>
              ))}
              {data.items.length === 0 && <EmptyRow columns={3}>No rooms yet — create one under Locations first.</EmptyRow>}
            </tbody>
          </DataTable>
        </>
      )}
    </div>
  );
}
