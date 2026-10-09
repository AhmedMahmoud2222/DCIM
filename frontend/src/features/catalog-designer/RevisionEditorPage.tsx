import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { useIsCatalogAdministrator } from "@/features/auth/useAuthorization";
import { GraphicsEditorPage } from "@/features/catalog-designer/GraphicsEditorPage";
import {
  createNetworkPort,
  createMonitoringMetric,
  createPowerSupply,
  deleteDraftRevision,
  deleteMonitoringMetric,
  deleteNetworkPort,
  deletePowerSupply,
  getRevision,
  publishRevision,
  retireRevision,
  updateRetireOverride,
  updateRevision,
  validateRevision,
} from "@/features/catalog-designer/api";
import { ApiError } from "@/lib/apiClient";
import { CatalogModelRevisionDetail, ValidationSummary } from "@/types";

import { DatasheetPanel } from "@/features/catalog-designer/DatasheetPanel";

const LIFECYCLE_COLORS: Record<string, string> = {
  draft: "bg-slate-700 text-slate-200",
  published: "bg-green-700 text-green-100",
  retired: "bg-red-900 text-red-100",
};

export function RevisionEditorPage() {
  const { revisionId } = useParams<{ revisionId: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const isCatalogAdministrator = useIsCatalogAdministrator();

  const revisionQuery = useQuery({
    queryKey: ["catalog", "revisions", revisionId],
    queryFn: () => getRevision(revisionId!),
    enabled: !!revisionId,
  });

  // Every mutation below changes fields that are also cached under the parent model's own
  // query key (ModelDetailPage's revision-history table shows lifecycle_status/published_at/
  // retired_at) — invalidating only the revision-level key would leave that table stale for
  // up to the query client's staleTime after navigating back, and would leave a subsequent
  // mutation on this page racing a stale `revision.version` read from an un-refetched cache,
  // which is exactly the source of an avoidable If-Match 409 this invalidation prevents.
  function invalidate() {
    queryClient.invalidateQueries({ queryKey: ["catalog", "revisions", revisionId] });
    if (revisionQuery.data) {
      queryClient.invalidateQueries({ queryKey: ["catalog", "models", revisionQuery.data.catalog_model_id] });
    }
  }

  const deleteMutation = useMutation({
    mutationFn: () => deleteDraftRevision(revisionId!, revisionQuery.data!.version),
    onSuccess: () => {
      const catalogModelId = revisionQuery.data!.catalog_model_id;
      queryClient.invalidateQueries({ queryKey: ["catalog", "revisions", revisionId] });
      queryClient.invalidateQueries({ queryKey: ["catalog", "models", catalogModelId] });
      navigate(`/admin/catalog/models/${catalogModelId}`);
    },
  });

  if (revisionQuery.isLoading) return <p className="text-sm text-slate-400">Loading…</p>;
  if (revisionQuery.error) return <p className="text-sm text-red-400">{(revisionQuery.error as Error).message}</p>;
  const revision = revisionQuery.data;
  if (!revision) return null;

  const isDraft = revision.lifecycle_status === "draft";
  const canEditDraft = isDraft && isCatalogAdministrator;

  return (
    <div>
      <Link to={`/admin/catalog/models/${revision.catalog_model_id}`} className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Model
      </Link>
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-lg font-semibold">Revision {revision.revision_number}</h1>
        <div className="flex items-center gap-3">
          <span className={`rounded-sm px-2 py-0.5 text-xs ${LIFECYCLE_COLORS[revision.lifecycle_status] ?? "bg-slate-700"}`}>
            {revision.lifecycle_status}
          </span>
          {canEditDraft && (
            <button
              onClick={() => deleteMutation.mutate()}
              disabled={deleteMutation.isPending}
              className="rounded-sm bg-red-900/50 px-3 py-1.5 text-sm text-red-200 hover:bg-red-900 disabled:opacity-50"
            >
              Delete draft
            </button>
          )}
        </div>
      </div>
      {deleteMutation.isError && <p className="mb-4 text-sm text-red-400">{(deleteMutation.error as Error).message}</p>}

      <ScalarFieldsSection revision={revision} readOnly={!canEditDraft} onSaved={invalidate} />

      <DatasheetPanel key={revision.id} revision={revision} readOnly={!canEditDraft} onChanged={invalidate} />

      <TemplateEditors revision={revision} readOnly={!canEditDraft} onChanged={invalidate} />

      <GraphicsEditorPage revision={revision} readOnly={!canEditDraft} onChanged={invalidate} />

      {canEditDraft && <PublishPanel revision={revision} onPublished={invalidate} />}
      {revision.lifecycle_status === "published" && isCatalogAdministrator && <RetirePanel revision={revision} onRetired={invalidate} />}
      {revision.lifecycle_status === "retired" && isCatalogAdministrator && (
        <RetireOverridePanel revision={revision} onChanged={invalidate} />
      )}
    </div>
  );
}

// -------------------------------------------------------------- Scalar fields (physical + electrical)

function ScalarFieldsSection({
  revision,
  readOnly,
  onSaved,
}: {
  revision: CatalogModelRevisionDetail;
  readOnly: boolean;
  onSaved: () => void;
}) {
  const [dimensionUnit, setDimensionUnit] = useState(revision.dimension_unit ?? "mm");
  const [widthValue, setWidthValue] = useState(revision.width_value?.toString() ?? "");
  const [heightValue, setHeightValue] = useState(revision.height_value?.toString() ?? "");
  const [depthValue, setDepthValue] = useState(revision.depth_value?.toString() ?? "");
  const [rackUnitHeight, setRackUnitHeight] = useState(revision.rack_unit_height?.toString() ?? "");
  const [weightUnit, setWeightUnit] = useState(revision.weight_unit ?? "kg");
  const [weightValue, setWeightValue] = useState(revision.weight_value?.toString() ?? "");
  const [ratedPowerW, setRatedPowerW] = useState(revision.rated_power_w?.toString() ?? "");
  const [typicalPowerW, setTypicalPowerW] = useState(revision.typical_power_w?.toString() ?? "");
  const [maxPowerW, setMaxPowerW] = useState(revision.max_power_w?.toString() ?? "");
  const [powerRedundancyMode, setPowerRedundancyMode] = useState(revision.power_redundancy_mode ?? "");

  useEffect(() => {
    setDimensionUnit(revision.dimension_unit ?? "mm");
    setWidthValue(revision.width_value?.toString() ?? "");
    setHeightValue(revision.height_value?.toString() ?? "");
    setDepthValue(revision.depth_value?.toString() ?? "");
    setRackUnitHeight(revision.rack_unit_height?.toString() ?? "");
    setWeightUnit(revision.weight_unit ?? "kg");
    setWeightValue(revision.weight_value?.toString() ?? "");
    setRatedPowerW(revision.rated_power_w?.toString() ?? "");
    setTypicalPowerW(revision.typical_power_w?.toString() ?? "");
    setMaxPowerW(revision.max_power_w?.toString() ?? "");
    setPowerRedundancyMode(revision.power_redundancy_mode ?? "");
  }, [revision]);

  const saveMutation = useMutation({
    mutationFn: () =>
      updateRevision(
        revision.id,
        {
          dimension_unit: dimensionUnit || undefined,
          width_value: widthValue ? Number(widthValue) : undefined,
          height_value: heightValue ? Number(heightValue) : undefined,
          depth_value: depthValue ? Number(depthValue) : undefined,
          rack_unit_height: rackUnitHeight ? Number(rackUnitHeight) : undefined,
          weight_unit: weightUnit || undefined,
          weight_value: weightValue ? Number(weightValue) : undefined,
          rated_power_w: ratedPowerW ? Number(ratedPowerW) : undefined,
          typical_power_w: typicalPowerW ? Number(typicalPowerW) : undefined,
          max_power_w: maxPowerW ? Number(maxPowerW) : undefined,
          power_redundancy_mode: powerRedundancyMode || undefined,
        },
        revision.version,
      ),
    onSuccess: onSaved,
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    saveMutation.mutate();
  }

  const inputClass =
    "w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden disabled:opacity-60";

  return (
    <form onSubmit={handleSubmit} className="mb-6 rounded-sm border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-semibold text-slate-300">Physical &amp; Electrical</h2>
      <div className="grid grid-cols-3 gap-3">
        <label className="text-xs text-slate-400">
          Dimension unit
          <select value={dimensionUnit} onChange={(e) => setDimensionUnit(e.target.value)} disabled={readOnly} className={`mt-1 ${inputClass}`}>
            <option value="mm">mm</option>
            <option value="in">in</option>
          </select>
        </label>
        <label className="text-xs text-slate-400">
          Width
          <input type="number" value={widthValue} onChange={(e) => setWidthValue(e.target.value)} disabled={readOnly} className={`mt-1 ${inputClass}`} />
        </label>
        <label className="text-xs text-slate-400">
          Height
          <input type="number" value={heightValue} onChange={(e) => setHeightValue(e.target.value)} disabled={readOnly} className={`mt-1 ${inputClass}`} />
        </label>
        <label className="text-xs text-slate-400">
          Depth
          <input type="number" value={depthValue} onChange={(e) => setDepthValue(e.target.value)} disabled={readOnly} className={`mt-1 ${inputClass}`} />
        </label>
        <label className="text-xs text-slate-400">
          Rack unit height (U)
          <input
            type="number"
            value={rackUnitHeight}
            onChange={(e) => setRackUnitHeight(e.target.value)}
            disabled={readOnly}
            className={`mt-1 ${inputClass}`}
          />
        </label>
        <label className="text-xs text-slate-400">
          Weight unit
          <select value={weightUnit} onChange={(e) => setWeightUnit(e.target.value)} disabled={readOnly} className={`mt-1 ${inputClass}`}>
            <option value="kg">kg</option>
            <option value="lb">lb</option>
          </select>
        </label>
        <label className="text-xs text-slate-400">
          Weight
          <input type="number" value={weightValue} onChange={(e) => setWeightValue(e.target.value)} disabled={readOnly} className={`mt-1 ${inputClass}`} />
        </label>
        <label className="text-xs text-slate-400">
          Rated power (W)
          <input type="number" value={ratedPowerW} onChange={(e) => setRatedPowerW(e.target.value)} disabled={readOnly} className={`mt-1 ${inputClass}`} />
        </label>
        <label className="text-xs text-slate-400">
          Typical power (W)
          <input
            type="number"
            value={typicalPowerW}
            onChange={(e) => setTypicalPowerW(e.target.value)}
            disabled={readOnly}
            className={`mt-1 ${inputClass}`}
          />
        </label>
        <label className="text-xs text-slate-400">
          Max power (W)
          <input type="number" value={maxPowerW} onChange={(e) => setMaxPowerW(e.target.value)} disabled={readOnly} className={`mt-1 ${inputClass}`} />
        </label>
        <label className="text-xs text-slate-400">
          Power redundancy mode
          <input
            value={powerRedundancyMode}
            onChange={(e) => setPowerRedundancyMode(e.target.value)}
            disabled={readOnly}
            placeholder="e.g. N+1"
            className={`mt-1 ${inputClass}`}
          />
        </label>
      </div>
      {!readOnly && (
        <div className="mt-4">
          <button
            type="submit"
            disabled={saveMutation.isPending}
            className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            {saveMutation.isPending ? "Saving…" : "Save"}
          </button>
          {saveMutation.isError && (
            <p className="mt-2 text-sm text-red-400">
              {saveMutation.error instanceof ApiError && saveMutation.error.status === 409
                ? "This revision changed elsewhere — reload and try again."
                : (saveMutation.error as Error).message}
            </p>
          )}
        </div>
      )}
    </form>
  );
}

// ---------------------------------------------------------------------- Template editors

function TemplateEditors({
  revision,
  readOnly,
  onChanged,
}: {
  revision: CatalogModelRevisionDetail;
  readOnly: boolean;
  onChanged: () => void;
}) {
  return (
    <div className="mb-6 grid grid-cols-1 gap-6 lg:grid-cols-3">
      <NetworkPortTemplateEditor revision={revision} readOnly={readOnly} onChanged={onChanged} />
      <PowerSupplyTemplateEditor revision={revision} readOnly={readOnly} onChanged={onChanged} />
      <MonitoringTemplateEditor revision={revision} readOnly={readOnly} onChanged={onChanged} />
    </div>
  );
}

function NetworkPortTemplateEditor({
  revision,
  readOnly,
  onChanged,
}: {
  revision: CatalogModelRevisionDetail;
  readOnly: boolean;
  onChanged: () => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const [stableKey, setStableKey] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [mediaType, setMediaType] = useState("copper");
  const [connectorType, setConnectorType] = useState("RJ45");
  const [side, setSide] = useState("rear");
  const [speeds, setSpeeds] = useState("1000");

  const createMutation = useMutation({
    mutationFn: () =>
      createNetworkPort(
        revision.id,
        {
          stable_key: stableKey.trim(),
          display_name: displayName.trim(),
          media_type: mediaType,
          connector_type: connectorType,
          side,
          supported_speeds_mbps: speeds
            .split(",")
            .map((s) => Number(s.trim()))
            .filter((n) => !Number.isNaN(n)),
        },
        revision.version,
      ),
    onSuccess: () => {
      setStableKey("");
      setDisplayName("");
      setShowForm(false);
      onChanged();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (portId: string) => deleteNetworkPort(revision.id, portId, revision.version),
    onSuccess: onChanged,
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (stableKey.trim() && displayName.trim()) createMutation.mutate();
  }

  return (
    <section className="rounded-sm border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Network Ports</h2>
        {!readOnly && (
          <button onClick={() => setShowForm((v) => !v)} className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700">
            {showForm ? "Cancel" : "Add"}
          </button>
        )}
      </div>
      {showForm && (
        <form onSubmit={handleSubmit} className="mb-3 space-y-2 rounded-sm border border-slate-800 bg-slate-950 p-3">
          <input
            value={stableKey}
            onChange={(e) => setStableKey(e.target.value)}
            placeholder="Stable key (e.g. eth0)"
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <input
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            placeholder="Display name"
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <div className="flex gap-2">
            <input
              value={connectorType}
              onChange={(e) => setConnectorType(e.target.value)}
              placeholder="Connector"
              className="w-1/2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
            />
            <select value={side} onChange={(e) => setSide(e.target.value)} className="w-1/2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100">
              <option value="front">front</option>
              <option value="rear">rear</option>
            </select>
          </div>
          <select value={mediaType} onChange={(e) => setMediaType(e.target.value)} className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100">
            <option value="copper">copper</option>
            <option value="fiber">fiber</option>
            <option value="other">other</option>
          </select>
          <input
            value={speeds}
            onChange={(e) => setSpeeds(e.target.value)}
            placeholder="Speeds (Mbps, comma-separated)"
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <button
            type="submit"
            disabled={createMutation.isPending}
            className="rounded-sm bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            Add port
          </button>
          {createMutation.isError && <p className="text-xs text-red-400">{(createMutation.error as Error).message}</p>}
        </form>
      )}
      <ul className="space-y-1 text-xs">
        {revision.network_ports.map((port) => (
          <li key={port.id} className="flex items-center justify-between rounded-sm bg-slate-800/50 px-2 py-1">
            <span>
              {port.display_name} <span className="text-slate-500">({port.connector_type}, {port.side})</span>
            </span>
            {!readOnly && (
              <button onClick={() => deleteMutation.mutate(port.id)} className="text-red-400 hover:text-red-300">
                Remove
              </button>
            )}
          </li>
        ))}
        {revision.network_ports.length === 0 && <li className="italic text-slate-500">No ports.</li>}
      </ul>
    </section>
  );
}

function PowerSupplyTemplateEditor({
  revision,
  readOnly,
  onChanged,
}: {
  revision: CatalogModelRevisionDetail;
  readOnly: boolean;
  onChanged: () => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const [stableKey, setStableKey] = useState("");
  const [label, setLabel] = useState("");
  const [connectorType, setConnectorType] = useState("C14");
  const [quantity, setQuantity] = useState("1");

  const createMutation = useMutation({
    mutationFn: () =>
      createPowerSupply(
        revision.id,
        { stable_key: stableKey.trim(), label: label.trim(), connector_type: connectorType, quantity: Number(quantity) || 1 },
        revision.version,
      ),
    onSuccess: () => {
      setStableKey("");
      setLabel("");
      setShowForm(false);
      onChanged();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (psuId: string) => deletePowerSupply(revision.id, psuId, revision.version),
    onSuccess: onChanged,
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (stableKey.trim() && label.trim()) createMutation.mutate();
  }

  return (
    <section className="rounded-sm border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Power Supplies</h2>
        {!readOnly && (
          <button onClick={() => setShowForm((v) => !v)} className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700">
            {showForm ? "Cancel" : "Add"}
          </button>
        )}
      </div>
      {showForm && (
        <form onSubmit={handleSubmit} className="mb-3 space-y-2 rounded-sm border border-slate-800 bg-slate-950 p-3">
          <input
            value={stableKey}
            onChange={(e) => setStableKey(e.target.value)}
            placeholder="Stable key (e.g. psu1)"
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="Label"
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <div className="flex gap-2">
            <input
              value={connectorType}
              onChange={(e) => setConnectorType(e.target.value)}
              placeholder="Connector"
              className="w-1/2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
            />
            <input
              type="number"
              value={quantity}
              onChange={(e) => setQuantity(e.target.value)}
              placeholder="Qty"
              className="w-1/2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
            />
          </div>
          <button
            type="submit"
            disabled={createMutation.isPending}
            className="rounded-sm bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            Add power supply
          </button>
          {createMutation.isError && <p className="text-xs text-red-400">{(createMutation.error as Error).message}</p>}
        </form>
      )}
      <ul className="space-y-1 text-xs">
        {revision.power_supplies.map((psu) => (
          <li key={psu.id} className="flex items-center justify-between rounded-sm bg-slate-800/50 px-2 py-1">
            <span>
              {psu.label} <span className="text-slate-500">({psu.connector_type} × {psu.quantity})</span>
            </span>
            {!readOnly && (
              <button onClick={() => deleteMutation.mutate(psu.id)} className="text-red-400 hover:text-red-300">
                Remove
              </button>
            )}
          </li>
        ))}
        {revision.power_supplies.length === 0 && <li className="italic text-slate-500">No power supplies.</li>}
      </ul>
    </section>
  );
}

function MonitoringTemplateEditor({
  revision,
  readOnly,
  onChanged,
}: {
  revision: CatalogModelRevisionDetail;
  readOnly: boolean;
  onChanged: () => void;
}) {
  const [showForm, setShowForm] = useState(false);
  const [stableKey, setStableKey] = useState("");
  const [metricName, setMetricName] = useState("");
  const [protocol, setProtocol] = useState("snmp");
  const [valueType, setValueType] = useState("numeric");
  const [oid, setOid] = useState("");

  const createMutation = useMutation({
    mutationFn: () =>
      createMonitoringMetric(
        revision.id,
        { stable_key: stableKey.trim(), metric_name: metricName.trim(), protocol, value_type: valueType, oid: oid.trim() || undefined },
        revision.version,
      ),
    onSuccess: () => {
      setStableKey("");
      setMetricName("");
      setOid("");
      setShowForm(false);
      onChanged();
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (metricId: string) => deleteMonitoringMetric(revision.id, metricId, revision.version),
    onSuccess: onChanged,
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (stableKey.trim() && metricName.trim()) createMutation.mutate();
  }

  return (
    <section className="rounded-sm border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Monitoring Metrics</h2>
        {!readOnly && (
          <button onClick={() => setShowForm((v) => !v)} className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-200 hover:bg-slate-700">
            {showForm ? "Cancel" : "Add"}
          </button>
        )}
      </div>
      {showForm && (
        <form onSubmit={handleSubmit} className="mb-3 space-y-2 rounded-sm border border-slate-800 bg-slate-950 p-3">
          <input
            value={stableKey}
            onChange={(e) => setStableKey(e.target.value)}
            placeholder="Stable key (e.g. temp_inlet)"
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <input
            value={metricName}
            onChange={(e) => setMetricName(e.target.value)}
            placeholder="Metric name"
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <div className="flex gap-2">
            <select value={protocol} onChange={(e) => setProtocol(e.target.value)} className="w-1/2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100">
              <option value="snmp">snmp</option>
              <option value="redfish">redfish</option>
              <option value="other">other</option>
            </select>
            <select value={valueType} onChange={(e) => setValueType(e.target.value)} className="w-1/2 rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100">
              <option value="numeric">numeric</option>
              <option value="boolean">boolean</option>
              <option value="enum">enum</option>
            </select>
          </div>
          <input
            value={oid}
            onChange={(e) => setOid(e.target.value)}
            placeholder="OID (SNMP only)"
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
          />
          <button
            type="submit"
            disabled={createMutation.isPending}
            className="rounded-sm bg-blue-600 px-3 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            Add metric
          </button>
          {createMutation.isError && <p className="text-xs text-red-400">{(createMutation.error as Error).message}</p>}
        </form>
      )}
      <ul className="space-y-1 text-xs">
        {revision.monitoring_metrics.map((metric) => (
          <li key={metric.id} className="flex items-center justify-between rounded-sm bg-slate-800/50 px-2 py-1">
            <span>
              {metric.metric_name} <span className="text-slate-500">({metric.protocol})</span>
            </span>
            {!readOnly && (
              <button onClick={() => deleteMutation.mutate(metric.id)} className="text-red-400 hover:text-red-300">
                Remove
              </button>
            )}
          </li>
        ))}
        {revision.monitoring_metrics.length === 0 && <li className="italic text-slate-500">No metrics.</li>}
      </ul>
    </section>
  );
}

// ------------------------------------------------------------------------- Validate / publish

function PublishPanel({ revision, onPublished }: { revision: CatalogModelRevisionDetail; onPublished: () => void }) {
  const [summary, setSummary] = useState<ValidationSummary | null>(null);

  const validateMutation = useMutation({
    mutationFn: () => validateRevision(revision.id),
    onSuccess: setSummary,
  });

  const publishMutation = useMutation({
    mutationFn: () => publishRevision(revision.id),
    onSuccess: (result) => {
      if ("valid" in result) {
        setSummary(result);
      } else {
        onPublished();
      }
    },
  });

  return (
    <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Validate &amp; Publish</h2>
        <div className="flex gap-2">
          <button
            onClick={() => validateMutation.mutate()}
            disabled={validateMutation.isPending}
            className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
          >
            Validate
          </button>
          <button
            onClick={() => publishMutation.mutate()}
            disabled={publishMutation.isPending || summary?.valid === false}
            className="rounded-sm bg-green-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-green-600 disabled:opacity-50"
          >
            {publishMutation.isPending ? "Publishing…" : "Publish"}
          </button>
        </div>
      </div>
      {summary && (
        <div className="space-y-2 text-sm">
          <p className={summary.valid ? "text-green-400" : "text-red-400"}>{summary.valid ? "Ready to publish." : "Not ready to publish."}</p>
          {summary.errors.map((issue, i) => (
            <p key={`err-${i}`} className="text-red-400">
              {issue.field}: {issue.message}
            </p>
          ))}
          {summary.warnings.map((issue, i) => (
            <p key={`warn-${i}`} className="text-yellow-400">
              {issue.field}: {issue.message}
            </p>
          ))}
        </div>
      )}
      {publishMutation.isError && <p className="mt-2 text-sm text-red-400">{(publishMutation.error as Error).message}</p>}
    </div>
  );
}

function RetirePanel({ revision, onRetired }: { revision: CatalogModelRevisionDetail; onRetired: () => void }) {
  const [showForm, setShowForm] = useState(false);
  const [reason, setReason] = useState("");
  const [allowInstall, setAllowInstall] = useState(false);

  const retireMutation = useMutation({
    mutationFn: () => retireRevision(revision.id, { reason: reason.trim(), allow_installation_when_retired: allowInstall }),
    onSuccess: onRetired,
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (reason.trim()) retireMutation.mutate();
  }

  return (
    <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-300">Retire</h2>
        <button onClick={() => setShowForm((v) => !v)} className="rounded-sm bg-red-900/50 px-3 py-1.5 text-sm text-red-200 hover:bg-red-900">
          {showForm ? "Cancel" : "Retire this revision"}
        </button>
      </div>
      {showForm && (
        <form onSubmit={handleSubmit} className="space-y-2">
          <textarea
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Reason for retirement (required)"
            rows={2}
            className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
          />
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input type="checkbox" checked={allowInstall} onChange={(e) => setAllowInstall(e.target.checked)} />
            Allow new installations after retirement
          </label>
          <button
            type="submit"
            disabled={retireMutation.isPending}
            className="rounded-sm bg-red-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-600 disabled:opacity-50"
          >
            Confirm retirement
          </button>
          {retireMutation.isError && <p className="text-sm text-red-400">{(retireMutation.error as Error).message}</p>}
        </form>
      )}
    </div>
  );
}

function RetireOverridePanel({ revision, onChanged }: { revision: CatalogModelRevisionDetail; onChanged: () => void }) {
  const [reason, setReason] = useState("");

  const toggleMutation = useMutation({
    mutationFn: () =>
      updateRetireOverride(revision.id, { allow_installation_when_retired: !revision.allow_installation_when_retired, reason: reason.trim() }),
    onSuccess: () => {
      setReason("");
      onChanged();
    },
  });

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    if (reason.trim()) toggleMutation.mutate();
  }

  return (
    <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
      <h2 className="mb-3 text-sm font-semibold text-slate-300">Retirement Override</h2>
      <p className="mb-3 text-sm text-slate-400">
        New installations against this revision are currently{" "}
        <strong className={revision.allow_installation_when_retired ? "text-green-400" : "text-red-400"}>
          {revision.allow_installation_when_retired ? "allowed" : "blocked"}
        </strong>
        .
      </p>
      <form onSubmit={handleSubmit} className="space-y-2">
        <textarea
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Reason for this change (required)"
          rows={2}
          className="w-full rounded-sm border border-slate-700 bg-slate-800 px-3 py-1.5 text-sm text-slate-100 focus:border-blue-500 focus:outline-hidden"
        />
        <button
          type="submit"
          disabled={toggleMutation.isPending}
          className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm text-slate-200 hover:bg-slate-700 disabled:opacity-50"
        >
          {revision.allow_installation_when_retired ? "Block new installations" : "Allow new installations"}
        </button>
        {toggleMutation.isError && <p className="text-sm text-red-400">{(toggleMutation.error as Error).message}</p>}
      </form>
    </div>
  );
}
