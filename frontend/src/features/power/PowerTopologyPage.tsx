import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import {
  createConnection,
  createGenerator,
  createPdu,
  createPowerPanel,
  createUps,
  createUtilityIntake,
  getDownstream,
  getNodeCapacity,
  getUpstream,
  listPowerNodes,
} from "@/features/power/api";
import { ApiError } from "@/lib/apiClient";

const NODE_TYPE_COLORS: Record<string, string> = {
  utility_intake: "bg-purple-800 text-purple-100",
  generator: "bg-orange-800 text-orange-100",
  ups: "bg-blue-800 text-blue-100",
  power_panel: "bg-teal-800 text-teal-100",
  power_circuit: "bg-teal-900 text-teal-200",
  pdu: "bg-green-800 text-green-100",
  pdu_outlet: "bg-green-900 text-green-200",
  equipment_power_input: "bg-slate-700 text-slate-100",
};

function NodeBadge({ nodeType }: { nodeType: string }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs ${NODE_TYPE_COLORS[nodeType] ?? "bg-slate-700 text-slate-200"}`}>
      {nodeType.replace(/_/g, " ")}
    </span>
  );
}

function DataQualityBadge({ quality }: { quality: string }) {
  const colors: Record<string, string> = {
    known: "bg-green-900 text-green-200",
    unknown: "bg-slate-700 text-slate-300",
    not_applicable: "bg-slate-800 text-slate-500",
  };
  return <span className={`rounded px-1.5 py-0.5 text-[10px] ${colors[quality] ?? "bg-slate-700"}`}>{quality}</span>;
}

function fmtKw(value: number | null): string {
  return value === null ? "—" : `${value.toFixed(1)} kW`;
}

export function PowerTopologyPage() {
  const queryClient = useQueryClient();
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [filterType, setFilterType] = useState<string>("");
  const [showCreateForm, setShowCreateForm] = useState(false);
  const [showConnectForm, setShowConnectForm] = useState(false);
  const [createKind, setCreateKind] = useState<"utility" | "pdu" | "ups" | "generator" | "power_panel">("pdu");
  const [assetTag, setAssetTag] = useState("");
  const [name, setName] = useState("");
  const [connSource, setConnSource] = useState("");
  const [connTarget, setConnTarget] = useState("");
  const [connFeedLabel, setConnFeedLabel] = useState("single");

  const nodesQuery = useQuery({
    queryKey: ["power", "nodes", filterType],
    queryFn: () => listPowerNodes(filterType || undefined),
  });

  const upstreamQuery = useQuery({
    queryKey: ["power", "upstream", selectedNodeId],
    queryFn: () => getUpstream(selectedNodeId!),
    enabled: !!selectedNodeId,
  });
  const downstreamQuery = useQuery({
    queryKey: ["power", "downstream", selectedNodeId],
    queryFn: () => getDownstream(selectedNodeId!),
    enabled: !!selectedNodeId,
  });
  const capacityQuery = useQuery({
    queryKey: ["power", "capacity", selectedNodeId],
    queryFn: () => getNodeCapacity(selectedNodeId!),
    enabled: !!selectedNodeId,
  });

  const createMutation = useMutation({
    mutationFn: () => {
      if (createKind === "utility") return createUtilityIntake(name);
      const body = { asset_tag: assetTag, name };
      if (createKind === "pdu") return createPdu(body);
      if (createKind === "ups") return createUps({ ...body, room_id: (document.getElementById("room_id_input") as HTMLInputElement)?.value });
      if (createKind === "generator")
        return createGenerator({ ...body, site_id: (document.getElementById("site_id_input") as HTMLInputElement)?.value });
      return createPowerPanel({ ...body, room_id: (document.getElementById("room_id_input") as HTMLInputElement)?.value });
    },
    onSuccess: () => {
      setShowCreateForm(false);
      setAssetTag("");
      setName("");
      queryClient.invalidateQueries({ queryKey: ["power", "nodes"] });
    },
  });

  const connectMutation = useMutation({
    mutationFn: () => createConnection({ source_node_id: connSource, target_node_id: connTarget, feed_label: connFeedLabel }),
    onSuccess: () => {
      setShowConnectForm(false);
      queryClient.invalidateQueries({ queryKey: ["power"] });
    },
  });

  const selectedNode = nodesQuery.data?.items.find((n) => n.id === selectedNodeId);
  const capacity = capacityQuery.data;

  return (
    <div>
      <h1 className="mb-1 text-lg font-semibold">Power Topology</h1>
      <p className="mb-6 max-w-2xl text-sm text-slate-400">
        Select a power node to inspect its upstream source chain, downstream loads, and capacity. No live telemetry
        yet — capacity figures come from rated/configured values and topology roll-up only (Phase 3).
      </p>

      <div className="grid grid-cols-3 gap-6">
        {/* Node list */}
        <div className="rounded border border-slate-800 bg-slate-900 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-300">Power Nodes</h2>
            <button
              onClick={() => setShowCreateForm((v) => !v)}
              className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700"
            >
              + New
            </button>
          </div>
          <select
            value={filterType}
            onChange={(e) => setFilterType(e.target.value)}
            className="mb-3 w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          >
            <option value="">All types</option>
            {Object.keys(NODE_TYPE_COLORS).map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>

          {showCreateForm && (
            <form
              onSubmit={(e: FormEvent) => {
                e.preventDefault();
                createMutation.mutate();
              }}
              className="mb-3 space-y-2 rounded border border-slate-700 bg-slate-800/50 p-2"
            >
              <select
                value={createKind}
                onChange={(e) => setCreateKind(e.target.value as typeof createKind)}
                className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
              >
                <option value="utility">Utility intake</option>
                <option value="pdu">PDU</option>
                <option value="ups">UPS</option>
                <option value="generator">Generator</option>
                <option value="power_panel">Power panel</option>
              </select>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Label / name"
                required
                className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
              />
              {createKind !== "utility" && (
                <input
                  value={assetTag}
                  onChange={(e) => setAssetTag(e.target.value)}
                  placeholder="Asset tag"
                  required
                  className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
                />
              )}
              {(createKind === "ups" || createKind === "power_panel") && (
                <input id="room_id_input" placeholder="Room ID" required className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100" />
              )}
              {createKind === "generator" && (
                <input id="site_id_input" placeholder="Site ID" required className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100" />
              )}
              <button
                type="submit"
                disabled={createMutation.isPending}
                className="w-full rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                Create
              </button>
              {createMutation.isError && (
                <p className="text-xs text-red-400">{(createMutation.error as Error).message}</p>
              )}
            </form>
          )}

          <div className="max-h-[420px] space-y-1 overflow-y-auto">
            {nodesQuery.data?.items.map((node) => (
              <button
                key={node.id}
                onClick={() => setSelectedNodeId(node.id)}
                className={`flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-xs hover:bg-slate-800 ${
                  selectedNodeId === node.id ? "bg-slate-800" : ""
                }`}
              >
                <span className="truncate">{node.label}</span>
                <NodeBadge nodeType={node.node_type} />
              </button>
            ))}
            {nodesQuery.data?.items.length === 0 && <p className="text-xs italic text-slate-500">No power nodes yet.</p>}
          </div>
        </div>

        {/* Selected node detail + topology */}
        <div className="col-span-2 space-y-4">
          {!selectedNode && (
            <div className="rounded border border-slate-800 bg-slate-900 p-4 text-sm text-slate-500">
              Select a power node from the list to inspect it.
            </div>
          )}

          {selectedNode && (
            <>
              <div className="rounded border border-slate-800 bg-slate-900 p-4">
                <div className="mb-3 flex items-center justify-between">
                  <div>
                    <h2 className="text-sm font-semibold text-slate-200">{selectedNode.label}</h2>
                    <NodeBadge nodeType={selectedNode.node_type} />
                  </div>
                  <button
                    onClick={() => setShowConnectForm((v) => !v)}
                    className="rounded bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700"
                  >
                    + Connect
                  </button>
                </div>

                {showConnectForm && (
                  <form
                    onSubmit={(e: FormEvent) => {
                      e.preventDefault();
                      connectMutation.mutate();
                    }}
                    className="mb-3 space-y-2 rounded border border-slate-700 bg-slate-800/50 p-2"
                  >
                    <p className="text-xs text-slate-400">Source (upstream) → this node is a common pattern; fill both IDs explicitly:</p>
                    <input
                      value={connSource}
                      onChange={(e) => setConnSource(e.target.value)}
                      placeholder="Source node ID"
                      required
                      className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
                    />
                    <input
                      value={connTarget}
                      onChange={(e) => setConnTarget(e.target.value)}
                      placeholder="Target node ID"
                      defaultValue={selectedNode.id}
                      required
                      className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
                    />
                    <select
                      value={connFeedLabel}
                      onChange={(e) => setConnFeedLabel(e.target.value)}
                      className="w-full rounded border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
                    >
                      <option value="single">single</option>
                      <option value="A">A</option>
                      <option value="B">B</option>
                    </select>
                    <button
                      type="submit"
                      disabled={connectMutation.isPending}
                      className="w-full rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                    >
                      Create connection
                    </button>
                    {connectMutation.isError && (
                      <p className="text-xs text-red-400">
                        {connectMutation.error instanceof ApiError
                          ? connectMutation.error.detail
                          : (connectMutation.error as Error).message}
                      </p>
                    )}
                  </form>
                )}

                {capacity && (
                  <dl className="grid grid-cols-2 gap-2 text-xs sm:grid-cols-3">
                    <div>
                      <dt className="text-slate-500">Rated</dt>
                      <dd>{fmtKw(capacity.rated_capacity_kw)}</dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Configured</dt>
                      <dd>{fmtKw(capacity.configured_capacity_kw)}</dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Allocated</dt>
                      <dd>{fmtKw(capacity.allocated_kw)}</dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Available</dt>
                      <dd className={capacity.available_kw !== null && capacity.available_kw < 0 ? "text-red-400" : ""}>
                        {fmtKw(capacity.available_kw)}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Utilization</dt>
                      <dd>{capacity.utilization_pct === null ? "—" : `${capacity.utilization_pct.toFixed(1)}%`}</dd>
                    </div>
                    <div>
                      <dt className="text-slate-500">Data quality</dt>
                      <dd>
                        <DataQualityBadge quality={capacity.data_quality} />
                      </dd>
                    </div>
                  </dl>
                )}
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="rounded border border-slate-800 bg-slate-900 p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Upstream (sources)</h3>
                  <div className="space-y-1">
                    {upstreamQuery.data?.length === 0 && (
                      <p className="text-xs italic text-amber-400">No upstream path — this node has no power source.</p>
                    )}
                    {upstreamQuery.data?.map((n) => (
                      <button
                        key={n.node_id}
                        onClick={() => setSelectedNodeId(n.node_id)}
                        className="flex w-full items-center justify-between rounded bg-slate-800/60 px-2 py-1 text-left text-xs hover:bg-slate-800"
                      >
                        <span>{n.label}</span>
                        <NodeBadge nodeType={n.node_type} />
                      </button>
                    ))}
                  </div>
                </div>
                <div className="rounded border border-slate-800 bg-slate-900 p-4">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-400">Downstream (loads)</h3>
                  <div className="space-y-1">
                    {downstreamQuery.data?.length === 0 && <p className="text-xs italic text-slate-500">Nothing connected downstream.</p>}
                    {downstreamQuery.data?.map((n) => (
                      <button
                        key={n.node_id}
                        onClick={() => setSelectedNodeId(n.node_id)}
                        className="flex w-full items-center justify-between rounded bg-slate-800/60 px-2 py-1 text-left text-xs hover:bg-slate-800"
                      >
                        <span>{n.label}</span>
                        <NodeBadge nodeType={n.node_type} />
                      </button>
                    ))}
                  </div>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
