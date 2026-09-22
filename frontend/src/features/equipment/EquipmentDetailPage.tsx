import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { getEquipment, moveEquipment, retireEquipment } from "@/features/equipment/api";
import { createEquipmentFeed, getEquipmentPowerSummary } from "@/features/power/api";
import { getEquipmentNetworkContext } from "@/features/network/api";
import { listRacks, listRooms } from "@/features/racks/api";
import { acknowledgeAlarm, getAlarmHistory, getLatestTelemetry, getTelemetryHistory } from "@/features/telemetry/api";
import { TelemetryTrend } from "@/features/telemetry/TelemetryTrend";
import { ApiError } from "@/lib/apiClient";
import { PLACEMENT_TYPES, PlacementType, SIDES, Side } from "@/types";
import { PageHeader, StatusBadge } from "@/components/ui/ProductUi";

const REDUNDANCY_LABELS: Record<string, { text: string; color: string }> = {
  dual_feed_healthy: { text: "Dual-feed (A+B), healthy", color: "bg-green-900 text-green-200" },
  single_feed: { text: "Single feed (no redundancy)", color: "bg-slate-700 text-slate-300" },
  degraded: { text: "Redundancy degraded", color: "bg-yellow-800 text-yellow-100" },
  no_power_modeled: { text: "No power modeled", color: "bg-slate-800 text-slate-500" },
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
  const [historyRange, setHistoryRange] = useState("24");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");

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
  const networkContextQuery = useQuery({ queryKey: ["network", "equipment", equipmentId], queryFn: () => getEquipmentNetworkContext(equipmentId!), enabled: !!equipmentId, retry: false });
  const latestTelemetryQuery = useQuery({
    queryKey: ["telemetry", "latest", equipmentId], queryFn: () => getLatestTelemetry(equipmentId!), enabled: !!equipmentId,
  });
  const alarmHistoryQuery = useQuery({
    queryKey: ["alarms", "equipment", equipmentId], queryFn: () => getAlarmHistory(equipmentId!), enabled: !!equipmentId,
  });
  const metric = selectedMetric ?? latestTelemetryQuery.data?.[0]?.metric ?? null;
  const historyWindow = useMemo(() => {
    if (historyRange !== "custom") { const hours = Number(historyRange); return { start: new Date(Date.now() - hours * 3_600_000), end: new Date(), valid: true }; }
    const start = new Date(customStart); const end = new Date(customEnd);
    return { start, end, valid: Boolean(customStart && customEnd && !Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime()) && start < end) };
  }, [customEnd, customStart, historyRange]);
  const historyQuery = useQuery({
    queryKey: ["telemetry", "history", equipmentId, metric, historyRange, customStart, customEnd],
    queryFn: () => getTelemetryHistory(equipmentId!, metric!, historyWindow.start, historyWindow.end),
    enabled: !!equipmentId && !!metric && historyWindow.valid,
  });
  const acknowledgeMutation = useMutation({
    mutationFn: acknowledgeAlarm,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["alarms", "equipment", equipmentId] }),
  });

  const addFeedMutation = useMutation({
    mutationFn: (label: string) => createEquipmentFeed(equipmentId!, label),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["power", "equipment-summary", equipmentId] }),
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
    <div className="page">
      <Link to="/equipment" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Equipment
      </Link>
      <PageHeader eyebrow={equipment.asset_tag} title={equipment.hostname ?? equipment.asset_tag} description="Placement, modeled power context, current readings, and retained history for this equipment." actions={<StatusBadge label={equipment.lifecycle_status} tone={equipment.lifecycle_status === "active" ? "healthy" : "neutral"} />} />

      <div className="grid gap-6 lg:grid-cols-[1.1fr_.9fr]">
        <div className="surface p-5">
          <div className="mb-4 flex items-center justify-between"><h2 className="text-base font-semibold text-slate-100">Placement</h2>{equipment.placement?.rack_id && <Link to={`/racks/${equipment.placement.rack_id}`} className="text-sm font-medium text-indigo-300 hover:text-indigo-200">Open rack →</Link>}</div>
          {equipment.placement ? (
            <dl className="space-y-1 text-sm">
              <div className="flex justify-between">
                <dt className="text-slate-500">Type</dt>
                <dd>{equipment.placement.placement_type}</dd>
              </div>
              <div className="flex justify-between">
                <dt className="text-slate-500">Room</dt>
                <dd>{currentRoom?.name ?? equipment.placement.room_id}</dd>
              </div>
              {equipment.placement.placement_type === "rack_mounted" && (
                <>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">Rack</dt>
                    <dd>{currentRack?.name ?? equipment.placement.rack_id}</dd>
                  </div>
                  <div className="flex justify-between">
                    <dt className="text-slate-500">U-range</dt>
                    <dd>
                      U{equipment.placement.u_start}–{equipment.placement.u_end} ({equipment.placement.side})
                    </dd>
                  </div>
                </>
              )}
            </dl>
          ) : (
            <p className="text-sm italic text-slate-500">Not currently placed.</p>
          )}
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => setShowMoveForm((v) => !v)}
              className="rounded bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700"
            >
              {equipment.placement ? "Move" : "Place"}
            </button>
            {equipment.placement && (
              <button
                onClick={() => retireMutation.mutate()}
                disabled={retireMutation.isPending}
                className="rounded bg-red-900/50 px-3 py-1.5 text-sm text-red-200 hover:bg-red-900 disabled:opacity-50"
              >
                Retire placement
              </button>
            )}
          </div>

          {showMoveForm && (
            <form onSubmit={handleMoveSubmit} className="mt-4 space-y-2 border-t border-slate-800 pt-4">
              <select
                value={placementType}
                onChange={(e) => setPlacementType(e.target.value as PlacementType)}
                className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
              >
                {PLACEMENT_TYPES.map((pt) => (
                  <option key={pt} value={pt}>
                    {pt}
                  </option>
                ))}
              </select>
              <select
                value={roomId}
                onChange={(e) => setRoomId(e.target.value)}
                className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
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
                    value={rackId}
                    onChange={(e) => setRackId(e.target.value)}
                    className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
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
                      type="number"
                      min={1}
                      value={uStart}
                      onChange={(e) => setUStart(e.target.value)}
                      placeholder="U start"
                      className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                    />
                    <input
                      type="number"
                      min={2}
                      value={uEnd}
                      onChange={(e) => setUEnd(e.target.value)}
                      placeholder="U end (exclusive)"
                      className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
                    />
                    <select
                      value={side}
                      onChange={(e) => setSide(e.target.value as Side)}
                      className="w-1/3 rounded border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100"
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
                className="rounded bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                Confirm
              </button>
              {moveMutation.isError && (
                <p className="text-sm text-red-400">
                  {moveMutation.error instanceof ApiError && moveMutation.error.status === 409
                    ? "That U-range conflicts with existing equipment, or someone else moved this item — reload and try again."
                    : (moveMutation.error as Error).message}
                </p>
              )}
            </form>
          )}
        </div>

        <div className="surface p-5">
          <h2 className="mb-4 text-base font-semibold text-slate-100">Details</h2>
          <dl className="space-y-1 text-sm">
            <div className="flex justify-between">
              <dt className="text-slate-500">Owner</dt>
              <dd>{equipment.owner ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Service</dt>
              <dd>{equipment.service ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Environment</dt>
              <dd>{equipment.environment ?? "—"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Version</dt>
              <dd>{equipment.version}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-slate-500">Created</dt>
              <dd>{new Date(equipment.created_at).toLocaleString()}</dd>
            </div>
          </dl>
        </div>
      </div>

      <div className="surface p-5">
        <div className="mb-3 flex items-center justify-between">
          <div><h2 className="text-base font-semibold text-slate-100">Power</h2><p className="mt-1 text-sm text-slate-500">Modeled feed and path context</p></div>
          <div className="flex gap-2">
            <button
              onClick={() => addFeedMutation.mutate("Feed A")}
              disabled={addFeedMutation.isPending}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
            >
              + Feed A
            </button>
            <button
              onClick={() => addFeedMutation.mutate("Feed B")}
              disabled={addFeedMutation.isPending}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700 disabled:opacity-50"
            >
              + Feed B
            </button>
            <Link to="/power" className="rounded bg-slate-800 px-2 py-1 text-xs text-blue-400 hover:bg-slate-700">
              Open topology →
            </Link>
          </div>
        </div>
        {powerSummaryQuery.data && (
          <>
            {(() => {
              const r = REDUNDANCY_LABELS[powerSummaryQuery.data.redundancy_classification];
              return <div className="mt-5 grid gap-3 sm:grid-cols-3"><div className={`power-kpi ${r.color}`}><span>Redundancy</span><strong>{r.text}</strong></div><div className="power-kpi"><span>Effective demand</span><strong>{powerSummaryQuery.data.effective_demand_kw === null ? "Unknown" : `${powerSummaryQuery.data.effective_demand_kw.toFixed(1)} kW`}</strong></div><div className="power-kpi"><span>Data quality</span><strong className="capitalize">{powerSummaryQuery.data.data_quality.replace(/_/g, " ")}</strong></div></div>;
            })()}
            <div className="mt-4 space-y-2">
              {powerSummaryQuery.data.feed_nodes.map((feed) => (
                <div key={feed.power_node_id} className="grid items-center gap-2 rounded-lg border border-slate-800 bg-slate-950/35 px-4 py-3 text-sm sm:grid-cols-[1fr_auto_auto]">
                  <span className="font-medium text-slate-200">{feed.feed_label ?? "Unlabeled feed"}</span>
                  <span className={feed.has_upstream_path ? "font-medium text-emerald-300" : "font-medium text-rose-300"}>
                    {feed.has_upstream_path ? "path OK" : "no upstream path"}
                  </span>
                  <span className="text-slate-400">{feed.effective_capacity_kw === null ? "capacity unknown" : `${feed.effective_capacity_kw.toFixed(1)} kW`}</span>
                </div>
              ))}
              {powerSummaryQuery.data.feed_nodes.length === 0 && (
                <p className="text-xs italic text-slate-500">No power feeds modeled for this equipment yet.</p>
              )}
            </div>
          </>
        )}
      </div>

      <div className="surface p-5">
        <div className="mb-3 flex items-center justify-between"><div><h2 className="text-base font-semibold">Network traceability</h2><p className="mt-1 text-sm text-slate-500">Modeled physical path; not a live reachability test.</p></div><Link to="/network" className="action-secondary">Open topology →</Link></div>
        {networkContextQuery.isLoading && <p className="text-sm text-slate-500">Resolving modeled path…</p>}
        {networkContextQuery.isError && <p className="text-sm text-slate-500">No network endpoint is modeled for this equipment.</p>}
        {networkContextQuery.data && <><StatusBadge label={networkContextQuery.data.state}/><p className="mt-2 text-xs text-slate-500">{networkContextQuery.data.statement}</p><ol className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-4">{networkContextQuery.data.hops.map((hop,index)=><li key={`${hop.device_id}-${index}`} className="surface-muted p-3 text-sm"><b>{hop.device}</b><p className="text-xs text-slate-500">{hop.interface??"Interface unavailable"} · VLAN {hop.vlan??"Unknown"}</p></li>)}</ol></>}
        <div className="mt-4 flex flex-wrap gap-2">{equipment.placement?.rack_id&&<Link className="action-secondary" to={`/racks/${equipment.placement.rack_id}`}>Rack</Link>}<Link className="action-secondary" to="/floor-plans">Floor plan</Link><Link className="action-secondary" to="/power">Power</Link><Link className="action-secondary" to="/network">Network</Link></div>
      </div>

      <div className="surface p-5">
        <div className="mb-4"><h2 className="text-base font-semibold text-slate-100">Live metrics</h2><p className="mt-1 text-sm text-slate-500">Latest authoritative readings; stale state is based on the source polling interval.</p></div>
        {latestTelemetryQuery.isLoading && <p className="text-sm text-slate-400">Loading telemetry…</p>}
        {latestTelemetryQuery.isError && <p className="text-sm text-red-400">Telemetry is currently unavailable.</p>}
        {latestTelemetryQuery.data?.length === 0 && <p className="text-sm italic text-slate-500">No telemetry is associated with this equipment.</p>}
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {latestTelemetryQuery.data?.map((reading) => {
            const ageMs = Date.now() - new Date(reading.occurred_at).getTime();
            // Two configured acquisition cycles is the MVP stale threshold.  The
            // backend supplies the integration-specific cadence in the same query.
            const stale = reading.expected_poll_interval_seconds != null && ageMs > reading.expected_poll_interval_seconds * 2_000;
            return <button key={reading.id} onClick={() => setSelectedMetric(reading.metric)} className="surface-muted p-4 text-left transition hover:border-indigo-400/40 hover:bg-slate-800/70">
              <p className="text-xs uppercase tracking-wide text-slate-500">{reading.metric}</p>
              <p className="mt-1 text-xl font-semibold">{reading.value} <span className="text-sm text-slate-400">{reading.unit}</span></p>
              <p className={stale ? "mt-1 text-xs text-yellow-400" : "mt-1 text-xs text-green-400"}>{stale ? "Stale" : "Current"} · occurred {new Date(reading.occurred_at).toLocaleString()}</p>
              {reading.received_at !== reading.occurred_at && <p className="mt-1 text-xs text-slate-500">received {new Date(reading.received_at).toLocaleString()}</p>}
            </button>;
          })}
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="surface p-5">
          <div className="mb-4 flex items-center justify-between"><h2 className="text-base font-semibold text-slate-100">Metric history</h2>
            <select value={historyRange} onChange={(e) => setHistoryRange(e.target.value)} className="field w-auto !py-1 text-xs">
              <option value="1">Last 1 hour</option><option value="24">Last 24 hours</option><option value="168">Last 7 days</option><option value="720">Last 30 days</option><option value="2160">Last 3 months</option><option value="4320">Last 6 months</option><option value="8760">Last 1 year</option><option value="custom">Custom range</option>
            </select></div>
          {historyRange === "custom" && <div className="mb-3 grid gap-2 sm:grid-cols-2"><label className="text-xs text-slate-400">Start<input aria-label="History start" type="datetime-local" className="field mt-1" value={customStart} onChange={(event) => setCustomStart(event.target.value)} /></label><label className="text-xs text-slate-400">End<input aria-label="History end" type="datetime-local" className="field mt-1" value={customEnd} onChange={(event) => setCustomEnd(event.target.value)} /></label>{!historyWindow.valid && <p className="sm:col-span-2 text-xs text-amber-200">Select a start time before the end time to load custom history.</p>}</div>}
          {!metric && <p className="text-sm italic text-slate-500">Choose a live metric to view its history.</p>}
          {historyQuery.isLoading && metric && <p className="text-sm text-slate-400">Loading history…</p>}
          {historyQuery.isError && <p className="text-sm text-red-400">Unable to load the selected history range.</p>}
          {historyQuery.data?.length === 0 && <p className="text-sm italic text-slate-500">No readings in this range.</p>}
          {historyQuery.data && <TelemetryTrend points={historyQuery.data} />}
          <div className="max-h-64 space-y-1 overflow-auto text-xs">{historyQuery.data?.map((point) => <div key={point.id} className="flex justify-between rounded bg-slate-800/50 px-2 py-1"><span>{new Date(point.occurred_at).toLocaleString()}</span><span>{point.resolution === "daily" ? `${point.value} avg (${point.minimum_value}–${point.maximum_value}, n=${point.sample_count})` : point.value} {point.unit} <span className="text-slate-500">{point.resolution ?? "raw"}</span></span></div>)}</div>
        </div>
        <div className="surface p-5"><h2 className="mb-4 text-base font-semibold text-slate-100">Alarm history</h2>
          {alarmHistoryQuery.isLoading && <p className="text-sm text-slate-400">Loading alarms…</p>}
          {alarmHistoryQuery.data?.items.length === 0 && <p className="text-sm italic text-slate-500">No alarms for this equipment.</p>}
          <div className="space-y-2">{alarmHistoryQuery.data?.items.map((alarm) => <div key={alarm.id} className="rounded bg-slate-800/50 p-2 text-xs"><div className="flex justify-between"><span className={alarm.status === "ACTIVE" ? "text-red-400" : alarm.status === "ACKNOWLEDGED" ? "text-yellow-400" : "text-green-400"}>{alarm.status}</span><span>{alarm.last_value}</span></div><p className="text-slate-400">Occurred {new Date(alarm.opened_at).toLocaleString()}</p>{alarm.acknowledged_at && <p className="text-slate-500">Acknowledged {new Date(alarm.acknowledged_at).toLocaleString()}</p>}{alarm.cleared_at && <p className="text-slate-500">Cleared {new Date(alarm.cleared_at).toLocaleString()}</p>}{alarm.status === "ACTIVE" && <button onClick={() => acknowledgeMutation.mutate(alarm.id)} disabled={acknowledgeMutation.isPending} className="mt-2 rounded bg-yellow-800 px-2 py-1 text-xs text-yellow-100">Acknowledge</button>}</div>)}</div>
        </div>
      </div>
    </div>
  );
}
