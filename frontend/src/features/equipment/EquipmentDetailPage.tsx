import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useHasPermission } from "@/features/auth/useAuthorization";
import { connectEquipmentPort, getEquipment, getEquipmentPorts, moveEquipment, retireEquipment } from "@/features/equipment/api";
import { ImpactAnalysisModal, ImpactTarget } from "@/features/impact/ImpactAnalysisModal";
import { createEquipmentFeed, getEquipmentPowerSummary } from "@/features/power/api";
import { listRacks, listRooms } from "@/features/racks/api";
import {
  acknowledgeAlarm,
  getAlarmHistory,
  getLatestPortStatusForEquipment,
  getLatestTelemetry,
  getTelemetryHistory,
} from "@/features/telemetry/api";
import { TelemetryTrend } from "@/features/telemetry/TelemetryTrend";
import { ApiError } from "@/lib/apiClient";
import { PLACEMENT_TYPES, PlacementType, PORT_CONNECTION_STATUSES, PortConnectionStatus, SIDES, Side } from "@/types";

const REDUNDANCY_LABELS: Record<string, { text: string; color: string }> = {
  dual_feed_healthy: { text: "Dual-feed (A+B), healthy", color: "bg-green-900 text-green-200" },
  single_feed: { text: "Single feed (no redundancy)", color: "bg-slate-700 text-slate-300" },
  degraded: { text: "Redundancy degraded", color: "bg-yellow-800 text-yellow-100" },
  no_power_modeled: { text: "No power modeled", color: "bg-slate-800 text-slate-400" },
};

const LIFECYCLE_COLORS: Record<string, string> = {
  planned: "bg-slate-700 text-slate-200",
  installed: "bg-blue-700 text-blue-100",
  active: "bg-green-700 text-green-100",
  maintenance: "bg-yellow-700 text-yellow-100",
  decommissioned: "bg-orange-800 text-orange-100",
  removed: "bg-red-900 text-red-100",
};

export function EquipmentDetailPage() {
  const { equipmentId } = useParams<{ equipmentId: string }>();
  const queryClient = useQueryClient();
  const [showMoveForm, setShowMoveForm] = useState(false);
  const [placementType, setPlacementType] = useState<PlacementType>("floor_standing");
  const [roomId, setRoomId] = useState("");
  const [rackId, setRackId] = useState("");
  const [uStart, setUStart] = useState("");
  const [uEnd, setUEnd] = useState("");
  const [side, setSide] = useState<Side>("front");
  const [selectedMetric, setSelectedMetric] = useState<string | null>(null);
  const [historyHours, setHistoryHours] = useState(24);
  const [impactTarget, setImpactTarget] = useState<ImpactTarget | null>(null);

  const equipmentQuery = useQuery({
    queryKey: ["equipment", equipmentId],
    queryFn: () => getEquipment(equipmentId!),
    enabled: !!equipmentId,
  });
  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms });
  const racksQuery = useQuery({ queryKey: ["racks"], queryFn: listRacks });
  const powerSummaryQuery = useQuery({
    queryKey: ["power", "equipment-summary", equipmentId],
    queryFn: () => getEquipmentPowerSummary(equipmentId!),
    enabled: !!equipmentId,
  });
  const latestTelemetryQuery = useQuery({
    queryKey: ["telemetry", "latest", equipmentId], queryFn: () => getLatestTelemetry(equipmentId!), enabled: !!equipmentId,
  });
  const alarmHistoryQuery = useQuery({
    queryKey: ["alarms", "equipment", equipmentId], queryFn: () => getAlarmHistory(equipmentId!), enabled: !!equipmentId,
  });
  const portsQuery = useQuery({
    queryKey: ["equipment", equipmentId, "ports"], queryFn: () => getEquipmentPorts(equipmentId!), enabled: !!equipmentId,
  });
  const portStatusQuery = useQuery({
    queryKey: ["telemetry", "port-status", "equipment", equipmentId],
    queryFn: () => getLatestPortStatusForEquipment(equipmentId!),
    enabled: !!equipmentId,
    refetchInterval: 5_000,
  });
  const portStatusByPortId = new Map(
    (portStatusQuery.data ?? []).filter((s) => s.equipment_port_id).map((s) => [s.equipment_port_id, s]),
  );
  const canManageCabling = useHasPermission("equipment:manage");
  const metric = selectedMetric ?? latestTelemetryQuery.data?.[0]?.metric ?? null;
  const historyQuery = useQuery({
    queryKey: ["telemetry", "history", equipmentId, metric, historyHours],
    queryFn: () => getTelemetryHistory(equipmentId!, metric!, new Date(Date.now() - historyHours * 3_600_000), new Date()),
    enabled: !!equipmentId && !!metric,
  });
  const acknowledgeMutation = useMutation({
    mutationFn: acknowledgeAlarm,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["alarms", "equipment", equipmentId] }),
  });

  const addFeedMutation = useMutation({
    mutationFn: (label: string) => createEquipmentFeed(equipmentId!, label),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["power", "equipment-summary", equipmentId] }),
  });

  const [connectingPortId, setConnectingPortId] = useState<string | null>(null);
  const connectMutation = useMutation({
    mutationFn: (vars: { portId: string; targetPortId?: string; targetPowerNodeId?: string; cableId?: string; status: PortConnectionStatus }) =>
      connectEquipmentPort(equipmentId!, {
        port_id: vars.portId, target_port_id: vars.targetPortId || null, target_power_node_id: vars.targetPowerNodeId || null,
        cable_id: vars.cableId || null, status: vars.status,
      }),
    onSuccess: () => {
      setConnectingPortId(null);
      queryClient.invalidateQueries({ queryKey: ["equipment", equipmentId, "ports"] });
    },
  });

  const moveMutation = useMutation({
    mutationFn: () =>
      moveEquipment(equipmentId!, {
        placement_type: placementType,
        room_id: roomId,
        rack_id: placementType === "rack_mounted" ? rackId : undefined,
        u_start: placementType === "rack_mounted" ? Number(uStart) : undefined,
        u_end: placementType === "rack_mounted" ? Number(uEnd) : undefined,
        side: placementType === "rack_mounted" ? side : undefined,
      }),
    onSuccess: () => {
      setShowMoveForm(false);
      queryClient.invalidateQueries({ queryKey: ["equipment"] });
    },
  });

  const retireMutation = useMutation({
    mutationFn: () => retireEquipment(equipmentId!),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["equipment"] }),
  });

  function handleMoveSubmit(e: FormEvent) {
    e.preventDefault();
    if (!roomId) return;
    if (placementType === "rack_mounted" && (!rackId || !uStart || !uEnd)) return;
    moveMutation.mutate();
  }

  if (equipmentQuery.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (equipmentQuery.error) return <p className="text-sm text-red-400">{(equipmentQuery.error as Error).message}</p>;
  const equipment = equipmentQuery.data;
  if (!equipment) return null;

  const currentRoom = roomsQuery.data?.items.find((r) => r.id === equipment.placement?.room_id);
  const currentRack = equipment.placement?.rack_id ? racksQuery.data?.items.find((r) => r.id === equipment.placement!.rack_id) : null;

  return (
    <div>
      <Link to="/equipment" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Equipment
      </Link>
      <div className="mb-6 flex items-start justify-between">
        <div>
          <h1 className="text-lg font-semibold">{equipment.hostname ?? equipment.asset_tag}</h1>
          <p className="font-mono text-sm text-slate-400">{equipment.asset_tag}</p>
        </div>
        <span className={`rounded-sm px-2 py-0.5 text-xs ${LIFECYCLE_COLORS[equipment.lifecycle_status] ?? "bg-slate-700"}`}>
          {equipment.lifecycle_status}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-6">
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">Placement</h2>
          {equipment.placement ? (
            <dl className="space-y-1 text-sm">
              <div className="flex justify-between">
                <dt className="text-slate-400">Type</dt>
                <dd>{equipment.placement.placement_type}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-400">Room</dt>
                <dd>{currentRoom?.name ?? equipment.placement.room_id}</dd>
              </div>
              {equipment.placement.placement_type === "rack_mounted" && (
                <>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">Rack</dt>
                    <dd>{currentRack?.name ?? equipment.placement.rack_id}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-400">U-range</dt>
                    <dd>
                      U{equipment.placement.u_start}–{equipment.placement.u_end} ({equipment.placement.side})
                    </dd>
                  </div>
                </>
              )}
            </dl>
          ) : (
            <p className="text-sm italic text-slate-400">Not currently placed.</p>
          )}
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => setShowMoveForm((v) => !v)}
              className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700"
            >
              {equipment.placement ? "Move" : "Place"}
            </button>
            {equipment.placement && (
              <button
                onClick={() => retireMutation.mutate()}
                disabled={retireMutation.isPending}
                className="rounded-sm bg-red-900/50 px-3 py-1.5 text-sm text-red-200 hover:bg-red-900 disabled:opacity-50"
              >
                Retire placement
              </button>
            )}
          </div>

          {showMoveForm && (
            <form onSubmit={handleMoveSubmit} className="mt-4 space-y-2 border-t border-slate-800 pt-4">
              <select
                aria-label="Placement type"
                value={placementType}
                onChange={(e) => setPlacementType(e.target.value as PlacementType)}
                className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                {PLACEMENT_TYPES.map((pt) => (
                  <option key={pt} value={pt}>
                    {pt}
                  </option>
                ))}
              </select>
              <select
                aria-label="Placement room"
                value={roomId}
                onChange={(e) => setRoomId(e.target.value)}
                className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                <option value="">Select room…</option>
                {roomsQuery.data?.items.map((room) => (
                  <option key={room.id} value={room.id}>
                    {room.name} ({room.code})
                  </option>
                ))}
              </select>

              {placementType === "rack_mounted" && (
                <>
                  <select
                    aria-label="Placement rack"
                    value={rackId}
                    onChange={(e) => setRackId(e.target.value)}
                    className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                  >
                    <option value="">Select rack…</option>
                    {racksQuery.data?.items.map((rack) => (
                      <option key={rack.id} value={rack.id}>
                        {rack.name}
                      </option>
                    ))}
                  </select>
                  <div className="flex gap-2">
                    <input
                      aria-label="Rack starting unit"
                      type="number"
                      min={1}
                      value={uStart}
                      onChange={(e) => setUStart(e.target.value)}
                      placeholder="U start"
                      className="w-1/3 rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                    />
                    <input
                      aria-label="Rack ending unit, exclusive"
                      type="number"
                      min={2}
                      value={uEnd}
                      onChange={(e) => setUEnd(e.target.value)}
                      placeholder="U end (exclusive)"
                      className="w-1/3 rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                    />
                    <select
                      aria-label="Rack side"
                      value={side}
                      onChange={(e) => setSide(e.target.value as Side)}
                      className="w-1/3 rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                    >
                      {SIDES.map((s) => (
                        <option key={s} value={s}>
                          {s}
                        </option>
                      ))}
                    </select>
                  </div>
                </>
              )}

              <button
                type="submit"
                disabled={moveMutation.isPending}
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                Confirm
              </button>
              {moveMutation.isError && (
                <p role="alert" className="text-sm text-red-400">
                  {moveMutation.error instanceof ApiError && moveMutation.error.status === 409
                    ? "That U-range conflicts with existing equipment, or someone else moved this item — reload and try again."
                    : (moveMutation.error as Error).message}
                </p>
              )}
            </form>
          )}
        </div>

        <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">Details</h2>
          <dl className="space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-slate-400">Owner</dt>
              <dd>{equipment.owner ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-400">Service</dt>
              <dd>{equipment.service ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-400">Environment</dt>
              <dd>{equipment.environment ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-400">Version</dt>
              <dd>{equipment.version}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-400">Created</dt>
              <dd>{new Date(equipment.created_at).toLocaleString()}</dd>
            </div>
          </dl>
        </div>
      </div>

      <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">Power</h2>
          <div className="flex gap-2">
            <button
              onClick={() => addFeedMutation.mutate("Feed A")}
              disabled={addFeedMutation.isPending}
              className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
            >
              + Feed A
            </button>
            <button
              onClick={() => addFeedMutation.mutate("Feed B")}
              disabled={addFeedMutation.isPending}
              className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
            >
              + Feed B
            </button>
            <Link to="/power" className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-blue-400 hover:bg-slate-700">
              Open topology →
            </Link>
          </div>
        </div>
        {powerSummaryQuery.data && (
          <>
            {(() => {
              const r = REDUNDANCY_LABELS[powerSummaryQuery.data.redundancy_classification];
              return <span className={`mb-3 inline-block rounded-sm px-2 py-0.5 text-xs ${r.color}`}>{r.text}</span>;
            })()}
            <div className="mt-2 space-y-1">
              {powerSummaryQuery.data.feed_nodes.map((feed) => (
                <div key={feed.power_node_id} className="flex items-center justify-between rounded-sm bg-slate-800/50 px-3 py-1.5 text-xs">
                  <span className="font-mono text-slate-400">{feed.power_node_id.slice(0, 8)}…</span>
                  <span>{feed.feed_label ?? "unlabeled"}</span>
                  <span className={feed.has_upstream_path ? "text-green-400" : "text-red-400"}>
                    {feed.has_upstream_path ? "path OK" : "no upstream path"}
                  </span>
                  <span>{feed.effective_capacity_kw === null ? "capacity unknown" : `${feed.effective_capacity_kw.toFixed(1)} kW`}</span>
                  <button
                    data-testid="simulate-outage-button"
                    onClick={() =>
                      setImpactTarget({
                        type: "power_node", id: feed.power_node_id,
                        label: `${equipment.hostname ?? equipment.asset_tag} — ${feed.feed_label ?? "power feed"}`,
                      })
                    }
                    className="rounded-sm bg-red-900/60 px-2 py-0.5 text-xs text-red-200 hover:bg-red-900"
                  >
                    Simulate outage
                  </button>
                </div>
              ))}
              {powerSummaryQuery.data.feed_nodes.length === 0 && (
                <p className="text-xs italic text-slate-400">No power feeds modeled for this equipment yet.</p>
              )}
            </div>
            {powerSummaryQuery.data.effective_demand_kw !== null && (
              <p className="mt-2 text-xs text-slate-400">
                Effective demand: {powerSummaryQuery.data.effective_demand_kw.toFixed(1)} kW
              </p>
            )}
          </>
        )}
      </div>

      {equipment.catalog_model_revision_id && (
        <div className="mt-6 rounded-sm border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">Ports &amp; cabling</h2>
          {portsQuery.isLoading && <p className="text-sm text-slate-400">Loading ports…</p>}
          {portsQuery.data?.ports.length === 0 && (
            <p className="text-sm italic text-slate-400">This catalog model has no network ports defined.</p>
          )}
          <div className="space-y-2">
            {portsQuery.data?.ports.map((port) => {
              const liveStatus = portStatusByPortId.get(port.id);
              return (
              <div key={port.id} data-testid="equipment-port-row" className="rounded-sm bg-slate-800/50 p-2 text-xs">
                <div className="flex items-center justify-between">
                  <span className="font-medium text-slate-200">
                    {port.display_name} <span className="text-slate-400">({port.connector_type}, {port.media_type}, {port.side})</span>
                    {liveStatus?.status_level && (
                      <span
                        data-testid="port-live-link-state"
                        className={`ml-2 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase ${
                          liveStatus.status_level === "UP"
                            ? "bg-green-900 text-green-200"
                            : liveStatus.status_level === "DEGRADED"
                              ? "bg-yellow-800 text-yellow-100"
                              : "bg-red-900 text-red-200"
                        }`}
                      >
                        {liveStatus.status_level}
                      </span>
                    )}
                  </span>
                  <div className="flex gap-2">
                    {port.connection && (
                      <button
                        data-testid="simulate-failure-button"
                        onClick={() =>
                          setImpactTarget({
                            type: "network_port", id: port.id,
                            label: `${equipment.hostname ?? equipment.asset_tag} — ${port.display_name}`,
                          })
                        }
                        className="rounded-sm bg-red-900/60 px-2 py-0.5 text-xs text-red-200 hover:bg-red-900"
                      >
                        Simulate failure
                      </button>
                    )}
                    {canManageCabling && (
                      <button
                        onClick={() => setConnectingPortId((id) => (id === port.id ? null : port.id))}
                        className="rounded-sm bg-slate-700 px-2 py-0.5 text-xs text-slate-200 hover:bg-slate-600"
                      >
                        {port.connection ? "Re-patch" : "Connect"}
                      </button>
                    )}
                  </div>
                </div>
                {port.connection ? (
                  <p className="mt-1 text-slate-400">
                    {port.connection.status} → {port.connection.target_port_id ? `port ${port.connection.target_port_id.slice(0, 8)}…` : `PDU outlet ${port.connection.target_power_node_id?.slice(0, 8)}…`}
                    {port.connection.cable_id && <> · cable {port.connection.cable_id}</>}
                  </p>
                ) : (
                  <p className="mt-1 italic text-slate-400">Unassigned</p>
                )}
                {connectingPortId === port.id && (
                  <ConnectPortForm
                    isPending={connectMutation.isPending}
                    error={connectMutation.error instanceof ApiError ? connectMutation.error.detail : null}
                    onCancel={() => setConnectingPortId(null)}
                    onSubmit={(vars) => connectMutation.mutate({ portId: port.id, ...vars })}
                  />
                )}
              </div>
              );
            })}
          </div>
          {portsQuery.data && portsQuery.data.power_inlets.length > 0 && (
            <>
              <h3 className="mb-2 mt-4 text-xs font-semibold text-slate-400">Power inlets</h3>
              <div className="space-y-1">
                {portsQuery.data.power_inlets.map((inlet) => (
                  <div key={inlet.id} className="rounded-sm bg-slate-800/50 px-2 py-1 text-xs text-slate-300">
                    {inlet.label} ({inlet.connector_type})
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}

      <div className="mt-6 rounded-sm border border-slate-800 bg-slate-900 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">Live metrics</h2>
        {latestTelemetryQuery.isLoading && <p className="text-sm text-slate-400">Loading telemetry…</p>}
        {latestTelemetryQuery.isError && <p className="text-sm text-red-400">Telemetry is currently unavailable.</p>}
        {latestTelemetryQuery.data?.length === 0 && <p className="text-sm italic text-slate-400">No telemetry is associated with this equipment.</p>}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {latestTelemetryQuery.data?.map((reading) => {
            const ageMs = Date.now() - new Date(reading.occurred_at).getTime();
            // Two configured acquisition cycles is the MVP stale threshold.  The
            // backend supplies the integration-specific cadence in the same query.
            const stale = reading.expected_poll_interval_seconds != null && ageMs > reading.expected_poll_interval_seconds * 2_000;
            return <button key={reading.id} onClick={() => setSelectedMetric(reading.metric)} className="rounded-sm bg-slate-800/60 p-3 text-left hover:bg-slate-800">
              <p className="text-xs uppercase tracking-wide text-slate-400">{reading.metric}</p>
              <p className="mt-1 text-xl font-semibold">{reading.presentation_value} <span className="text-sm text-slate-400">{reading.presentation_unit}</span></p>
              <p className={stale ? "mt-1 text-xs text-yellow-400" : "mt-1 text-xs text-green-400"}>{stale ? "Stale" : "Current"} · occurred {new Date(reading.occurred_at).toLocaleString()}</p>
              {reading.received_at !== reading.occurred_at && <p className="mt-1 text-xs text-slate-400">received {new Date(reading.received_at).toLocaleString()}</p>}
            </button>;
          })}
        </div>
      </div>

      <div className="mt-6 grid gap-6 lg:grid-cols-2">
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
          <div className="mb-3 flex items-center justify-between"><h2 className="text-sm font-semibold text-slate-300">Metric history</h2>
            <select value={historyHours} onChange={(e) => setHistoryHours(Number(e.target.value))} className="rounded-sm bg-slate-800 px-2 py-1 text-xs">
              <option value={1}>Last 1 hour</option><option value={24}>Last 24 hours</option><option value={168}>Last 7 days</option><option value={720}>Last 30 days</option><option value={2160}>Last 3 months</option><option value={4320}>Last 6 months</option><option value={8760}>Last 1 year</option>
            </select></div>
          {!metric && <p className="text-sm italic text-slate-400">Choose a live metric to view its history.</p>}
          {historyQuery.isLoading && metric && <p className="text-sm text-slate-400">Loading history…</p>}
          {historyQuery.isError && <p className="text-sm text-red-400">Unable to load the selected history range.</p>}
          {historyQuery.data?.length === 0 && <p className="text-sm italic text-slate-400">No readings in this range.</p>}
          {historyQuery.data && <TelemetryTrend points={historyQuery.data} />}
          <div className="max-h-64 space-y-1 overflow-auto text-xs">{historyQuery.data?.map((point) => <div key={point.id} className="flex justify-between rounded-sm bg-slate-800/50 px-2 py-1"><span>{new Date(point.occurred_at).toLocaleString()}</span><span>{point.resolution === "daily" ? `${point.presentation_value} avg (${point.presentation_minimum_value}–${point.presentation_maximum_value}, n=${point.sample_count})` : point.presentation_value} {point.presentation_unit} <span className="text-slate-400">{point.resolution ?? "raw"}</span></span></div>)}</div>
        </div>
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-4"><h2 className="mb-3 text-sm font-semibold text-slate-300">Alarm history</h2>
          {alarmHistoryQuery.isLoading && <p className="text-sm text-slate-400">Loading alarms…</p>}
          {alarmHistoryQuery.data?.items.length === 0 && <p className="text-sm italic text-slate-400">No alarms for this equipment.</p>}
          <div className="space-y-2">{alarmHistoryQuery.data?.items.map((alarm) => <div key={alarm.id} className="rounded-sm bg-slate-800/50 p-2 text-xs"><div className="flex justify-between"><span className={alarm.status === "ACTIVE" ? "text-red-400" : alarm.status === "ACKNOWLEDGED" ? "text-yellow-400" : "text-green-400"}>{alarm.status}</span><span>{alarm.presentation_value}{alarm.presentation_unit === "%" ? "%" : ` ${alarm.presentation_unit ?? ""}`}</span></div><p className="text-slate-400">Occurred {new Date(alarm.opened_at).toLocaleString()}</p>{alarm.acknowledged_at && <p className="text-slate-400">Acknowledged {new Date(alarm.acknowledged_at).toLocaleString()}</p>}{alarm.cleared_at && <p className="text-slate-400">Cleared {new Date(alarm.cleared_at).toLocaleString()}</p>}{alarm.status === "ACTIVE" && <button onClick={() => acknowledgeMutation.mutate(alarm.id)} disabled={acknowledgeMutation.isPending} className="mt-2 rounded-sm bg-yellow-800 px-2 py-1 text-xs text-yellow-100">Acknowledge</button>}</div>)}</div>
        </div>
      </div>
      <ImpactAnalysisModal target={impactTarget} onClose={() => setImpactTarget(null)} />
    </div>
  );
}

function ConnectPortForm({
  isPending,
  error,
  onCancel,
  onSubmit,
}: {
  isPending: boolean;
  error: string | null;
  onCancel: () => void;
  onSubmit: (vars: { targetPortId?: string; targetPowerNodeId?: string; cableId?: string; status: PortConnectionStatus }) => void;
}) {
  const [targetKind, setTargetKind] = useState<"port" | "power_node">("port");
  const [targetId, setTargetId] = useState("");
  const [cableId, setCableId] = useState("");
  const [status, setStatus] = useState<PortConnectionStatus>("active");

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (!targetId.trim()) return;
    onSubmit({
      targetPortId: targetKind === "port" ? targetId.trim() : undefined,
      targetPowerNodeId: targetKind === "power_node" ? targetId.trim() : undefined,
      cableId: cableId.trim() || undefined,
      status,
    });
  }

  return (
    <form onSubmit={handleSubmit} className="mt-2 space-y-1.5 rounded-sm border border-slate-700 bg-slate-950 p-2">
      <div className="flex gap-2">
        <select aria-label="Connection target type" value={targetKind} onChange={(e) => setTargetKind(e.target.value as "port" | "power_node")} className="rounded-sm bg-slate-800 px-2 py-1 text-xs">
          <option value="port">Patch panel / switch port ID</option>
          <option value="power_node">PDU outlet (power node) ID</option>
        </select>
        <select aria-label="Connection status" value={status} onChange={(e) => setStatus(e.target.value as PortConnectionStatus)} className="rounded-sm bg-slate-800 px-2 py-1 text-xs">
          {PORT_CONNECTION_STATUSES.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      </div>
      <input
        aria-label="Connection target ID"
        value={targetId}
        onChange={(e) => setTargetId(e.target.value)}
        placeholder={targetKind === "port" ? "Target EquipmentPort ID" : "Target PowerNode ID"}
        className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
      />
      <input
        aria-label="Cable ID"
        value={cableId}
        onChange={(e) => setCableId(e.target.value)}
        placeholder="Cable ID (optional)"
        className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
      />
      <div className="flex gap-2">
        <button type="submit" disabled={isPending} className="rounded-sm bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50">
          {isPending ? "Connecting…" : "Save"}
        </button>
        <button type="button" onClick={onCancel} className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700">
          Cancel
        </button>
      </div>
      {error && <p role="alert" className="text-xs text-red-400">{error}</p>}
    </form>
  );
}
