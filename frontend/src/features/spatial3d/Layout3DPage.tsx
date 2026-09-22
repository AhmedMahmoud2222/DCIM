import { useQuery } from "@tanstack/react-query";
import { type CSSProperties, type PointerEvent, type WheelEvent, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { PageHeader } from "@/components/ui/ProductUi";
import { getRoomSpatialView } from "@/features/floor-plans/api";
import { listRooms } from "@/features/racks/api";
import type { RackMountedEquipment, RoomRack } from "@/types";

const FALLBACK_ROOM_MM = 10_000;

function rackTransform(rack: RoomRack, index: number, width: number, height: number): CSSProperties {
  const x = rack.x_mm ?? 900 + (index % 4) * 1800;
  const y = rack.y_mm ?? 900 + Math.floor(index / 4) * 2200;
  return { left: `${(x / width) * 100}%`, top: `${(y / height) * 100}%`, transform: `translate(-50%, -50%) rotateZ(${rack.rotation_deg ?? 0}deg)` };
}

function RackCuboid({ rack, index, roomWidth, roomHeight, selected, onSelect, equipment }: { rack: RoomRack; index: number; roomWidth: number; roomHeight: number; selected: boolean; onSelect: () => void; equipment: RackMountedEquipment[] }) {
  const equipmentItems = equipment.filter((item) => item.rack_id === rack.id);
  return <div className={`layout3d-rack ${selected ? "layout3d-rack-selected" : ""}`} style={rackTransform(rack, index, roomWidth, roomHeight)}>
    <div className="layout3d-rack-face layout3d-rack-front" role="button" tabIndex={0} onClick={onSelect} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelect(); } }} aria-label={`Select rack ${rack.name}`}><b>{rack.name}</b><small>{equipmentItems.length ? `${equipmentItems.length} placed asset${equipmentItems.length === 1 ? "" : "s"}` : "No placed equipment"}</small>{equipmentItems.slice(0, 6).map((item, itemIndex) => <Link key={item.id} to={`/equipment/${item.id}`} onClick={(event) => event.stopPropagation()} className="layout3d-equipment" style={{ top: `${18 + itemIndex * 11}%` }} title={`Open ${item.hostname ?? item.asset_tag}`}>{item.hostname ?? item.asset_tag}</Link>)}</div>
    <span className="layout3d-rack-face layout3d-rack-back" /><span className="layout3d-rack-face layout3d-rack-left" /><span className="layout3d-rack-face layout3d-rack-right" /><span className="layout3d-rack-face layout3d-rack-top" /><span className="layout3d-rack-face layout3d-rack-bottom" />
  </div>;
}

export function Layout3DPage() {
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: listRooms });
  const [roomId, setRoomId] = useState("");
  const activeRoom = roomId || rooms.data?.items[0]?.id || "";
  const view = useQuery({ queryKey: ["spatial", activeRoom, "3d"], queryFn: () => getRoomSpatialView(activeRoom), enabled: !!activeRoom });
  const [selectedRack, setSelectedRack] = useState<RoomRack | null>(null);
  const [yaw, setYaw] = useState(-28); const [pitch, setPitch] = useState(56); const [zoom, setZoom] = useState(0.82); const [pan, setPan] = useState({ x: 0, y: 0 });
  const [drag, setDrag] = useState<{ mode: "orbit" | "pan"; x: number; y: number } | null>(null);
  const roomWidth = view.data?.room_width_mm ?? FALLBACK_ROOM_MM; const roomHeight = view.data?.room_height_mm ?? FALLBACK_ROOM_MM;
  const unplaced = useMemo(() => view.data?.racks.filter((rack) => rack.x_mm === null || rack.y_mm === null) ?? [], [view.data]);
  const reset = () => { setYaw(-28); setPitch(56); setZoom(0.82); setPan({ x: 0, y: 0 }); };
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => { event.currentTarget.setPointerCapture(event.pointerId); setDrag({ mode: event.shiftKey ? "pan" : "orbit", x: event.clientX, y: event.clientY }); };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => { if (!drag) return; const dx = event.clientX - drag.x; const dy = event.clientY - drag.y; if (drag.mode === "pan") setPan((current) => ({ x: current.x + dx, y: current.y + dy })); else { setYaw((value) => Math.max(-85, Math.min(85, value + dx * 0.35))); setPitch((value) => Math.max(28, Math.min(78, value - dy * 0.25))); } setDrag({ ...drag, x: event.clientX, y: event.clientY }); };
  const onWheel = (event: WheelEvent<HTMLDivElement>) => { event.preventDefault(); setZoom((value) => Math.max(0.45, Math.min(1.45, value - event.deltaY * 0.001))); };
  const equipment = view.data?.rack_equipment ?? [];

  return <div className="page"><PageHeader eyebrow="Infrastructure" title="3D layout" description="Interactive room scene projected from authoritative room, rack, and equipment placements." actions={<div className="flex gap-2"><Link to={activeRoom ? `/floor-plans/room/${activeRoom}` : "/floor-plans"} className="action-secondary">2D floor plan</Link><button className="action-secondary" onClick={reset}>Reset / fit</button></div>} />
    <div className="mb-3 flex flex-col gap-2 sm:flex-row sm:items-end"><label className="text-sm"><span className="mb-1 block text-slate-400">Room</span><select className="field min-w-64" value={activeRoom} onChange={(event) => { setRoomId(event.target.value); setSelectedRack(null); reset(); }}>{rooms.data?.items.map((room) => <option key={room.id} value={room.id}>{room.name}</option>)}</select></label><div className="text-xs text-slate-500">Drag to orbit · Shift + drag to pan · scroll to zoom · select a rack for context.</div></div>
    {view.isLoading ? <div className="surface p-8 text-sm text-slate-500">Loading authoritative spatial data…</div> : view.isError ? <div className="surface p-8 text-sm text-rose-300">Unable to load this room’s spatial data.</div> : <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_320px]"><section className="layout3d-viewport surface" onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={() => setDrag(null)} onPointerCancel={() => setDrag(null)} onWheel={onWheel} aria-label={`Interactive 3D layout of ${view.data?.room_name ?? "room"}`}>
      <div className="layout3d-world" style={{ transform: `translate3d(${pan.x}px, ${pan.y}px, 0) rotateX(${pitch}deg) rotateZ(${yaw}deg) scale(${zoom})` }}><div className="layout3d-floor"><span className="layout3d-floor-label">{view.data?.room_name}</span>{view.data?.racks.map((rack, index) => <RackCuboid key={rack.id} rack={rack} index={index} roomWidth={roomWidth} roomHeight={roomHeight} selected={selectedRack?.id === rack.id} onSelect={() => setSelectedRack(rack)} equipment={equipment} />)}</div></div>
      <div className="layout3d-controls" aria-label="3D view controls"><label>Orbit <input type="range" min="-85" max="85" value={yaw} onChange={(event) => setYaw(+event.target.value)} /></label><label>Tilt <input type="range" min="28" max="78" value={pitch} onChange={(event) => setPitch(+event.target.value)} /></label><label>Zoom <input type="range" min="0.45" max="1.45" step="0.05" value={zoom} onChange={(event) => setZoom(+event.target.value)} /></label></div><p className="layout3d-provenance">No placement is inferred: racks lacking room coordinates remain in the inventory list.</p>
    </section><aside className="surface p-4"><h2 className="font-semibold">Rack context</h2>{selectedRack ? <><h3 className="mt-3 text-lg font-semibold">{selectedRack.name}</h3><p className="text-xs text-slate-500">{selectedRack.asset_tag}</p><dl className="mt-4 space-y-2 text-sm"><div><dt className="text-slate-500">Room position</dt><dd>{selectedRack.x_mm ?? "Unavailable"}, {selectedRack.y_mm ?? "Unavailable"} mm</dd></div><div><dt className="text-slate-500">Rotation</dt><dd>{selectedRack.rotation_deg ?? "Unavailable"}°</dd></div><div><dt className="text-slate-500">Placed equipment</dt><dd>{equipment.filter((item) => item.rack_id === selectedRack.id).length || "None recorded"}</dd></div></dl><Link to={`/racks/${selectedRack.id}`} className="action-primary mt-5 inline-block">Open rack elevation</Link></> : <p className="mt-3 text-sm text-slate-500">Select a rack in the scene.</p>}<div className="mt-6 border-t border-slate-800 pt-3"><h3 className="text-xs font-semibold uppercase text-slate-500">Unplaced inventory</h3>{unplaced.length ? <ul className="mt-2 space-y-1 text-sm">{unplaced.map((rack) => <li key={rack.id}><Link className="text-indigo-300 hover:text-indigo-200" to={`/racks/${rack.id}`}>{rack.name}</Link> <span className="text-slate-500">— coordinates unavailable</span></li>)}</ul> : <p className="mt-2 text-sm text-slate-500">Every room rack has coordinates.</p>}<p className="mt-4 text-xs text-slate-600">Layers: room · racks · placed equipment. Power, network, cooling, and environment overlays are intentionally not represented until those authoritative spatial relationships exist.</p></div></aside></div>}</div>;
}
