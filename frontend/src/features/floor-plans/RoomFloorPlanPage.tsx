import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChangeEvent, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import {
  acceptCandidate,
  activateFloorPlan,
  createFloorPlan,
  getImportDiagnostics,
  getRoomSpatialView,
  listFloorPlans,
  listImportCandidates,
  listImportJobs,
  rejectCandidate,
  uploadFloorPlanFile,
} from "@/features/floor-plans/api";
import { RoomSpatialCanvas } from "@/features/floor-plans/RoomSpatialCanvas";
import { listRooms } from "@/features/racks/api";
import { ApiError } from "@/lib/apiClient";

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

export function RoomFloorPlanPage() {
  const { roomId } = useParams<{ roomId: string }>();
  const queryClient = useQueryClient();
  const [selectedFloorPlanId, setSelectedFloorPlanId] = useState<string | null>(null);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);

  const roomsQuery = useQuery({ queryKey: ["rooms"], queryFn: listRooms });
  const room = roomsQuery.data?.items.find((r) => r.id === roomId);

  const spatialViewQuery = useQuery({
    queryKey: ["spatial", roomId],
    queryFn: () => getRoomSpatialView(roomId!),
    enabled: !!roomId,
  });

  const floorPlansQuery = useQuery({
    queryKey: ["floor-plans", roomId],
    queryFn: () => listFloorPlans(roomId),
    enabled: !!roomId,
  });

  useEffect(() => {
    if (!selectedFloorPlanId && floorPlansQuery.data && floorPlansQuery.data.items.length > 0) {
      setSelectedFloorPlanId(floorPlansQuery.data.items[0].id);
    }
  }, [floorPlansQuery.data, selectedFloorPlanId]);

  const importJobsQuery = useQuery({
    queryKey: ["import-jobs", selectedFloorPlanId],
    queryFn: () => listImportJobs(selectedFloorPlanId!),
    enabled: !!selectedFloorPlanId,
    refetchInterval: 3000, // import runs in a background worker — poll until parsed/failed
  });

  useEffect(() => {
    if (importJobsQuery.data && importJobsQuery.data.items.length > 0) {
      setSelectedJobId(importJobsQuery.data.items[0].id);
    }
  }, [importJobsQuery.data]);

  const selectedJob = importJobsQuery.data?.items.find((j) => j.id === selectedJobId);
  // The import job runs in a background worker (§36) — diagnostics/candidates don't
  // exist yet while it's still queued/parsing, so both queries poll alongside the job
  // itself and stop once it reaches a terminal state, rather than 404ing once and (with
  // react-query's default retry: false-adjacent single-shot behavior) never refreshing.
  const jobInFlight = !selectedJob || selectedJob.status === "queued" || selectedJob.status === "parsing";

  const diagnosticsQuery = useQuery({
    queryKey: ["import-diagnostics", selectedJobId],
    queryFn: () => getImportDiagnostics(selectedJobId!),
    enabled: !!selectedJobId,
    retry: false,
    refetchInterval: jobInFlight ? 2000 : false,
  });

  const candidatesQuery = useQuery({
    queryKey: ["import-candidates", selectedJobId],
    queryFn: () => listImportCandidates(selectedJobId!, "pending"),
    enabled: !!selectedJobId,
    refetchInterval: jobInFlight ? 2000 : false,
  });

  const createFloorPlanMutation = useMutation({
    mutationFn: () => createFloorPlan(roomId!),
    onSuccess: (fp) => {
      setSelectedFloorPlanId(fp.id);
      queryClient.invalidateQueries({ queryKey: ["floor-plans", roomId] });
    },
  });

  const activateMutation = useMutation({
    mutationFn: (fp: { id: string; version: number }) => activateFloorPlan(fp.id, fp.version),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["floor-plans", roomId] });
      queryClient.invalidateQueries({ queryKey: ["spatial", roomId] });
    },
  });

  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadFloorPlanFile(selectedFloorPlanId!, file),
    onSuccess: (job) => {
      setSelectedJobId(job.id);
      queryClient.invalidateQueries({ queryKey: ["import-jobs", selectedFloorPlanId] });
    },
  });

  const acceptMutation = useMutation({
    mutationFn: (candidateId: string) => acceptCandidate(selectedJobId!, candidateId, "imported_shape"),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["import-candidates", selectedJobId] });
      queryClient.invalidateQueries({ queryKey: ["spatial", roomId] });
    },
  });

  const rejectMutation = useMutation({
    mutationFn: (candidateId: string) => rejectCandidate(selectedJobId!, candidateId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["import-candidates", selectedJobId] }),
  });

  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (file) uploadMutation.mutate(file);
    e.target.value = "";
  }

  const selectedFloorPlan = floorPlansQuery.data?.items.find((fp) => fp.id === selectedFloorPlanId);

  return (
    <div>
      <Link to="/floor-plans" className="mb-4 inline-block text-sm text-slate-400 hover:text-slate-200">
        ← Floor Plans
      </Link>
      <h1 className="mb-6 text-lg font-semibold">{room?.name ?? "Room"} — Spatial View</h1>

      <div className="mb-6 rounded border border-slate-800 bg-slate-900 p-4">
        <h2 className="mb-3 text-sm font-semibold text-slate-300">2D Layout</h2>
        {spatialViewQuery.isLoading && <p className="text-sm text-slate-400">Loading…</p>}
        {spatialViewQuery.error && <p className="text-sm text-red-400">{(spatialViewQuery.error as Error).message}</p>}
        {spatialViewQuery.data && (
          <>
            <p className="mb-2 text-xs text-slate-500">
              {spatialViewQuery.data.active_floor_plan_id
                ? `Active revision ${spatialViewQuery.data.active_floor_plan_revision}`
                : "No active floor plan for this room yet — showing placed racks/equipment only."}
            </p>
            <RoomSpatialCanvas view={spatialViewQuery.data} />
          </>
        )}
      </div>

      <div className="grid grid-cols-2 gap-6">
        <div className="rounded border border-slate-800 bg-slate-900 p-4">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-300">Floor Plan Revisions</h2>
            <button
              onClick={() => createFloorPlanMutation.mutate()}
              disabled={createFloorPlanMutation.isPending}
              className="rounded bg-blue-600 px-2.5 py-1 text-xs font-medium text-white hover:bg-blue-500 disabled:opacity-50"
            >
              New draft
            </button>
          </div>
          <ul className="space-y-1">
            {floorPlansQuery.data?.items.map((fp) => (
              <li
                key={fp.id}
                onClick={() => setSelectedFloorPlanId(fp.id)}
                className={`flex cursor-pointer items-center justify-between rounded px-2 py-1.5 text-sm ${
                  fp.id === selectedFloorPlanId ? "bg-slate-800" : "hover:bg-slate-800/50"
                }`}
              >
                <span>Revision {fp.revision_number}</span>
                <span className="flex items-center gap-2">
                  <span className={`rounded px-2 py-0.5 text-xs ${STATUS_COLORS[fp.status]}`}>{fp.status}</span>
                  {fp.status !== "active" && (
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        activateMutation.mutate({ id: fp.id, version: fp.version });
                      }}
                      className="text-xs text-blue-400 hover:underline"
                    >
                      Activate
                    </button>
                  )}
                </span>
              </li>
            ))}
            {floorPlansQuery.data?.items.length === 0 && (
              <li className="py-4 text-center text-sm text-slate-500">No floor plans yet.</li>
            )}
          </ul>

          {selectedFloorPlan && (
            <div className="mt-4 border-t border-slate-800 pt-4">
              <label className="block text-xs text-slate-500">
                Upload SVG/PNG/JPEG for revision {selectedFloorPlan.revision_number}
                <input
                  type="file"
                  accept=".svg,.png,.jpg,.jpeg,image/svg+xml,image/png,image/jpeg"
                  onChange={handleFileChange}
                  disabled={uploadMutation.isPending}
                  className="mt-1 block w-full text-xs text-slate-400 file:mr-2 file:rounded file:border-0 file:bg-slate-800 file:px-2 file:py-1 file:text-xs file:text-slate-200"
                />
              </label>
              {uploadMutation.isError && (
                <p className="mt-1 text-xs text-red-400">
                  {uploadMutation.error instanceof ApiError ? uploadMutation.error.detail : (uploadMutation.error as Error).message}
                </p>
              )}
            </div>
          )}
        </div>

        <div className="rounded border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-3 text-sm font-semibold text-slate-300">Import Diagnostics</h2>
          {!selectedJob && <p className="text-sm text-slate-500">Upload a file to see import diagnostics here.</p>}
          {selectedJob && (
            <>
              <div className="mb-2 flex items-center gap-2 text-sm">
                <span className="text-slate-400">{selectedJob.original_filename}</span>
                <span className={`rounded px-2 py-0.5 text-xs ${STATUS_COLORS[selectedJob.status] ?? "bg-slate-700"}`}>
                  {selectedJob.status}
                </span>
              </div>
              {selectedJob.status === "failed" && (
                <p className="mb-2 text-sm text-red-400">{selectedJob.rejection_reason}</p>
              )}
              {(selectedJob.status === "queued" || selectedJob.status === "parsing") && (
                <p className="text-sm text-slate-500">Processing… this view refreshes automatically.</p>
              )}
              {diagnosticsQuery.data && (
                <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm">
                  <dt className="text-slate-500">Objects discovered</dt>
                  <dd>{diagnosticsQuery.data.objects_discovered}</dd>
                  <dt className="text-slate-500">Racks detected</dt>
                  <dd>{diagnosticsQuery.data.racks_detected}</dd>
                  <dt className="text-slate-500">Unsupported/stripped</dt>
                  <dd>{diagnosticsQuery.data.unsupported_object_count}</dd>
                </dl>
              )}
              {diagnosticsQuery.data && diagnosticsQuery.data.warnings.length > 0 && (
                <ul className="mt-2 space-y-0.5 text-xs text-yellow-400">
                  {diagnosticsQuery.data.warnings.map((w, i) => (
                    <li key={i}>⚠ {w}</li>
                  ))}
                </ul>
              )}

              {candidatesQuery.data && candidatesQuery.data.items.length > 0 && (
                <div className="mt-4 border-t border-slate-800 pt-3">
                  <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">
                    Pending candidates ({candidatesQuery.data.total})
                  </h3>
                  <ul className="space-y-1">
                    {candidatesQuery.data.items.map((c) => (
                      <li key={c.id} className="flex items-center justify-between rounded bg-slate-800/50 px-2 py-1.5 text-sm">
                        <span>
                          {c.raw_geometry.shape_type}
                          {c.suggested_object_type && (
                            <span className="ml-2 rounded bg-slate-700 px-1.5 py-0.5 text-xs text-slate-300">
                              suggests: {c.suggested_object_type}
                            </span>
                          )}
                        </span>
                        <span className="flex gap-2">
                          <button
                            onClick={() => acceptMutation.mutate(c.id)}
                            disabled={acceptMutation.isPending}
                            className="text-xs text-green-400 hover:underline disabled:opacity-50"
                          >
                            Accept
                          </button>
                          <button
                            onClick={() => rejectMutation.mutate(c.id)}
                            disabled={rejectMutation.isPending}
                            className="text-xs text-red-400 hover:underline disabled:opacity-50"
                          >
                            Reject
                          </button>
                        </span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
