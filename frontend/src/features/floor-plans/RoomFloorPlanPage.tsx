import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  type CandidateCorrection,
  acceptCandidate,
  activateFloorPlan,
  correctCandidate,
  createFloorPlan,
  getImportDiagnostics,
  getRoomOverlays,
  listFloorPlanObjects,
  getRoomSpatialView,
  getSourceGeometry,
  listFloorPlans,
  listImportCandidates,
  listImportJobs,
  rejectCandidate,
  setCalibration,
  setRoomBoundary,
  undoCandidateCorrection,
  uploadFloorPlanFile,
} from "@/features/floor-plans/api";
import { CalibrationPanel } from "@/features/floor-plans/CalibrationPanel";
import { CoolingEnvironmentPanel } from "@/features/cooling/CoolingEnvironmentPanel";
import { CandidateReview } from "@/features/floor-plans/CandidateReview";
import { ImportReviewCanvas, type PickMode } from "@/features/floor-plans/ImportReviewCanvas";
import { OVERLAY_KINDS } from "@/features/floor-plans/overlayStyle";
import { RoomSpatialCanvas } from "@/features/floor-plans/RoomSpatialCanvas";
import { useGridSettings } from "@/features/floor-plans/gridSettings";
import { GridControls } from "@/features/floor-plans/SceneChrome";
import type { Point } from "@/features/floor-plans/spatialMath";
import { listRooms } from "@/features/racks/api";
import { ApiError } from "@/lib/apiClient";
import type { AcceptCandidateRequest, CalibrationRequest, ImportCandidate, OverlayKind } from "@/types";

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-slate-700 text-slate-200",
  active: "bg-green-700 text-green-100",
  superseded: "bg-slate-800 text-slate-500",
  queued: "bg-slate-700 text-slate-200",
  quarantined: "bg-yellow-700 text-yellow-100",
  parsing: "bg-blue-700 text-blue-100",
  parsed: "bg-green-700 text-green-100",
  failed: "bg-red-900 text-red-100",
  rejected: "bg-red-900 text-red-100",
};

const messageOf = (e: unknown): string => (e instanceof ApiError ? e.detail : e instanceof Error ? e.message : String(e));

export function RoomFloorPlanPage() {
  const { roomId } = useParams<{ roomId: string }>();
  const queryClient = useQueryClient();
  const [selectedFloorPlanId, setSelectedFloorPlanId] = useState<string | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null);
  const [pickMode, setPickMode] = useState<PickMode>("none");
  const [picks, setPicks] = useState<Point[]>([]);
  const [origin, setOrigin] = useState<Point | null>(null);
  const [overlay, setOverlay] = useState<OverlayKind | "none">("none");
  const [planMode, setPlanMode] = useState<"layout" | "cooling">("layout");
  const [grid, setGrid] = useGridSettings();
  const [actionError, setActionError] = useState<string | null>(null);
  const [boundaryWidth, setBoundaryWidth] = useState("");
  const [boundaryHeight, setBoundaryHeight] = useState("");
  const [notice, setNotice] = useState<string | null>(null);

  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms });
  const room = roomsQuery.data?.items.find((r) => r.id === roomId);

  const spatialViewQuery = useQuery({ queryKey: ["spatial", roomId], queryFn: () => getRoomSpatialView(roomId!), enabled: !!roomId });
  const overlaysQuery = useQuery({
    queryKey: ["overlays", roomId, overlay],
    queryFn: () => getRoomOverlays(roomId!, [overlay as OverlayKind]),
    enabled: !!roomId && overlay !== "none",
    // operational state must never be served from a stale cache entry: refetch on every switch and keep it live
    staleTime: 0,
    refetchOnMount: "always",
    refetchInterval: 30_000,
  });
  const floorPlansQuery = useQuery({ queryKey: ["floor-plans", roomId], queryFn: () => listFloorPlans(roomId), enabled: !!roomId });

  useEffect(() => {
    if (!selectedFloorPlanId && floorPlansQuery.data && floorPlansQuery.data.items.length > 0) setSelectedFloorPlanId(floorPlansQuery.data.items[0].id);
  }, [floorPlansQuery.data, selectedFloorPlanId]);

  const importJobsQuery = useQuery({
    queryKey: ["import-jobs", selectedFloorPlanId],
    queryFn: () => listImportJobs(selectedFloorPlanId!),
    enabled: !!selectedFloorPlanId,
    refetchInterval: 3000, // import runs in a background worker: poll until parsed/failed
  });
  useEffect(() => {
    // Pick the newest job only when nothing is selected. A job we just uploaded is selected before the polled list
    // contains it; resetting then would snap the view back to the previous import.
    const jobs = importJobsQuery.data?.items;
    if (jobs && jobs.length > 0 && !selectedJobId) setSelectedJobId(jobs[0].id);
  }, [importJobsQuery.data, selectedJobId]);

  const selectedJob = importJobsQuery.data?.items.find((j) => j.id === selectedJobId);
  const jobInFlight = (!!selectedJobId && !selectedJob) || (!!selectedJob && (selectedJob.status === "queued" || selectedJob.status === "parsing"));
  const parsed = selectedJob?.status === "parsed";

  const diagnosticsQuery = useQuery({
    queryKey: ["import-diagnostics", selectedJobId],
    queryFn: () => getImportDiagnostics(selectedJobId!),
    enabled: !!selectedJobId,
    retry: false,
    refetchInterval: jobInFlight ? 2000 : false,
  });
  const sourceQuery = useQuery({ queryKey: ["import-source", selectedJobId], queryFn: () => getSourceGeometry(selectedJobId!), enabled: !!selectedJobId && parsed });
  const candidatesQuery = useQuery({
    queryKey: ["import-candidates", selectedJobId],
    queryFn: () => listImportCandidates(selectedJobId!, "pending"),
    enabled: !!selectedJobId,
    refetchInterval: jobInFlight ? 2000 : false,
  });

  const selectedFloorPlan = floorPlansQuery.data?.items.find((fp) => fp.id === selectedFloorPlanId);
  // Accepted shapes of the *selected* revision (the room view only carries the active one): they lock the calibration.
  const planObjectsQuery = useQuery({
    queryKey: ["floor-plan-objects", selectedFloorPlanId],
    queryFn: () => listFloorPlanObjects(selectedFloorPlanId!),
    enabled: !!selectedFloorPlanId,
  });
  const calibration = selectedFloorPlan?.current_calibration ?? null;
  const candidates = useMemo(() => candidatesQuery.data?.items ?? [], [candidatesQuery.data]);
  useEffect(() => {
    if (!selectedCandidateId && candidates.length > 0) setSelectedCandidateId((candidates.find((c) => c.effective_object_type === "rack") ?? candidates[0]).id);
    if (selectedCandidateId && candidates.length > 0 && !candidates.some((c) => c.id === selectedCandidateId)) setSelectedCandidateId(candidates[0].id);
  }, [candidates, selectedCandidateId]);

  const refreshAll = () => {
    queryClient.invalidateQueries({ queryKey: ["floor-plans", roomId] });
    queryClient.invalidateQueries({ queryKey: ["import-candidates", selectedJobId] });
    queryClient.invalidateQueries({ queryKey: ["spatial", roomId] });
    queryClient.invalidateQueries({ queryKey: ["overlays", roomId] });
    queryClient.invalidateQueries({ queryKey: ["floor-plan-objects", selectedFloorPlanId] });
  };
  const onError = (e: unknown) => setActionError(messageOf(e));
  const onOk = () => setActionError(null);

  const createFloorPlanMutation = useMutation({
    mutationFn: () => createFloorPlan(roomId!),
    onSuccess: (fp) => {
      setSelectedFloorPlanId(fp.id);
      setSelectedJobId(null);
      queryClient.invalidateQueries({ queryKey: ["floor-plans", roomId] });
    },
    onError,
  });
  const activateMutation = useMutation({
    mutationFn: (fp: { id: string; version: number }) => activateFloorPlan(fp.id, fp.version),
    onSuccess: () => (onOk(), refreshAll()),
    onError,
  });
  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadFloorPlanFile(selectedFloorPlanId!, file),
    onSuccess: (job) => {
      onOk();
      setNotice(job.deduplicated ? "This exact file was already uploaded to this revision; showing the existing import." : null);
      setSelectedJobId(job.id);
      setSelectedCandidateId(null);
      setPicks([]);
      setOrigin(null);
      queryClient.invalidateQueries({ queryKey: ["import-jobs", selectedFloorPlanId] });
    },
    onError,
  });
  const calibrateMutation = useMutation({
    mutationFn: (request: CalibrationRequest) => setCalibration(selectedFloorPlan!, request),
    onSuccess: (result) => {
      onOk();
      setNotice(
        `Calibration recorded (${result.calibration.confidence} confidence${result.calibration.error_bound_mm != null ? `, error ≤ ±${result.calibration.error_bound_mm} mm` : ""}). Rack matches were re-derived.`,
      );
      setPicks([]);
      setOrigin(null);
      setPickMode("none");
      refreshAll();
    },
    onError,
  });
  const correctMutation = useMutation({
    mutationFn: (v: { candidate: ImportCandidate; body: CandidateCorrection }) => correctCandidate(selectedJobId!, v.candidate, v.body),
    onSuccess: () => (onOk(), queryClient.invalidateQueries({ queryKey: ["import-candidates", selectedJobId] })),
    onError: (e) => (onError(e), queryClient.invalidateQueries({ queryKey: ["import-candidates", selectedJobId] })),
  });
  const undoMutation = useMutation({
    mutationFn: (candidate: ImportCandidate) => undoCandidateCorrection(selectedJobId!, candidate),
    onSuccess: () => (onOk(), queryClient.invalidateQueries({ queryKey: ["import-candidates", selectedJobId] })),
    onError,
  });
  const acceptMutation = useMutation({
    mutationFn: (v: { candidate: ImportCandidate; body: AcceptCandidateRequest }) => acceptCandidate(selectedJobId!, v.candidate, v.body),
    onSuccess: () => {
      onOk();
      setNotice("Accepted. The shape is now authoritative geometry for this revision.");
      refreshAll();
    },
    onError: (e) => (onError(e), queryClient.invalidateQueries({ queryKey: ["import-candidates", selectedJobId] })),
  });
  const rejectMutation = useMutation({
    mutationFn: (candidate: ImportCandidate) => rejectCandidate(selectedJobId!, candidate),
    onSuccess: () => (onOk(), refreshAll()),
    onError,
  });
  const boundaryMutation = useMutation({
    mutationFn: (v: { width: number; height: number }) => setRoomBoundary(selectedFloorPlan!, { shape: "rect", x_mm: 0, y_mm: 0, width_mm: v.width, height_mm: v.height }),
    onSuccess: (r) => {
      onOk();
      setNotice(
        r.racks_outside_boundary.length ? `Boundary saved. Racks outside it (not moved): ${r.racks_outside_boundary.join(", ")}.` : "Room boundary saved.",
      );
      refreshAll();
    },
    onError,
  });

  function handlePick(point: Point) {
    if (pickMode === "two-point") {
      setPicks((current) => {
        const next = current.length >= 2 ? [point] : [...current, point];
        if (next.length === 2) setPickMode("none");
        return next;
      });
    } else if (pickMode === "origin") {
      setOrigin(point);
      setPickMode("none");
    }
  }

  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) uploadMutation.mutate(file);
    e.target.value = "";
  }

  function submitBoundary(e: FormEvent) {
    e.preventDefault();
    const width = Number(boundaryWidth);
    const height = Number(boundaryHeight);
    if (width > 0 && height > 0) boundaryMutation.mutate({ width, height });
  }

  const acceptedCount = (planObjectsQuery.data ?? []).filter((o) => o.source === "imported").length;
  const steps = [
    { id: "upload", label: "Upload", done: !!selectedJob },
    { id: "review", label: "Review & classify", done: parsed && candidates.length > 0 },
    { id: "calibrate", label: "Calibrate", done: !!calibration },
    { id: "correct", label: "Correct & match", done: candidates.some((c) => c.correction) },
    { id: "accept", label: "Accept", done: acceptedCount > 0 },
    { id: "verify", label: "Verify in 2D / 3D", done: spatialViewQuery.data?.layout_state === "validated" },
  ];
  const currentStep = steps.findIndex((s) => !s.done);

  return (
    <div>
      <Link to="/floor-plans" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Floor Plans
      </Link>
      <h1 className="mb-4 text-lg font-semibold">{room?.name ?? "Room"} — Spatial digital twin</h1>

      <ol className="mb-5 flex flex-wrap gap-2 text-xs" aria-label="Import workflow" data-testid="workflow-steps">
        {steps.map((s, i) => (
          <li key={s.id} aria-current={i === currentStep ? "step" : undefined} data-step={s.id} data-done={s.done ? "true" : "false"} className={`rounded-sm px-2 py-1 ${s.done ? "bg-green-900 text-green-100" : i === currentStep ? "bg-blue-800 text-blue-100" : "bg-slate-800 text-slate-400"}`}>
            {i + 1}. {s.label}
            {s.done && <span className="sr-only"> (done)</span>}
          </li>
        ))}
      </ol>

      {actionError && (
        <p role="alert" className="mb-3 rounded-sm border border-red-900 bg-red-950/40 p-2 text-sm text-red-300" data-testid="action-error">
          {actionError}
        </p>
      )}
      {notice && (
        <p role="status" className="mb-3 rounded-sm border border-slate-700 bg-slate-900 p-2 text-sm text-slate-300" data-testid="notice">
          {notice}
        </p>
      )}

      <div className="mb-6 grid gap-6 lg:grid-cols-2">
        <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-300">Floor plan revisions</h2>
            <button onClick={() => createFloorPlanMutation.mutate()} disabled={createFloorPlanMutation.isPending} className="rounded-sm bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50">
              New draft
            </button>
          </div>
          <ul className="space-y-1">
            {floorPlansQuery.data?.items.map((fp) => (
              <li key={fp.id}>
                <div className={`flex items-center justify-between rounded px-2 py-1.5 text-sm ${fp.id === selectedFloorPlanId ? "bg-slate-800" : "hover:bg-slate-800/50"}`}>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedFloorPlanId(fp.id);
                      setSelectedJobId(null);
                      setSelectedCandidateId(null);
                    }}
                    aria-pressed={fp.id === selectedFloorPlanId} className="text-left">
                    Revision {fp.revision_number}
                    {fp.current_calibration && <span className="ml-2 text-xs text-slate-500">calibrated</span>}
                  </button>
                  <span className="flex items-center gap-2">
                    <span className={`rounded-sm px-2 py-0.5 text-xs ${STATUS_COLORS[fp.status]}`}>{fp.status}</span>
                    {fp.status !== "active" && (
                      <button type="button" onClick={() => activateMutation.mutate({ id: fp.id, version: fp.version })} className="text-xs text-blue-400 hover:underline">
                        Activate
                      </button>
                    )}
                  </span>
                </div>
              </li>
            ))}
            {floorPlansQuery.data?.items.length === 0 && <li className="py-4 text-center text-sm text-slate-500">No floor plans yet.</li>}
          </ul>

          {selectedFloorPlan && (
            <div className="mt-4 space-y-3 border-t border-slate-800 pt-4">
              <label className="block text-xs text-slate-500">
                Upload DXF, VSDX, SVG, PNG or JPEG for revision {selectedFloorPlan.revision_number}
                <input
                  type="file"
                  aria-label="Upload floor plan file"
                  accept=".dxf,.vsdx,.svg,.png,.jpg,.jpeg,image/svg+xml,image/png,image/jpeg"
                  onChange={handleFileChange}
                  disabled={uploadMutation.isPending}
                  className="mt-1 block w-full text-xs text-slate-400 file:mr-2 file:rounded-sm file:border-0 file:bg-slate-800 file:px-2 file:py-1 file:text-xs file:text-slate-200"
                />
              </label>
              {uploadMutation.isPending && <p role="status" className="text-xs text-slate-400">Uploading…</p>}
              <p className="text-xs text-slate-600">Files are checked by content, parsed in an isolated sandbox, and never change inventory until you accept a shape.</p>

              <form onSubmit={submitBoundary} className="rounded-sm bg-slate-800/40 p-2 text-xs text-slate-400" aria-label="Set rectangular room boundary">
                <p className="mb-1 font-medium text-slate-300">Room boundary (manual)</p>
                <div className="flex flex-wrap items-end gap-2">
                  <label>
                    Width (mm)
                    <input aria-label="Boundary width (mm)" inputMode="numeric" value={boundaryWidth} onChange={(e) => setBoundaryWidth(e.target.value)} className="mt-1 block w-24 rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                  </label>
                  <label>
                    Depth (mm)
                    <input aria-label="Boundary depth (mm)" inputMode="numeric" value={boundaryHeight} onChange={(e) => setBoundaryHeight(e.target.value)} className="mt-1 block w-24 rounded-sm border border-slate-700 bg-slate-950 px-2 py-1 text-sm" />
                  </label>
                  <button type="submit" disabled={boundaryMutation.isPending || !(Number(boundaryWidth) > 0 && Number(boundaryHeight) > 0)} className="rounded-sm border border-slate-600 px-2 py-1 disabled:opacity-40">
                    Set boundary
                  </button>
                </div>
                <p className="mt-1 text-slate-500">Or accept a detected room outline below.</p>
              </form>
            </div>
          )}
        </div>

        <div className="rounded-sm border border-slate-800 bg-slate-900 p-4" data-testid="diagnostics-panel">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">Import diagnostics</h2>
          {!selectedJob && !selectedJobId && <p className="text-sm text-slate-500">Upload a file to see import diagnostics here.</p>}
          {!selectedJob && !!selectedJobId && <p role="status" className="text-sm text-slate-500">Processing… this view refreshes automatically.</p>}
          {selectedJob && (
            <>
              <div className="mb-2 flex flex-wrap items-center gap-2 text-sm">
                <span className="text-slate-400">{selectedJob.original_filename}</span>
                <span data-testid="job-status" className={`rounded-sm px-2 py-0.5 text-xs ${STATUS_COLORS[selectedJob.status] ?? "bg-slate-700"}`}>
                  {selectedJob.status}
                </span>
                {selectedJob.detected_format && <span className="text-xs text-slate-500">detected: {selectedJob.detected_format}</span>}
              </div>
              {selectedJob.status === "failed" && (
                <p role="alert" className="mb-2 text-sm text-red-400" data-testid="job-failure">
                  {selectedJob.rejection_reason}
                </p>
              )}
              {jobInFlight && <p role="status" className="text-sm text-slate-500">Processing… this view refreshes automatically.</p>}
              {diagnosticsQuery.data && (
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                  <dt className="text-slate-500">Parser</dt>
                  <dd>
                    {diagnosticsQuery.data.parser_name ?? "n/a"} v{diagnosticsQuery.data.parser_version ?? "?"}
                  </dd>
                  <dt className="text-slate-500">Source units</dt>
                  <dd>
                    {diagnosticsQuery.data.source_units ?? "n/a"}
                    {diagnosticsQuery.data.units_trusted === false && " (not authoritative)"}
                  </dd>
                  <dt className="text-slate-500">Objects discovered</dt>
                  <dd>{diagnosticsQuery.data.objects_discovered}</dd>
                  <dt className="text-slate-500">Racks detected</dt>
                  <dd data-testid="racks-detected">{diagnosticsQuery.data.racks_detected}</dd>
                  <dt className="text-slate-500">Needing review</dt>
                  <dd>{diagnosticsQuery.data.ambiguous_count}</dd>
                  <dt className="text-slate-500">Unsupported/stripped</dt>
                  <dd>{diagnosticsQuery.data.unsupported_object_count}</dd>
                  {diagnosticsQuery.data.sir_sha256 && (
                    <>
                      <dt className="text-slate-500">Geometry fingerprint</dt>
                      <dd className="truncate font-mono text-xs" title={diagnosticsQuery.data.sir_sha256}>
                        {diagnosticsQuery.data.sir_sha256.slice(0, 12)}…
                      </dd>
                    </>
                  )}
                </dl>
              )}
              {diagnosticsQuery.data && diagnosticsQuery.data.warnings.length > 0 && (
                <ul className="mt-2 space-y-0.5 text-xs text-yellow-400">
                  {diagnosticsQuery.data.warnings.map((w, i) => (
                    <li key={i}>⚠ {w}</li>
                  ))}
                </ul>
              )}
            </>
          )}
        </div>
      </div>

      {selectedFloorPlan && selectedJob && parsed && (
        <div className="mb-6 grid gap-6 xl:grid-cols-[minmax(0,1fr)_420px]" data-testid="review-workspace">
          <div className="rounded-sm border border-slate-800 bg-slate-900 p-4">
            <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
              <h2 className="text-sm font-semibold text-slate-300">Review, correct and calibrate</h2>
              <GridControls settings={grid} onChange={setGrid} idPrefix="review" />
            </div>
            <ImportReviewCanvas
              source={sourceQuery.data ?? null}
              candidates={candidates}
              calibration={calibration}
              selectedId={selectedCandidateId}
              onSelect={setSelectedCandidateId}
              onMove={(candidate, centre) => correctMutation.mutate({ candidate, body: centre })}
              pickMode={pickMode}
              picks={origin ? [...picks, origin] : picks}
              onPick={handlePick}
              showGrid={grid.showGrid}
              snapMm={grid.snapMm}
            />
            {sourceQuery.data?.truncated && <p className="mt-1 text-xs text-yellow-400">Only the first {sourceQuery.data.entities.length} of {sourceQuery.data.total_entities} drawing entities are shown.</p>}
          </div>
          <div className="space-y-4">
            <CalibrationPanel
              floorPlan={selectedFloorPlan}
              jobId={selectedJob.id}
              diagnostics={diagnosticsQuery.data}
              picks={picks}
              pickMode={pickMode}
              onPickMode={setPickMode}
              onClearPicks={() => setPicks([])}
              origin={origin}
              onClearOrigin={() => setOrigin(null)}
              pending={calibrateMutation.isPending}
              error={calibrateMutation.isError ? messageOf(calibrateMutation.error) : null}
              locked={acceptedCount > 0 && !!calibration}
              onSubmit={(request) => calibrateMutation.mutate(request)}
            />
            <CandidateReview
              view={spatialViewQuery.data}
              candidates={candidates}
              selectedId={selectedCandidateId}
              onSelect={setSelectedCandidateId}
              calibration={calibration}
              busy={correctMutation.isPending || acceptMutation.isPending || rejectMutation.isPending || undoMutation.isPending}
              error={actionError}
              onCorrect={(candidate, body) => correctMutation.mutate({ candidate, body })}
              onUndo={(candidate) => undoMutation.mutate(candidate)}
              onAccept={(candidate, body) => acceptMutation.mutate({ candidate, body })}
              onReject={(candidate) => rejectMutation.mutate(candidate)}
            />
          </div>
        </div>
      )}

      <div className="rounded-sm border border-slate-800 bg-slate-900 p-4" data-testid="verify-2d">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-sm font-semibold text-slate-300">Scaled 2D layout (active revision)</h2>
          <div className="flex flex-wrap items-center gap-4">
            <div role="group" aria-label="Plan mode" className="flex overflow-hidden rounded-sm border border-slate-700 text-xs">
              {([["layout", "Layout"], ["cooling", "Cooling & environment"]] as const).map(([mode, label]) => (
                <button key={mode} type="button" aria-pressed={planMode === mode} onClick={() => setPlanMode(mode)} className={`px-2.5 py-1 ${planMode === mode ? "bg-blue-700 text-white" : "bg-slate-900 text-slate-300 hover:bg-slate-800"}`}>
                  {label}
                </button>
              ))}
            </div>
            {planMode === "layout" && <GridControls settings={grid} onChange={setGrid} idPrefix="verify" />}
            {planMode === "layout" && <label className="text-xs text-slate-400">
              Overlay{" "}
              <select aria-label="Operational overlay" value={overlay} onChange={(e) => setOverlay(e.target.value as OverlayKind | "none")} className="rounded-sm border border-slate-700 bg-slate-950 px-1 py-0.5 text-xs">
                <option value="none">none</option>
                {OVERLAY_KINDS.map((k) => (
                  <option key={k.id} value={k.id}>
                    {k.label}
                  </option>
                ))}
              </select>
            </label>}
            <Link to={`/floor-plans/3d-layout?room=${roomId}`} className="rounded-sm border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:bg-slate-800">
              Open 3D twin
            </Link>
          </div>
        </div>
        {planMode === "layout" && overlay !== "none" && (
          <p className="mb-2 text-xs text-slate-500" data-testid="overlay-source">
            {OVERLAY_KINDS.find((k) => k.id === overlay)?.source}
            {overlaysQuery.isLoading && " · loading…"}
            {overlaysQuery.isError && <span role="alert" className="text-red-400"> · {messageOf(overlaysQuery.error)}</span>}
          </p>
        )}
        {spatialViewQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
        {spatialViewQuery.error && <p role="alert" className="text-sm text-red-400">{messageOf(spatialViewQuery.error)}</p>}
        {spatialViewQuery.data && (
          <>
            <p className="mb-2 text-xs text-slate-500">
              {spatialViewQuery.data.active_floor_plan_id
                ? `Active revision ${spatialViewQuery.data.active_floor_plan_revision}`
                : "No active floor plan for this room yet. Activate a revision to publish its geometry; placed racks are shown meanwhile."}
            </p>
            {planMode === "cooling" && roomId ? (
              <CoolingEnvironmentPanel roomId={roomId} view={spatialViewQuery.data} />
            ) : (
              <RoomSpatialCanvas view={spatialViewQuery.data} overlay={overlay} overlays={overlaysQuery.data ?? null} showGrid={grid.showGrid} />
            )}
          </>
        )}
      </div>
    </div>
  );
}
