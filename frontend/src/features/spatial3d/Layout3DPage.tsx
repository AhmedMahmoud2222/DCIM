import { useQuery } from "@tanstack/react-query";
import { type CSSProperties, type KeyboardEvent, type PointerEvent, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";

import { getCoolingLayout, getHeatMap } from "@/features/cooling/api";
import { ThermalLegend } from "@/features/cooling/ThermalLegend";
import { MAP_STATE_META, SENSOR_STATE_META, describeSensor, explainReason, metricLabel } from "@/features/cooling/thermalMath";
import { getRoomOverlays, getRoomSpatialView } from "@/features/floor-plans/api";
import { OVERLAY_KINDS, STATE_COLORS, STATE_LABEL, STATE_SYMBOL, describeOverlay, overlayIndex } from "@/features/floor-plans/overlayStyle";
import { explainIncomplete, formatLength, niceGridInterval } from "@/features/floor-plans/spatialMath";
import { listRooms } from "@/features/racks/api";
import { type SceneBox, buildScene, occupiesFace, uRangeToPlacement } from "./layout3dMath";
import { ThermalFloor3D } from "./ThermalFloor3D";
import type { OverlayItem, OverlayKind, RoomRackEquipment, ThermalMetric } from "@/types";

const PAN_STEP_PX = 40;
const ZOOM_STEP = 0.05;
const ZOOM_MIN = 0.35;
const ZOOM_MAX = 1.8;
const DEFAULT_RACK_COLOR = "#2563eb";
const DEFAULT_EQUIPMENT_COLOR = "#0d9488";

/** One rack face's equipment, positioned from authoritative u_start/u_end: never by array index, never capped. Front
 * and rear are respected, and out-of-range placement data is clamped to a visibly flagged layout. */
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

function boxStyle(box: SceneBox, color: string): CSSProperties {
  return {
    left: box.left,
    top: box.top,
    transform: `rotateZ(${box.rotationDeg}deg)`,
    ["--w" as string]: `${box.width}px`,
    ["--d" as string]: `${box.depth}px`,
    ["--h" as string]: `${box.height}px`,
    ["--c" as string]: color,
  };
}

function Cuboid({
  box,
  color,
  selected,
  onSelect,
  overlayItem,
  overlayKind,
  equipment = [],
}: {
  box: SceneBox;
  color: string;
  selected: boolean;
  onSelect: () => void;
  overlayItem?: OverlayItem;
  overlayKind: OverlayKind | "none";
  equipment?: RoomRackEquipment[];
}) {
  const stateText = overlayItem && overlayKind !== "none" ? `, ${describeOverlay(overlayKind, overlayItem)}` : "";
  const noun = box.kind === "rack" ? "rack" : "equipment";
  return (
    <div
      className={`layout3d-cuboid ${selected ? "layout3d-selected" : ""}`}
      style={boxStyle(box, color)}
      data-asset-id={box.id}
      data-asset-kind={box.kind}
      data-width-px={box.width.toFixed(2)}
      data-depth-px={box.depth.toFixed(2)}
      data-height-px={box.height.toFixed(2)}
      data-x-mm={box.mm.x}
      data-y-mm={box.mm.y}
      data-rotation-deg={box.rotationDeg}
      data-testid={`cuboid-${box.id}`}
    >
      <div
        className="layout3d-face layout3d-face-front"
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
        aria-pressed={selected}
        aria-label={`Select ${noun} ${box.label}${box.heightU ? `, ${box.heightU}U capacity, ${equipment.length} placed asset${equipment.length === 1 ? "" : "s"}` : ""}, ${formatLength(box.mm.width)} by ${formatLength(box.mm.depth)}${stateText}`}
      >
        <b>{box.label}</b>
        {box.heightU != null && <small>{equipment.length ? `${equipment.length} placed asset${equipment.length === 1 ? "" : "s"}` : "No placed equipment"}</small>}
        {overlayItem && overlayKind !== "none" && (
          <i className="layout3d-badge" aria-hidden="true" title={describeOverlay(overlayKind, overlayItem)}>
            {STATE_SYMBOL[overlayItem.state]}
          </i>
        )}
        {box.heightU != null && <RackFace equipment={equipment} heightU={box.heightU} face="front" />}
      </div>
      <span className="layout3d-face layout3d-face-back">{box.heightU != null && <RackFace equipment={equipment} heightU={box.heightU} face="rear" />}</span>
      <span className="layout3d-face layout3d-face-left" />
      <span className="layout3d-face layout3d-face-right" />
      <span className="layout3d-face layout3d-face-top" />
    </div>
  );
}

export function Layout3DPage() {
  const [searchParams] = useSearchParams();
  const rooms = useQuery({ queryKey: ["rooms"], queryFn: listRooms, staleTime: 30_000 });
  const [roomId, setRoomId] = useState(searchParams.get("room") ?? "");
  const activeRoom = roomId || rooms.data?.items[0]?.id || "";
  const view = useQuery({
    queryKey: ["spatial", activeRoom, "3d"],
    queryFn: () => getRoomSpatialView(activeRoom),
    enabled: !!activeRoom,
    // authoritative placement is what this view shows: never serve an old copy when the page is opened again
    staleTime: 0,
    refetchOnMount: "always",
  });
  const [overlay, setOverlay] = useState<OverlayKind | "none">("none");
  const overlays = useQuery({
    queryKey: ["overlays", activeRoom, overlay, "3d"],
    queryFn: () => getRoomOverlays(activeRoom, [overlay as OverlayKind]),
    enabled: !!activeRoom && overlay !== "none",
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: 30_000,
  });
  const states = useMemo(() => overlayIndex(overlays.data, overlay), [overlays.data, overlay]);
  const [thermal, setThermal] = useState<ThermalMetric | "none">("none");
  const thermalMap = useQuery({
    queryKey: ["cooling", activeRoom, "heat-map", thermal, "3d"],
    queryFn: () => getHeatMap(activeRoom, thermal as ThermalMetric),
    enabled: !!activeRoom && thermal !== "none",
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: 30_000,
    retry: false,
  });
  const thermalLayout = useQuery({
    queryKey: ["cooling", activeRoom, "layout", "3d"],
    queryFn: () => getCoolingLayout(activeRoom),
    enabled: !!activeRoom && thermal !== "none",
    staleTime: 0,
    refetchOnMount: "always",
    retry: false,
  });
  const scene = useMemo(() => (view.data ? buildScene(view.data) : null), [view.data]);
  const [selected, setSelected] = useState<{ kind: "rack" | "equipment"; id: string } | null>(null);
  const [yaw, setYaw] = useState(-28);
  const [pitch, setPitch] = useState(56);
  const [zoom, setZoom] = useState(0.82);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [drag, setDrag] = useState<{ mode: "orbit" | "pan"; x: number; y: number } | null>(null);
  const reset = () => {
    setYaw(-28);
    setPitch(56);
    setZoom(0.82);
    setPan({ x: 0, y: 0 });
  };

  // Cleanup on navigation/unmount: never leave a drag mode latched if the route changes mid-drag.
  useEffect(() => () => setDrag(null), [activeRoom]);

  const endDrag = (event: PointerEvent<HTMLElement>) => {
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    setDrag(null);
  };
  const onPointerDown = (event: PointerEvent<HTMLElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    setDrag({ mode: event.shiftKey ? "pan" : "orbit", x: event.clientX, y: event.clientY });
  };
  const onPointerMove = (event: PointerEvent<HTMLElement>) => {
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (drag.mode === "pan") {
      setPan((current) => ({ x: current.x + dx, y: current.y + dy }));
    } else {
      setYaw((value) => Math.max(-180, Math.min(180, value + dx * 0.35)));
      setPitch((value) => Math.max(10, Math.min(88, value - dy * 0.25)));
    }
    setDrag({ ...drag, x: event.clientX, y: event.clientY });
  };
  // React delegates onWheel as a passive listener, so preventDefault() inside it is dropped and the page scrolls
  // under the scene. A native { passive: false } listener on the viewport is the only way to stop that.
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
  }, [view.isLoading, view.isError, scene]);
  // Direct keyboard camera control on the scene itself (arrows pan, +/- zoom, 0 resets), scoped to the viewport's own
  // focus so a rack's own keyboard handling is never hijacked.
  const onViewportKeyDown = (event: KeyboardEvent<HTMLElement>) => {
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
  const selectedBox = scene ? [...scene.racks, ...scene.floorEquipment].find((b) => b.id === selected?.id) : undefined;
  const selectedOverlay = selected ? states.get(selected.id) : undefined;
  const gridPx = scene ? niceGridInterval(scene.scale * zoom, 56) * scene.scale : 100;
  const reasons = view.data?.incomplete_reasons ?? [];
  const calibration = view.data?.calibration;

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">3D digital twin</h1>
      <p className="mb-4 text-sm text-slate-500">
        Room, rack and equipment geometry at real scale, taken from authoritative placements and catalog dimensions. Anything whose position or
        dimensions are not recorded is listed below the scene instead of being drawn.
      </p>

      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-end sm:justify-between">
        <div className="flex flex-wrap items-end gap-3">
          <label className="block text-sm">
            <span className="mb-1 block text-slate-400">Room</span>
            <select
              className="min-w-64 rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
              value={activeRoom}
              onChange={(event) => {
                setRoomId(event.target.value);
                setSelected(null);
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
          <label className="block text-sm">
            <span className="mb-1 block text-slate-400">Thermal layer</span>
            <select
              aria-label="Thermal layer"
              className="rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
              value={thermal}
              onChange={(event) => setThermal(event.target.value as ThermalMetric | "none")}
            >
              <option value="none">none</option>
              <option value="temperature_c">Temperature heat map</option>
              <option value="humidity_percent">Humidity heat map</option>
            </select>
          </label>
          <label className="block text-sm">
            <span className="mb-1 block text-slate-400">Overlay</span>
            <select
              aria-label="Operational overlay"
              className="rounded-sm border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
              value={overlay}
              onChange={(event) => setOverlay(event.target.value as OverlayKind | "none")}
            >
              <option value="none">none</option>
              {OVERLAY_KINDS.map((kind) => (
                <option key={kind.id} value={kind.id}>
                  {kind.label}
                </option>
              ))}
            </select>
          </label>
        </div>
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

      {rooms.isLoading || (!!activeRoom && view.isPending) ? (
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-sm text-slate-500" role="status">
          Loading authoritative spatial data…
        </div>
      ) : !activeRoom ? (
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-sm text-slate-500" role="status">
          There are no rooms to show yet.
        </div>
      ) : view.isError ? (
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-8 text-sm text-red-400" role="alert">
          Unable to load this room's spatial data.
        </div>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-3 text-xs" data-testid="layout-state" data-layout-state={view.data?.layout_state ?? "incomplete"}>
            {view.data?.layout_state === "validated" ? (
              <span className="rounded-sm bg-green-900 px-2 py-0.5 text-green-100">Validated layout</span>
            ) : (
              <span className="rounded-sm bg-yellow-900 px-2 py-0.5 text-yellow-100">Incomplete layout: some data is missing, so this is not a full model</span>
            )}
            {calibration && (
              <span className="text-slate-400">
                Calibration: 1 {calibration.source_units} = {Number(calibration.mm_per_unit.toPrecision(6))} mm ·{" "}
                {calibration.error_bound_mm == null ? "error bound unverified" : `error ≤ ±${calibration.error_bound_mm} mm`} · {calibration.confidence} confidence
              </span>
            )}
          </div>
          {reasons.length > 0 && (
            <ul className="mb-3 list-disc pl-5 text-xs text-yellow-400" data-testid="incomplete-reasons">
              {reasons.map((reason) => (
                <li key={reason}>{explainIncomplete(reason)}</li>
              ))}
            </ul>
          )}
          <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
            {scene ? (
              <section
                ref={viewportRef}
                className="layout3d-viewport rounded-sm border border-slate-800"
                tabIndex={0}
                data-testid="scene-viewport"
                data-scale-px-per-mm={scene.scale.toFixed(6)}
                onPointerDown={onPointerDown}
                onPointerMove={onPointerMove}
                onPointerUp={endDrag}
                onPointerCancel={endDrag}
                onKeyDown={onViewportKeyDown}
                aria-label={`Interactive 3D layout of ${view.data?.room_name ?? "room"}. Use arrow keys to pan, plus and minus to zoom, zero to reset.`}
              >
                <div
                  className="layout3d-world"
                  style={{
                    width: scene.floor.width,
                    height: scene.floor.height,
                    marginLeft: -scene.floor.width / 2,
                    marginTop: -scene.floor.height / 2,
                    transform: `translate3d(${pan.x}px, ${pan.y}px, 0) rotateX(${pitch}deg) rotateZ(${yaw}deg) scale(${zoom})`,
                  }}
                >
                  <div className="layout3d-floor" style={{ backgroundSize: `${gridPx}px ${gridPx}px` }} data-testid="scene-floor" data-grid-px={gridPx.toFixed(2)}>
                    <span className="layout3d-floor-label">{view.data?.room_name}</span>
                    <span className="layout3d-origin" aria-hidden="true" style={{ left: -scene.floor.boundsMm.minX * scene.scale - 1, top: -scene.floor.boundsMm.minY * scene.scale - 1 }} />
                    {scene.boundaryPx && (
                      <svg className="layout3d-boundary" width={scene.floor.width} height={scene.floor.height} aria-hidden="true" data-testid="scene-boundary">
                        <polygon points={scene.boundaryPx.map((p) => p.join(",")).join(" ")} fill="rgb(56 189 248 / .06)" stroke="#38bdf8" strokeWidth={2} />
                      </svg>
                    )}
                    {thermal !== "none" && <ThermalFloor3D scene={scene} map={thermalMap.data} layout={thermalLayout.data} />}
                    {scene.racks.map((box) => (
                      <Cuboid
                        key={box.id}
                        box={box}
                        color={states.get(box.id) ? STATE_COLORS[states.get(box.id)!.state] : DEFAULT_RACK_COLOR}
                        selected={selected?.id === box.id}
                        onSelect={() => setSelected({ kind: "rack", id: box.id })}
                        overlayItem={states.get(box.id)}
                        overlayKind={overlay}
                        equipment={rackEquipment.filter((item) => item.rack_id === box.id)}
                      />
                    ))}
                    {scene.floorEquipment.map((box) => (
                      <Cuboid
                        key={box.id}
                        box={box}
                        color={states.get(box.id) ? STATE_COLORS[states.get(box.id)!.state] : DEFAULT_EQUIPMENT_COLOR}
                        selected={selected?.id === box.id}
                        onSelect={() => setSelected({ kind: "equipment", id: box.id })}
                        overlayItem={states.get(box.id)}
                        overlayKind={overlay}
                      />
                    ))}
                    {scene.pins.map((pin) => (
                      <div key={pin.id} className="layout3d-pin" style={{ left: pin.left, top: pin.top }} data-asset-id={pin.id} data-testid={`pin-${pin.id}`} title={`${pin.label}: ${pin.reason}`}>
                        <span aria-hidden="true">?</span>
                      </div>
                    ))}
                  </div>
                </div>
                <div className="layout3d-controls" aria-label="3D view controls">
                  <label>
                    Orbit <input type="range" min="-180" max="180" value={yaw} onChange={(event) => setYaw(+event.target.value)} />
                  </label>
                  <label>
                    Tilt <input type="range" min="10" max="88" value={pitch} onChange={(event) => setPitch(+event.target.value)} />
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
                <p className="layout3d-provenance" data-testid="scene-provenance">
                  Real scale: 1 px = {(1 / scene.scale).toFixed(1)} mm in the scene. Rack footprints are catalog width × depth; heights are rack units × 44.45 mm.
                  Grid squares: {niceGridInterval(scene.scale * zoom, 56) >= 1000 ? `${niceGridInterval(scene.scale * zoom, 56) / 1000} m` : `${niceGridInterval(scene.scale * zoom, 56)} mm`}.
                </p>
              </section>
            ) : (
              <div className="rounded-sm border border-dashed border-slate-700 p-8 text-sm text-slate-400" role="status" data-testid="scene-empty">
                There is no room boundary, recorded room size or positioned rack for this room yet, so there is nothing to scale a model against. Set a
                room boundary on the 2D floor plan page or give racks floor coordinates.
              </div>
            )}
            <aside className="rounded-sm border border-slate-800 bg-slate-900 p-4">
              <h2 className="font-semibold">Selection</h2>
              {selectedBox ? (
                <>
                  <h3 className="mt-3 text-lg font-semibold" data-testid="selected-name">
                    {selectedBox.label}
                  </h3>
                  <p className="text-xs text-slate-500" data-testid="selected-asset-id" data-asset-id={selectedBox.id}>
                    {selectedBox.assetTag} · {selectedBox.id}
                  </p>
                  <dl className="mt-4 space-y-2 text-sm">
                    <div>
                      <dt className="text-slate-500">Room position</dt>
                      <dd>
                        {selectedBox.mm.x}, {selectedBox.mm.y} mm
                      </dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Rotation</dt>
                      <dd>{selectedBox.rotationDeg}°</dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Footprint (W × D)</dt>
                      <dd>
                        {selectedBox.mm.width} × {selectedBox.mm.depth} mm
                      </dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Height</dt>
                      <dd>
                        {selectedBox.mm.height} mm{selectedBox.heightU ? ` (${selectedBox.heightU}U)` : ""}
                      </dd>
                    </div>
                    {selectedBox.kind === "rack" && (
                      <div>
                        <dt className="text-slate-500">Placed equipment</dt>
                        <dd>{rackEquipment.filter((item) => item.rack_id === selectedBox.id).length || "None recorded"}</dd>
                      </div>
                    )}
                    {overlay !== "none" && (
                      <div data-testid="selected-overlay">
                        <dt className="text-slate-500">{OVERLAY_KINDS.find((k) => k.id === overlay)?.label} state</dt>
                        <dd>{selectedOverlay ? `${STATE_LABEL[selectedOverlay.state]}: ${selectedOverlay.reason}` : "No data for this asset"}</dd>
                      </div>
                    )}
                  </dl>
                  <Link
                    to={selectedBox.kind === "rack" ? `/racks/${selectedBox.id}` : `/equipment/${selectedBox.id}`}
                    className="mt-5 inline-block rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500"
                  >
                    {selectedBox.kind === "rack" ? "Open rack elevation" : "Open equipment"}
                  </Link>
                </>
              ) : (
                <p className="mt-3 text-sm text-slate-500">Select a rack or equipment item in the scene.</p>
              )}

              {scene && (scene.missingRackPosition.length > 0 || scene.missingRackDimensions.length > 0 || scene.missingEquipmentPosition.length > 0 || scene.pins.length > 0) && (
                <div className="mt-4 border-t border-slate-800 pt-3 text-xs text-yellow-400" role="status" data-testid="incomplete-panel">
                  <p className="mb-1 font-medium">Not drawn to scale (data incomplete)</p>
                  <ul className="list-disc space-y-0.5 pl-5">
                    {scene.missingRackPosition.map((r) => (
                      <li key={r.id}>
                        <Link className="underline" to={`/racks/${r.id}`}>
                          Rack {r.name}
                        </Link>
                        : placed in this room but no floor position is recorded
                      </li>
                    ))}
                    {scene.missingRackDimensions.map((r) => (
                      <li key={r.id}>Rack {r.name}: catalog footprint unknown</li>
                    ))}
                    {scene.missingEquipmentPosition.map((e) => (
                      <li key={e.id}>{e.hostname ?? e.asset_tag}: no linked floor position</li>
                    ))}
                    {scene.pins.map((p) => (
                      <li key={p.id}>
                        {p.label}: {p.reason}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {!scene && (view.data?.racks.length ?? 0) > 0 && (
                <p className="mt-4 border-t border-slate-800 pt-3 text-xs text-yellow-400" data-testid="no-scene-racks">
                  {view.data!.racks.length} rack{view.data!.racks.length === 1 ? " is" : "s are"} placed in this room without floor coordinates, so none can be drawn.
                </p>
              )}

              {thermal !== "none" && (
                <div className="mt-4 border-t border-slate-800 pt-3 text-xs text-slate-400" data-testid="thermal-3d-panel">
                  <p className="mb-1 font-medium text-slate-300">{metricLabel(thermal)} heat map</p>
                  {thermalMap.isLoading && <p role="status">Loading…</p>}
                  {thermalMap.isError && <p role="alert" className="text-red-400">Could not load the heat map.</p>}
                  {thermalMap.data && (
                    <>
                      <p data-testid="thermal-3d-state" data-map-state={thermalMap.data.state}>
                        <span aria-hidden="true">{MAP_STATE_META[thermalMap.data.state].glyph}</span> {MAP_STATE_META[thermalMap.data.state].label} · {thermalMap.data.quality.fresh_count} fresh, {thermalMap.data.quality.stale_count} stale, {thermalMap.data.quality.missing_count} missing · as of {new Date(thermalMap.data.as_of).toLocaleTimeString()}
                      </p>
                      {thermalMap.data.state_reasons.map((r) => (
                        <p key={r} className="text-yellow-400">{explainReason(r)}</p>
                      ))}
                      <p className="mt-1">{thermalMap.data.disclaimer}</p>
                      <ul className="mt-1 space-y-0.5" data-testid="thermal-3d-sensors">
                        {thermalMap.data.sensors.map((p) => (
                          <li key={p.sensor_id}><span aria-hidden="true">{SENSOR_STATE_META[p.state].glyph}</span> {describeSensor(p)}</li>
                        ))}
                      </ul>
                    </>
                  )}
                  <ThermalLegend map={thermalMap.data} />
                </div>
              )}

              {overlay !== "none" && (
                <div className="mt-4 border-t border-slate-800 pt-3 text-xs text-slate-400" data-testid="overlay-legend">
                  <p className="mb-1 font-medium text-slate-300">{OVERLAY_KINDS.find((k) => k.id === overlay)?.label} overlay</p>
                  <p className="mb-1">{OVERLAY_KINDS.find((k) => k.id === overlay)?.source}</p>
                  {overlays.isLoading && <p>Loading…</p>}
                  {overlays.isError && <p role="alert" className="text-red-400">Could not load this overlay.</p>}
                  <ul className="space-y-0.5">
                    {(Object.keys(STATE_COLORS) as (keyof typeof STATE_COLORS)[]).map((state) => (
                      <li key={state} className="flex items-center gap-1.5">
                        <span className="inline-block h-3 w-3" style={{ background: STATE_COLORS[state] }} /> {STATE_SYMBOL[state]} {STATE_LABEL[state]}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <p className="mt-4 text-xs text-slate-600">
                Layers: room boundary · racks · floor-standing equipment · rack-mounted equipment on rack faces · optional thermal layer (interpolated
                heat map, sensors, cooling units; not CFD). Racks with no room placement anywhere are not listed; check the{" "}
                <Link to="/racks" className="text-blue-400 hover:underline">
                  Racks
                </Link>{" "}
                page.
              </p>
            </aside>
          </div>
        </>
      )}
    </div>
  );
}
