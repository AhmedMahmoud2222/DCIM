import { useQuery } from "@tanstack/react-query";
import { type CSSProperties, type KeyboardEvent, type PointerEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { getRoomSpatialView } from "@/features/floor-plans/api";
import { listRooms } from "@/features/racks/api";
import { occupiesFace, racksMissingCoordinates, uRangeToPlacement } from "./layout3dMath";
import type { RoomRack, RoomRackEquipment } from "@/types";

const FALLBACK_ROOM_MM = 10_000;
const PAN_STEP_PX = 40;
const ZOOM_STEP = 0.05;
const ZOOM_MIN = 0.45;
const ZOOM_MAX = 1.45;

function rackTransform(rack: RoomRack, index: number, width: number, height: number): CSSProperties {
  const x = rack.x_mm ?? 900 + (index % 4) * 1800;
  const y = rack.y_mm ?? 900 + Math.floor(index / 4) * 2200;
  return { left: `${(x / width) * 100}%`, top: `${(y / height) * 100}%`, transform: `translate(-50%, -50%) rotateZ(${rack.rotation_deg ?? 0}deg)` };
}

/** One rack face's equipment, positioned from authoritative u_start/u_end — never by
 * array index, never capped — with front/rear respected and out-of-range placement
 * data clamped to a visibly-flagged safe layout instead of breaking the scene. */
function RackFace({ equipment, heightU, face }: { equipment: RoomRackEquipment[]; heightU: number; face: "front" | "rear" }) {
  const items = equipment.filter((item) => occupiesFace(item.side, face));
  return (
    <>
      {items.map((item) => {
        const { topPct, heightPct, irregular } = uRangeToPlacement(item.u_start, item.u_end, heightU);
        const label = item.hostname ?? item.asset_tag;
        return (
          <Link
            key={`${item.id}-${face}`}
            to={`/equipment/${item.id}`}
            onClick={(event) => event.stopPropagation()}
            onPointerDown={(event) => event.stopPropagation()}
            className={`layout3d-equipment${irregular ? " layout3d-equipment-irregular" : ""}`}
            style={{ top: `${topPct}%`, height: `${Math.max(heightPct, 4)}%` }}
            title={`Open ${label} · U${item.u_start}–U${item.u_end - 1} · ${item.side}${irregular ? " · irregular placement data, shown clamped" : ""}`}
          >
            {label}
          </Link>
        );
      })}
    </>
  );
}

function RackCuboid({
  rack,
  index,
  roomWidth,
  roomHeight,
  selected,
  onSelect,
  equipment,
}: {
  rack: RoomRack;
  index: number;
  roomWidth: number;
  roomHeight: number;
  selected: boolean;
  onSelect: () => void;
  equipment: RoomRackEquipment[];
}) {
  const equipmentItems = equipment.filter((item) => item.rack_id === rack.id);
  const usingFallbackPosition = rack.x_mm === null || rack.y_mm === null;
  return (
    <div className={`layout3d-rack ${selected ? "layout3d-rack-selected" : ""}`} style={rackTransform(rack, index, roomWidth, roomHeight)}>
      <div
        className="layout3d-rack-face layout3d-rack-front"
        role="button"
        tabIndex={0}
        onClick={onSelect}
        onPointerDown={(event) => event.stopPropagation()}
        onKeyDown={(event) => {
          if (event.target !== event.currentTarget) return;
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            onSelect();
          }
        }}
        aria-label={`Select rack ${rack.name}, ${rack.height_u}U capacity, ${equipmentItems.length} placed asset${equipmentItems.length === 1 ? "" : "s"}${usingFallbackPosition ? ", position estimated — no authoritative coordinates recorded" : ""}`}
      >
        <b>{rack.name}</b>
        <small>{equipmentItems.length ? `${equipmentItems.length} placed asset${equipmentItems.length === 1 ? "" : "s"}` : "No placed equipment"}</small>
        {usingFallbackPosition && (
          <i
            className="layout3d-fallback-badge"
            aria-hidden="true"
            title="No authoritative x/y position recorded — shown at a standardized fallback grid position, not its real location"
          >
            ≈
          </i>
        )}
        <RackFace equipment={equipmentItems} heightU={rack.height_u} face="front" />
      </div>
      <span className="layout3d-rack-face layout3d-rack-back">
        <RackFace equipment={equipmentItems} heightU={rack.height_u} face="rear" />
      </span>
      <span className="layout3d-rack-face layout3d-rack-left" />
      <span className="layout3d-rack-face layout3d-rack-right" />
      <span className="layout3d-rack-face layout3d-rack-top" />
      <span className="layout3d-rack-face layout3d-rack-bottom" />
    </div>
  );
}

export function Layout3DPage() {
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: listRooms, staleTime: 30_000 });
  const [roomId, setRoomId] = useState("");
  const activeRoom = roomId || rooms.data?.items[0]?.id || "";
  const view = useQuery({
    queryKey: ["spatial", activeRoom, "3d"],
    queryFn: () => getRoomSpatialView(activeRoom),
    enabled: !!activeRoom,
    staleTime: 15_000,
  });
  const [selectedRack, setSelectedRack] = useState<RoomRack | null>(null);
  const [yaw, setYaw] = useState(-28);
  const [pitch, setPitch] = useState(56);
  const [zoom, setZoom] = useState(0.82);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [drag, setDrag] = useState<{ mode: "orbit" | "pan"; x: number; y: number } | null>(null);
  const roomWidth = view.data?.room_width_mm ?? FALLBACK_ROOM_MM;
  const roomHeight = view.data?.room_height_mm ?? FALLBACK_ROOM_MM;
  const coordinateIncomplete = useMemo(() => racksMissingCoordinates(view.data?.racks ?? []), [view.data]);
  const reset = () => {
    setYaw(-28);
    setPitch(56);
    setZoom(0.82);
    setPan({ x: 0, y: 0 });
  };

  // Cleanup on navigation/unmount: never leave a drag mode latched if the route changes
  // mid-drag (the browser releases pointer capture itself when the element unmounts).
  useEffect(() => () => setDrag(null), [activeRoom]);

  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setDrag(null);
  };
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    setDrag({ mode: event.shiftKey ? "pan" : "orbit", x: event.clientX, y: event.clientY });
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (drag.mode === "pan") {
      setPan((current) => ({ x: current.x + dx, y: current.y + dy }));
    } else {
      setYaw((value) => Math.max(-85, Math.min(85, value + dx * 0.35)));
      setPitch((value) => Math.max(28, Math.min(78, value - dy * 0.25)));
    }
    setDrag({ ...drag, x: event.clientX, y: event.clientY });
  };
  // React delegates onWheel as a passive listener, so calling preventDefault() inside it
  // is silently dropped by the browser and the page scrolls underneath the scene while
  // the camera also zooms. A native listener attached directly to the viewport with
  // { passive: false } is the only way to actually stop that scroll.
  const viewportRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    const node = viewportRef.current;
    if (!node) return;
    const handleWheel = (event: globalThis.WheelEvent) => {
      event.preventDefault();
      setZoom((value) => Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, value - event.deltaY * 0.001)));
    };
    node.addEventListener("wheel", handleWheel, { passive: false });
    return () => node.removeEventListener("wheel", handleWheel);
  }, [view.isLoading, view.isError]);
  // Direct keyboard camera control on the scene itself (arrow keys pan, +/- zoom, 0
  // resets) — a keyboard-only user is never limited to only what the range sliders
  // cover. Scoped to the viewport's own focus, matching the same "don't hijack a
  // control's own interaction" rule pointer events already follow.
  const onViewportKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.target !== event.currentTarget) return;
    switch (event.key) {
      case "ArrowLeft":
        event.preventDefault();
        setPan((current) => ({ ...current, x: current.x + PAN_STEP_PX }));
        break;
      case "ArrowRight":
        event.preventDefault();
        setPan((current) => ({ ...current, x: current.x - PAN_STEP_PX }));
        break;
      case "ArrowUp":
        event.preventDefault();
        setPan((current) => ({ ...current, y: current.y + PAN_STEP_PX }));
        break;
      case "ArrowDown":
        event.preventDefault();
        setPan((current) => ({ ...current, y: current.y - PAN_STEP_PX }));
        break;
      case "+":
      case "=":
        event.preventDefault();
        setZoom((value) => Math.min(ZOOM_MAX, value + ZOOM_STEP));
        break;
      case "-":
        event.preventDefault();
        setZoom((value) => Math.max(ZOOM_MIN, value - ZOOM_STEP));
        break;
      case "0":
        event.preventDefault();
        reset();
        break;
      default:
        break;
    }
  };
  const rackEquipment = view.data?.rack_equipment ?? [];

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">3D Layout</h1>
      <p className="mb-4 text-sm text-slate-500">
        Interactive room scene projected from authoritative room, rack, and equipment placements — a schematic CSS projection for
        orientation, not a dimensionally accurate digital twin.
      </p>

      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <label className="block text-sm">
          <span className="mb-1 block text-slate-400">Room</span>
          <select
            className="min-w-64 rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
            value={activeRoom}
            onChange={(event) => {
              setRoomId(event.target.value);
              setSelectedRack(null);
              reset();
            }}
          >
            {rooms.data?.items.map((room) => (
              <option key={room.id} value={room.id}>
                {room.name}
              </option>
            ))}
          </select>
        </label>
        <div className="flex items-center gap-3">
          <span className="text-xs text-slate-500">
            Drag to orbit · Shift + drag to pan · scroll to zoom · focus the scene and use arrow keys to pan, +/− to zoom, 0 to reset.
          </span>
          <Link
            to={activeRoom ? `/floor-plans/room/${activeRoom}` : "/floor-plans"}
            className="whitespace-nowrap rounded-sm border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-800"
          >
            2D floor plan
          </Link>
          <button
            type="button"
            onClick={reset}
            className="whitespace-nowrap rounded-sm border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-800"
          >
            Reset / fit
          </button>
        </div>
      </div>

      {view.isLoading ? (
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-sm text-slate-500">Loading authoritative spatial data…</div>
      ) : view.isError ? (
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-sm text-red-400">Unable to load this room's spatial data.</div>
      ) : (
        <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
          <section
            ref={viewportRef}
            className="layout3d-viewport rounded-sm border border-slate-800"
            tabIndex={0}
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={endDrag}
            onPointerCancel={endDrag}
            onKeyDown={onViewportKeyDown}
            aria-label={`Interactive 3D layout of ${view.data?.room_name ?? "room"}. Use arrow keys to pan, plus and minus to zoom, zero to reset.`}
          >
            <div className="layout3d-world" style={{ transform: `translate3d(${pan.x}px, ${pan.y}px, 0) rotateX(${pitch}deg) rotateZ(${yaw}deg) scale(${zoom})` }}>
              <div className="layout3d-floor">
                <span className="layout3d-floor-label">{view.data?.room_name}</span>
                {view.data?.racks.map((rack, index) => (
                  <RackCuboid
                    key={rack.id}
                    rack={rack}
                    index={index}
                    roomWidth={roomWidth}
                    roomHeight={roomHeight}
                    selected={selectedRack?.id === rack.id}
                    onSelect={() => setSelectedRack(rack)}
                    equipment={rackEquipment}
                  />
                ))}
              </div>
            </div>
            <div className="layout3d-controls" aria-label="3D view controls">
              <label>
                Orbit <input type="range" min="-85" max="85" value={yaw} onChange={(event) => setYaw(+event.target.value)} />
              </label>
              <label>
                Tilt <input type="range" min="28" max="78" value={pitch} onChange={(event) => setPitch(+event.target.value)} />
              </label>
              <label>
                Zoom <input type="range" min={ZOOM_MIN} max={ZOOM_MAX} step={ZOOM_STEP} value={zoom} onChange={(event) => setZoom(+event.target.value)} />
              </label>
              <div className="layout3d-pan-pad" role="group" aria-label="Pan camera">
                <button type="button" aria-label="Pan up" onClick={() => setPan((current) => ({ ...current, y: current.y + PAN_STEP_PX }))}>
                  ▲
                </button>
                <button type="button" aria-label="Pan left" onClick={() => setPan((current) => ({ ...current, x: current.x + PAN_STEP_PX }))}>
                  ◀
                </button>
                <button type="button" aria-label="Pan right" onClick={() => setPan((current) => ({ ...current, x: current.x - PAN_STEP_PX }))}>
                  ▶
                </button>
                <button type="button" aria-label="Pan down" onClick={() => setPan((current) => ({ ...current, y: current.y - PAN_STEP_PX }))}>
                  ▼
                </button>
              </div>
            </div>
            <p className="layout3d-provenance">
              Schematic projection only — rack footprints are standardized, not to scale. A rack with no recorded x/y still renders
              (marked ≈) at a standardized fallback grid position.
            </p>
          </section>
          <aside className="rounded-sm border border-slate-800 bg-slate-900 p-4">
            <h2 className="font-semibold">Rack context</h2>
            {selectedRack ? (
              <>
                <h3 className="mt-3 text-lg font-semibold">{selectedRack.name}</h3>
                <p className="text-xs text-slate-500">{selectedRack.asset_tag}</p>
                <dl className="mt-4 space-y-2 text-sm">
                  <div>
                    <dt className="text-slate-500">Room position</dt>
                    <dd>
                      {selectedRack.x_mm ?? "Unavailable (estimated in scene)"}, {selectedRack.y_mm ?? "Unavailable (estimated in scene)"} mm
                    </dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Rotation</dt>
                    <dd>{selectedRack.rotation_deg ?? "Unavailable"}°</dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Capacity</dt>
                    <dd>{selectedRack.height_u}U</dd>
                  </div>
                  <div>
                    <dt className="text-slate-500">Placed equipment</dt>
                    <dd>{rackEquipment.filter((item) => item.rack_id === selectedRack.id).length || "None recorded"}</dd>
                  </div>
                </dl>
                <Link to={`/racks/${selectedRack.id}`} className="mt-5 inline-block rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500">
                  Open rack elevation
                </Link>
              </>
            ) : (
              <p className="mt-3 text-sm text-slate-500">Select a rack in the scene.</p>
            )}
            {coordinateIncomplete.length > 0 && (
              <p className="mt-4 border-t border-slate-800 pt-3 text-xs text-yellow-400">
                {coordinateIncomplete.length} rack{coordinateIncomplete.length === 1 ? "" : "s"} placed in this room{" "}
                {coordinateIncomplete.length === 1 ? "has" : "have"} no recorded floor position yet (marked ≈ above), which is
                different from being unplaced.
              </p>
            )}
            <p className="mt-4 text-xs text-slate-600">
              Layers: room · racks · placed equipment. Power, network, cooling, and environment overlays are intentionally not
              represented until those authoritative spatial relationships exist. Racks with no room placement anywhere are not
              listed here — check the <Link to="/racks" className="text-blue-400 hover:underline">Racks</Link> page.
            </p>
          </aside>
        </div>
      )}
    </div>
  );
}
