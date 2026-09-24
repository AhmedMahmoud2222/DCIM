import { useMutation } from "@tanstack/react-query";
import { ChangeEvent, KeyboardEvent, MouseEvent, PointerEvent as ReactPointerEvent, useEffect, useRef, useState } from "react";

import {
  CatalogGraphicMarkerInput,
  createGraphicMarker,
  deleteGraphicMarker,
  fetchGraphicImage,
  updateGraphicMarker,
  uploadGraphic,
} from "@/features/catalog-designer/api";
import { ApiError } from "@/lib/apiClient";
import { CatalogGraphic, CatalogGraphicMarker, CatalogModelRevisionDetail } from "@/types";

const SIDES = ["front", "rear"] as const;
type Side = (typeof SIDES)[number];
type MarkerType = CatalogGraphicMarker["marker_type"];

const KEYBOARD_NUDGE_STEP = 0.01;
const DRAG_COMMIT_EPSILON = 0.001;

const MARKER_TYPE_LABELS: Record<MarkerType, string> = {
  network_port: "Network port",
  power_supply: "Power supply",
  module: "Module",
  other: "Other",
};

const MARKER_COLORS: Record<MarkerType, string> = {
  network_port: "#0ea5e9",
  power_supply: "#f97316",
  module: "#a855f7",
  other: "#64748b",
};

/** Embedded as a section inside RevisionEditorPage.tsx — same placement convention as
 * TemplateEditors, not a separate route, since placing a marker needs the network-port/
 * power-supply rows that page has already loaded for the same revision. */
export function GraphicsEditorPage({
  revision,
  readOnly,
  onChanged,
}: {
  revision: CatalogModelRevisionDetail;
  readOnly: boolean;
  onChanged: () => void;
}) {
  return (
    <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-2">
      {SIDES.map((side) => (
        <SideGraphicPanel key={side} side={side} revision={revision} readOnly={readOnly} onChanged={onChanged} />
      ))}
    </div>
  );
}

function SideGraphicPanel({
  side,
  revision,
  readOnly,
  onChanged,
}: {
  side: Side;
  revision: CatalogModelRevisionDetail;
  readOnly: boolean;
  onChanged: () => void;
}) {
  const graphic = revision.graphics.find((g) => g.side === side) ?? null;
  const fileInputRef = useRef<HTMLInputElement>(null);

  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadGraphic(revision.id, side, file, revision.version),
    onSuccess: onChanged,
  });

  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) uploadMutation.mutate(file);
    e.target.value = "";
  }

  return (
    <section className="rounded border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold capitalize text-slate-300">{side} view</h2>
        {!readOnly && (
          <>
            <input ref={fileInputRef} type="file" accept="image/png,image/jpeg" className="hidden" onChange={handleFileChange} />
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadMutation.isPending}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
            >
              {uploadMutation.isPending ? "Uploading…" : graphic ? "Replace image" : "Upload image"}
            </button>
          </>
        )}
      </div>
      {uploadMutation.isError && (
        <p className="mb-2 text-xs text-red-400">
          {uploadMutation.error instanceof ApiError ? uploadMutation.error.detail : "Upload failed."}
        </p>
      )}
      {graphic ? (
        <FrontRearImageCanvas graphic={graphic} side={side} revision={revision} readOnly={readOnly} onChanged={onChanged} />
      ) : (
        <p className="text-xs italic text-slate-500">No {side} image uploaded yet.</p>
      )}
    </section>
  );
}

/** Blob-URL bridge for an endpoint that needs an Authorization header a plain
 * `<img src>` can't send. Keyed on graphic.id (not revisionId/side, which can stay the
 * same across a re-upload) so a replaced image is refetched. */
function useGraphicImageUrl(revisionId: string, side: Side, graphicId: string): string | null {
  const [url, setUrl] = useState<string | null>(null);
  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    setUrl(null);
    fetchGraphicImage(revisionId, side).then((blob) => {
      if (cancelled) return;
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphicId]);
  return url;
}

interface PendingPlacement {
  type: MarkerType;
  x: number;
  y: number;
}

function FrontRearImageCanvas({
  graphic,
  side,
  revision,
  readOnly,
  onChanged,
}: {
  graphic: CatalogGraphic;
  side: Side;
  revision: CatalogModelRevisionDetail;
  readOnly: boolean;
  onChanged: () => void;
}) {
  const imageUrl = useGraphicImageUrl(revision.id, side, graphic.id);
  const [placingType, setPlacingType] = useState<MarkerType | null>(null);
  const [pendingPlacement, setPendingPlacement] = useState<PendingPlacement | null>(null);
  const [editingMarkerId, setEditingMarkerId] = useState<string | null>(null);
  const [dragPosition, setDragPosition] = useState<{ id: string; x: number; y: number } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const draggingIdRef = useRef<string | null>(null);
  const nudgeInFlightRef = useRef(false);

  const createMutation = useMutation({
    mutationFn: (body: CatalogGraphicMarkerInput) => createGraphicMarker(revision.id, graphic.id, body, revision.version),
    onSuccess: () => {
      setPendingPlacement(null);
      onChanged();
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ markerId, body }: { markerId: string; body: Partial<CatalogGraphicMarkerInput> }) =>
      updateGraphicMarker(revision.id, graphic.id, markerId, body, revision.version),
    onSuccess: onChanged,
    onSettled: () => {
      nudgeInFlightRef.current = false;
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (markerId: string) => deleteGraphicMarker(revision.id, graphic.id, markerId, revision.version),
    onSuccess: () => {
      setEditingMarkerId(null);
      onChanged();
    },
  });

  function fractionalPositionFromEvent(e: { clientX: number; clientY: number }): { x: number; y: number } {
    const rect = containerRef.current!.getBoundingClientRect();
    const x = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const y = Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height));
    return { x, y };
  }

  function handleContainerClick(e: MouseEvent) {
    if (readOnly || !placingType) return;
    if ((e.target as HTMLElement).closest("[data-marker]")) return;
    const { x, y } = fractionalPositionFromEvent(e);
    setPendingPlacement({ type: placingType, x, y });
    setPlacingType(null);
  }

  function handleMarkerPointerDown(e: ReactPointerEvent, marker: CatalogGraphicMarker) {
    if (readOnly) return;
    e.stopPropagation();
    (e.target as Element).setPointerCapture(e.pointerId);
    draggingIdRef.current = marker.id;
    setDragPosition({ id: marker.id, x: marker.marker_x, y: marker.marker_y });
  }

  function handleContainerPointerMove(e: ReactPointerEvent) {
    if (!draggingIdRef.current) return;
    const { x, y } = fractionalPositionFromEvent(e);
    setDragPosition({ id: draggingIdRef.current, x, y });
  }

  function handleMarkerPointerUp(e: ReactPointerEvent, marker: CatalogGraphicMarker) {
    if (!draggingIdRef.current) return;
    (e.target as Element).releasePointerCapture(e.pointerId);
    const { x, y } = fractionalPositionFromEvent(e);
    draggingIdRef.current = null;
    setDragPosition(null);
    if (Math.abs(x - marker.marker_x) > DRAG_COMMIT_EPSILON || Math.abs(y - marker.marker_y) > DRAG_COMMIT_EPSILON) {
      updateMutation.mutate({ markerId: marker.id, body: { marker_x: x, marker_y: y } });
    }
  }

  function handleMarkerKeyDown(e: KeyboardEvent, marker: CatalogGraphicMarker) {
    if (readOnly) return;
    switch (e.key) {
      case "ArrowLeft":
      case "ArrowRight":
      case "ArrowUp":
      case "ArrowDown": {
        e.preventDefault();
        // Guards against rapid repeated key presses racing each other: each nudge reads
        // `marker.marker_x/y` and the revision's `version` from this render's closure, so
        // firing a second PATCH before the first's response has updated both would send a
        // now-stale version and get a legitimate (but silently-dropped, from the user's
        // point of view) 409 — found via this feature's own E2E test pressing ArrowRight
        // twice in a row. A ref (not `updateMutation.isPending`) is required here: two
        // keydown events dispatched back to back can both run before React re-renders
        // with the first mutate() call's updated `isPending`, so a state-based check
        // reads the same stale `false` for both and lets them race anyway. The ref
        // updates synchronously, so the second keydown always sees the first's claim.
        if (nudgeInFlightRef.current) break;
        nudgeInFlightRef.current = true;
        const dx = e.key === "ArrowLeft" ? -KEYBOARD_NUDGE_STEP : e.key === "ArrowRight" ? KEYBOARD_NUDGE_STEP : 0;
        const dy = e.key === "ArrowUp" ? -KEYBOARD_NUDGE_STEP : e.key === "ArrowDown" ? KEYBOARD_NUDGE_STEP : 0;
        const x = Math.min(1, Math.max(0, marker.marker_x + dx));
        const y = Math.min(1, Math.max(0, marker.marker_y + dy));
        updateMutation.mutate({ markerId: marker.id, body: { marker_x: x, marker_y: y } });
        break;
      }
      case "Enter":
      case " ":
        e.preventDefault();
        setEditingMarkerId(marker.id);
        break;
      case "Delete":
      case "Backspace":
        e.preventDefault();
        if (window.confirm(`Remove this ${MARKER_TYPE_LABELS[marker.marker_type]} marker?`)) {
          deleteMutation.mutate(marker.id);
        }
        break;
    }
  }

  const editingMarker = graphic.markers.find((m) => m.id === editingMarkerId) ?? null;

  return (
    <div>
      {!readOnly && (
        <div className="mb-2 flex flex-wrap gap-1">
          {(Object.keys(MARKER_TYPE_LABELS) as MarkerType[]).map((type) => (
            <button
              key={type}
              onClick={() => setPlacingType((current) => (current === type ? null : type))}
              className={`rounded px-2 py-1 text-xs ${
                placingType === type ? "bg-blue-600 text-white" : "bg-slate-800 text-slate-300 hover:bg-slate-700"
              }`}
            >
              {placingType === type ? `Click image to place…` : `+ ${MARKER_TYPE_LABELS[type]}`}
            </button>
          ))}
        </div>
      )}
      <div
        ref={containerRef}
        data-testid="graphic-canvas"
        onClick={handleContainerClick}
        onPointerMove={handleContainerPointerMove}
        className={`relative w-full overflow-hidden rounded border border-slate-800 bg-slate-950 ${placingType ? "cursor-crosshair" : ""}`}
        style={{ aspectRatio: `${graphic.width_px} / ${graphic.height_px}` }}
      >
        {imageUrl ? (
          <img
            src={imageUrl}
            alt={`${side} view of this model`}
            className="pointer-events-none h-full w-full select-none object-contain"
            draggable={false}
          />
        ) : (
          <div className="flex h-40 items-center justify-center text-xs text-slate-500">Loading image…</div>
        )}
        <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="absolute inset-0 h-full w-full">
          {graphic.markers.map((marker) => {
            const pos = dragPosition && dragPosition.id === marker.id ? dragPosition : { x: marker.marker_x, y: marker.marker_y };
            return (
              <g
                key={marker.id}
                data-marker
                tabIndex={readOnly ? -1 : 0}
                role="button"
                aria-label={`${MARKER_TYPE_LABELS[marker.marker_type]} marker${
                  marker.label ? `, ${marker.label}` : ""
                }, at ${Math.round(pos.x * 100)} percent across, ${Math.round(pos.y * 100)} percent down. Arrow keys move, Enter edits, Delete removes.`}
                className="cursor-move outline-none"
                onPointerDown={(e) => handleMarkerPointerDown(e, marker)}
                onPointerUp={(e) => handleMarkerPointerUp(e, marker)}
                onKeyDown={(e) => handleMarkerKeyDown(e, marker)}
                onDoubleClick={() => setEditingMarkerId(marker.id)}
              >
                <circle cx={pos.x} cy={pos.y} r={0.014} fill={MARKER_COLORS[marker.marker_type]} stroke="#0f172a" strokeWidth={0.003} />
                <circle cx={pos.x} cy={pos.y} r={0.02} fill="transparent" stroke="#3b82f6" strokeWidth={0.004} className="opacity-0 focus-visible:opacity-100" />
              </g>
            );
          })}
        </svg>
      </div>
      {pendingPlacement && (
        <MarkerPlacementForm
          placement={pendingPlacement}
          revision={revision}
          isPending={createMutation.isPending}
          error={createMutation.error instanceof ApiError ? createMutation.error.detail : null}
          onCancel={() => setPendingPlacement(null)}
          onCreate={(body) => createMutation.mutate(body)}
        />
      )}
      {editingMarker && (
        <MarkerEditForm
          marker={editingMarker}
          revision={revision}
          isSaving={updateMutation.isPending}
          isDeleting={deleteMutation.isPending}
          onClose={() => setEditingMarkerId(null)}
          onSave={(body) => updateMutation.mutate({ markerId: editingMarker.id, body })}
          onDelete={() => deleteMutation.mutate(editingMarker.id)}
        />
      )}
    </div>
  );
}

function MarkerPlacementForm({
  placement,
  revision,
  isPending,
  error,
  onCancel,
  onCreate,
}: {
  placement: PendingPlacement;
  revision: CatalogModelRevisionDetail;
  isPending: boolean;
  error: string | null;
  onCancel: () => void;
  onCreate: (body: CatalogGraphicMarkerInput) => void;
}) {
  const [targetId, setTargetId] = useState("");
  const [label, setLabel] = useState("");

  const needsTarget = placement.type === "network_port" || placement.type === "power_supply";
  const targetOptions = placement.type === "network_port" ? revision.network_ports : placement.type === "power_supply" ? revision.power_supplies : [];

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (needsTarget && !targetId) return;
    onCreate({
      marker_type: placement.type,
      marker_x: placement.x,
      marker_y: placement.y,
      network_port_template_id: placement.type === "network_port" ? targetId : null,
      power_supply_template_id: placement.type === "power_supply" ? targetId : null,
      label: label.trim() || null,
    });
  }

  return (
    <form onSubmit={handleSubmit} className="mt-2 space-y-2 rounded border border-slate-700 bg-slate-950 p-3">
      <p className="text-xs text-slate-400">Placing a {MARKER_TYPE_LABELS[placement.type]} marker.</p>
      {needsTarget ? (
        <select
          value={targetId}
          onChange={(e) => setTargetId(e.target.value)}
          required
          className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
        >
          <option value="">Select {placement.type === "network_port" ? "a port" : "a power supply"}…</option>
          {targetOptions.map((option) => (
            <option key={option.id} value={option.id}>
              {"display_name" in option ? option.display_name : option.label}
            </option>
          ))}
        </select>
      ) : (
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Label (optional)"
          className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
        />
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          disabled={isPending || (needsTarget && !targetId)}
          className="rounded bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
        >
          {isPending ? "Placing…" : "Place marker"}
        </button>
        <button type="button" onClick={onCancel} className="rounded bg-slate-800 px-3 py-1 text-xs text-slate-300 hover:bg-slate-700">
          Cancel
        </button>
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}
    </form>
  );
}

function MarkerEditForm({
  marker,
  revision,
  isSaving,
  isDeleting,
  onClose,
  onSave,
  onDelete,
}: {
  marker: CatalogGraphicMarker;
  revision: CatalogModelRevisionDetail;
  isSaving: boolean;
  isDeleting: boolean;
  onClose: () => void;
  onSave: (body: Partial<CatalogGraphicMarkerInput>) => void;
  onDelete: () => void;
}) {
  const [label, setLabel] = useState(marker.label ?? "");
  const needsTarget = marker.marker_type === "network_port" || marker.marker_type === "power_supply";
  const currentTargetId = marker.network_port_template_id ?? marker.power_supply_template_id ?? "";
  const targetOptions = marker.marker_type === "network_port" ? revision.network_ports : marker.marker_type === "power_supply" ? revision.power_supplies : [];
  const targetName = targetOptions.find((o) => o.id === currentTargetId);

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    onSave({ label: label.trim() || null });
  }

  return (
    <form onSubmit={handleSubmit} className="mt-2 space-y-2 rounded border border-slate-700 bg-slate-950 p-3">
      <p className="text-xs text-slate-400">
        Editing {MARKER_TYPE_LABELS[marker.marker_type]} marker
        {needsTarget && targetName && (
          <> linked to <span className="text-slate-300">{"display_name" in targetName ? targetName.display_name : targetName.label}</span></>
        )}
        .
      </p>
      {!needsTarget && (
        <input
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Label (optional)"
          className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
        />
      )}
      <div className="flex gap-2">
        {!needsTarget && (
          <button
            type="submit"
            disabled={isSaving}
            className="rounded bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            {isSaving ? "Saving…" : "Save"}
          </button>
        )}
        <button
          type="button"
          onClick={onDelete}
          disabled={isDeleting}
          className="rounded bg-red-900/50 px-3 py-1 text-xs text-red-200 hover:bg-red-900 disabled:opacity-50"
        >
          {isDeleting ? "Removing…" : "Remove marker"}
        </button>
        <button type="button" onClick={onClose} className="rounded bg-slate-800 px-3 py-1 text-xs text-slate-300 hover:bg-slate-700">
          Close
        </button>
      </div>
    </form>
  );
}
