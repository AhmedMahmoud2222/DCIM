import { useMutation, useQuery } from "@tanstack/react-query";
import { ChangeEvent, FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";

import {
  BulkImportResource,
  cancelImportJob,
  commitImportJob,
  downloadImportReport,
  downloadImportTemplate,
  getImportJob,
  listImportJobRows,
  uploadImportJob,
} from "@/features/bulk-import/api";
import { ApiError } from "@/lib/apiClient";
import { BulkImportMode } from "@/types";

const POLL_INTERVAL_MS = 1500;
const ROWS_PER_PAGE = 25;

// Backend states, per backend/app/domain/bulk_import/models.py: the job is still being
// worked on by a background task while in one of these — keep polling GET /import-jobs/
// {id} (and the rows list, once it's past uploaded/parsing) until it leaves this set.
const IN_FLIGHT_STATUSES = new Set(["uploaded", "parsing", "committing"]);
const CANCELLABLE_STATUSES = new Set(["uploaded", "validated"]);
const JOB_TERMINAL_STATUSES = new Set(["committed", "committed_with_errors", "cancelled", "failed_parse"]);

const JOB_STATUS_COLORS: Record<string, string> = {
  uploaded: "bg-slate-700 text-slate-200",
  parsing: "bg-blue-700 text-blue-100",
  validated: "bg-green-700 text-green-100",
  failed_parse: "bg-red-900 text-red-100",
  committing: "bg-blue-700 text-blue-100",
  committed: "bg-green-700 text-green-100",
  committed_with_errors: "bg-yellow-700 text-yellow-100",
  cancelled: "bg-slate-800 text-slate-500",
};

const ROW_STATUS_COLORS: Record<string, string> = {
  pending: "bg-slate-700 text-slate-200",
  valid: "bg-green-700 text-green-100",
  invalid: "bg-red-900 text-red-100",
  skipped: "bg-slate-800 text-slate-500",
  committed: "bg-green-700 text-green-100",
  failed: "bg-red-900 text-red-100",
};

function apiErrorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.detail : fallback;
}

function downloadBlob(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(url);
}

/** One reusable bulk-import wizard, shared by the Racks/Equipment/Catalog pages
 * (backend/app/api/v1/bulk_import.py's generic job pipeline). Hosted as a modal — same
 * fixed-overlay/focus-trap/Escape-to-close shape as ImpactAnalysisModal.tsx, the app's
 * only other modal — since a full upload → validate → preview → commit flow is too much
 * to fit in the pages' existing toggle-inline-form idiom used for "New Rack" etc. */
export function BulkImportPanel({
  resource,
  resourceLabel,
  onClose,
  onCommitted,
}: {
  resource: BulkImportResource;
  resourceLabel: string;
  onClose: () => void;
  onCommitted?: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [mode, setMode] = useState<BulkImportMode>("create_only");
  const [file, setFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [rowStatusFilter, setRowStatusFilter] = useState<"" | "valid" | "invalid">("");
  const [offset, setOffset] = useState(0);
  const [commitStarted, setCommitStarted] = useState(false);
  const [notifiedCommitted, setNotifiedCommitted] = useState(false);

  const templateMutation = useMutation({
    mutationFn: () => downloadImportTemplate(resource),
    onSuccess: (blob) => downloadBlob(blob, `${resource}-import-template.xlsx`),
  });

  const uploadMutation = useMutation({
    mutationFn: () => uploadImportJob(resource, file!, mode),
    onSuccess: (job) => setJobId(job.id),
  });

  const jobQuery = useQuery({
    queryKey: ["bulk-import-job", jobId],
    queryFn: () => getImportJob(jobId!),
    enabled: !!jobId,
    // Also keep polling once commit has been requested even while the job still reads
    // "validated": the POST /import-jobs/{id}/commit response (and this query's first
    // refetch right after it) can land before the dispatched Celery task has actually
    // flipped the job to "committing" — without this, that single race-prone refetch
    // would find the job still "validated" (not in IN_FLIGHT_STATUSES) and stop the
    // interval for good, leaving the UI stuck showing "validated" forever.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status || JOB_TERMINAL_STATUSES.has(status)) return false;
      return IN_FLIGHT_STATUSES.has(status) || commitStarted ? POLL_INTERVAL_MS : false;
    },
  });
  const job = jobQuery.data;

  // The row preview only exists (server-side) once parsing has produced rows — polls
  // alongside the job itself while a commit is in flight so committed/failed counts on
  // each row update live, then stops once the job reaches a terminal state.
  const showPreview = !!job && job.status !== "uploaded" && job.status !== "parsing" && job.status !== "failed_parse";
  const jobInFlight =
    !!job && !JOB_TERMINAL_STATUSES.has(job.status) && (IN_FLIGHT_STATUSES.has(job.status) || commitStarted);

  const rowsQuery = useQuery({
    queryKey: ["bulk-import-job-rows", jobId, rowStatusFilter, offset],
    queryFn: () => listImportJobRows(jobId!, { status: rowStatusFilter || undefined, limit: ROWS_PER_PAGE, offset }),
    enabled: showPreview,
    refetchInterval: jobInFlight ? POLL_INTERVAL_MS : false,
  });

  const commitMutation = useMutation({
    mutationFn: () => commitImportJob(jobId!),
    onSuccess: () => {
      setCommitStarted(true);
      jobQuery.refetch();
    },
  });

  const reportMutation = useMutation({
    mutationFn: () => downloadImportReport(jobId!),
    onSuccess: (blob) => downloadBlob(blob, `bulk-import-report-${jobId}.xlsx`),
  });

  const jobDone = job?.status === "committed" || job?.status === "committed_with_errors";
  useEffect(() => {
    if (jobDone && !notifiedCommitted) {
      setNotifiedCommitted(true);
      onCommitted?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobDone]);

  useEffect(() => {
    setOffset(0);
  }, [rowStatusFilter]);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    return () => previousFocus?.focus();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function handleDialogKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const controls = Array.from(
      dialogRef.current?.querySelectorAll<HTMLElement>(
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ) ?? [],
    );
    if (!controls.length) {
      event.preventDefault();
      return;
    }
    if (event.shiftKey && document.activeElement === controls[0]) {
      event.preventDefault();
      controls[controls.length - 1].focus();
    } else if (!event.shiftKey && document.activeElement === controls[controls.length - 1]) {
      event.preventDefault();
      controls[0].focus();
    }
  }

  function handleFileChange(e: ChangeEvent<HTMLInputElement>) {
    setFile(e.target.files?.[0] ?? null);
  }

  function handleUploadSubmit(e: FormEvent) {
    e.preventDefault();
    if (file) uploadMutation.mutate();
  }

  function resetForNewUpload() {
    if (job && CANCELLABLE_STATUSES.has(job.status)) {
      // Best-effort housekeeping — nothing in the UI depends on this resolving, and a
      // job left in 'uploaded'/'validated' is harmless either way.
      cancelImportJob(job.id).catch(() => undefined);
    }
    setJobId(null);
    setFile(null);
    setRowStatusFilter("");
    setOffset(0);
    setCommitStarted(false);
    setNotifiedCommitted(false);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }

  const committing = commitStarted && !jobDone && job?.status !== "cancelled";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="bulk-import-dialog-title"
        onKeyDown={handleDialogKeyDown}
        data-testid="bulk-import-panel"
        className="max-h-[85vh] w-full max-w-3xl overflow-auto rounded-sm border border-slate-700 bg-slate-900 p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h2 id="bulk-import-dialog-title" className="text-base font-semibold text-slate-100">
            Bulk import {resourceLabel}
          </h2>
          <button
            ref={closeButtonRef}
            onClick={onClose}
            className="rounded-sm bg-slate-800 px-2 py-1 text-xs text-slate-300 hover:bg-slate-700"
          >
            Close
          </button>
        </div>

        {!jobId && (
          <div className="space-y-4">
            <div>
              <button
                onClick={() => templateMutation.mutate()}
                disabled={templateMutation.isPending}
                className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm font-medium text-slate-100 hover:bg-slate-700 disabled:opacity-50"
              >
                {templateMutation.isPending ? "Downloading…" : "Download template"}
              </button>
              {templateMutation.isError && (
                <p role="alert" className="mt-2 text-sm text-red-400">
                  {apiErrorMessage(templateMutation.error, "Could not download the template.")}
                </p>
              )}
            </div>

            <form onSubmit={handleUploadSubmit} className="space-y-3 rounded-sm border border-slate-800 bg-slate-950 p-4">
              <fieldset>
                <legend className="mb-1 text-xs uppercase tracking-wide text-slate-500">Mode</legend>
                <label className="mr-4 inline-flex items-center gap-2 text-sm text-slate-200">
                  <input
                    type="radio"
                    name="bulk-import-mode"
                    value="create_only"
                    checked={mode === "create_only"}
                    onChange={() => setMode("create_only")}
                  />
                  Create only
                </label>
                <label className="inline-flex items-center gap-2 text-sm text-slate-200">
                  <input
                    type="radio"
                    name="bulk-import-mode"
                    value="update_existing"
                    checked={mode === "update_existing"}
                    onChange={() => setMode("update_existing")}
                  />
                  Update existing
                </label>
              </fieldset>

              <label className="block text-sm text-slate-300">
                File
                <input
                  ref={fileInputRef}
                  type="file"
                  aria-label="Import file"
                  accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                  onChange={handleFileChange}
                  className="mt-1 block w-full text-xs text-slate-400 file:mr-2 file:rounded-sm file:border-0 file:bg-slate-800 file:px-2 file:py-1 file:text-xs file:text-slate-200"
                />
              </label>

              <button
                type="submit"
                disabled={!file || uploadMutation.isPending}
                className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
              >
                {uploadMutation.isPending ? "Uploading…" : "Upload"}
              </button>
              {uploadMutation.isError && (
                <p role="alert" className="text-sm text-red-400">
                  {apiErrorMessage(uploadMutation.error, "Upload failed.")}
                </p>
              )}
            </form>
          </div>
        )}

        {jobId && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2 text-sm">
                <span className="text-slate-400">{job?.original_filename ?? "Uploading…"}</span>
                {job && (
                  <span
                    data-testid="bulk-import-job-status"
                    className={`rounded-sm px-2 py-0.5 text-xs ${JOB_STATUS_COLORS[job.status] ?? "bg-slate-700"}`}
                  >
                    {job.status.replace(/_/g, " ")}
                  </span>
                )}
              </div>
              <button onClick={resetForNewUpload} className="text-xs text-blue-400 hover:underline">
                Start over
              </button>
            </div>

            {!job && <p role="status" className="text-sm text-slate-400">Loading job…</p>}

            {job && jobInFlight && (
              <p role="status" className="text-sm text-slate-400">
                Processing… this view refreshes automatically.
              </p>
            )}

            {job?.status === "failed_parse" && (
              <p role="alert" className="text-sm text-red-400">
                {job.rejection_reason ?? "The uploaded file could not be parsed."}
              </p>
            )}

            {showPreview && job && (
              <>
                <dl className="grid grid-cols-3 gap-x-4 gap-y-1 text-sm sm:grid-cols-6">
                  <dt className="text-slate-500">Rows</dt>
                  <dd>{job.row_count}</dd>
                  <dt className="text-slate-500">Valid</dt>
                  <dd>{job.valid_row_count}</dd>
                  <dt className="text-slate-500">Errors</dt>
                  <dd>{job.error_row_count}</dd>
                  <dt className="text-slate-500">Warnings</dt>
                  <dd>{job.warning_row_count}</dd>
                  <dt className="text-slate-500">Committed</dt>
                  <dd>{job.committed_row_count}</dd>
                  <dt className="text-slate-500">Failed</dt>
                  <dd>{job.failed_row_count}</dd>
                </dl>

                <div className="flex items-center justify-between">
                  <label className="flex items-center gap-2 text-xs text-slate-400">
                    Filter
                    <select
                      aria-label="Filter rows by status"
                      value={rowStatusFilter}
                      onChange={(e) => setRowStatusFilter(e.target.value as "" | "valid" | "invalid")}
                      className="rounded-sm border border-slate-700 bg-slate-800 px-2 py-1 text-xs text-slate-100"
                    >
                      <option value="">All rows</option>
                      <option value="valid">Valid only</option>
                      <option value="invalid">Invalid only</option>
                    </select>
                  </label>

                  <button
                    onClick={() => commitMutation.mutate()}
                    disabled={job.status !== "validated" || commitMutation.isPending || commitStarted}
                    className="rounded-sm bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
                  >
                    {commitMutation.isPending || committing ? (
                      <span className="inline-flex items-center gap-1.5">
                        <span
                          aria-hidden="true"
                          className="h-3 w-3 animate-spin rounded-full border-2 border-white/40 border-t-white"
                        />
                        Committing…
                      </span>
                    ) : (
                      "Commit import"
                    )}
                  </button>
                </div>
                {commitMutation.isError && (
                  <p role="alert" className="text-sm text-red-400">
                    {apiErrorMessage(commitMutation.error, "Commit failed.")}
                  </p>
                )}

                {rowsQuery.data && (
                  <table className="w-full border-collapse text-sm">
                    <caption className="sr-only">Import rows preview</caption>
                    <thead>
                      <tr className="border-b border-slate-800 text-left text-slate-400">
                        <th scope="col" className="pb-2">Row</th>
                        <th scope="col" className="pb-2">Status</th>
                        <th scope="col" className="pb-2">Action</th>
                        <th scope="col" className="pb-2">Messages</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rowsQuery.data.items.map((row) => (
                        <tr key={row.id} data-testid="bulk-import-row" className="border-b border-slate-900 align-top">
                          <th scope="row" className="py-2 text-left font-normal text-slate-300">
                            {row.row_number}
                          </th>
                          <td className="py-2">
                            <span className={`rounded-sm px-2 py-0.5 text-xs ${ROW_STATUS_COLORS[row.status] ?? "bg-slate-700"}`}>
                              {row.status}
                            </span>
                          </td>
                          <td className="py-2 text-slate-400">{row.action ?? "—"}</td>
                          <td className="py-2">
                            {row.errors.length === 0 && row.warnings.length === 0 && <span className="text-slate-600">—</span>}
                            {row.errors.length > 0 && (
                              <ul className="space-y-0.5 text-xs text-red-400">
                                {row.errors.map((err, i) => (
                                  <li key={i}>
                                    {err.field}: {err.message}
                                  </li>
                                ))}
                              </ul>
                            )}
                            {row.warnings.length > 0 && (
                              <ul className="space-y-0.5 text-xs text-yellow-400">
                                {row.warnings.map((w, i) => (
                                  <li key={i}>
                                    {w.field}: {w.message}
                                  </li>
                                ))}
                              </ul>
                            )}
                          </td>
                        </tr>
                      ))}
                      {rowsQuery.data.items.length === 0 && (
                        <tr>
                          <td colSpan={4} className="py-4 text-center text-slate-500">
                            No rows match this filter.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                )}

                {rowsQuery.data && rowsQuery.data.total > ROWS_PER_PAGE && (
                  <div className="flex items-center justify-between text-xs text-slate-400">
                    <button
                      onClick={() => setOffset((o) => Math.max(0, o - ROWS_PER_PAGE))}
                      disabled={offset === 0}
                      className="rounded-sm bg-slate-800 px-2 py-1 hover:bg-slate-700 disabled:opacity-50"
                    >
                      Previous
                    </button>
                    <span>
                      {offset + 1}-{Math.min(offset + ROWS_PER_PAGE, rowsQuery.data.total)} of {rowsQuery.data.total}
                    </span>
                    <button
                      onClick={() => setOffset((o) => o + ROWS_PER_PAGE)}
                      disabled={offset + ROWS_PER_PAGE >= rowsQuery.data.total}
                      className="rounded-sm bg-slate-800 px-2 py-1 hover:bg-slate-700 disabled:opacity-50"
                    >
                      Next
                    </button>
                  </div>
                )}

                {!jobInFlight && job.report_available && (
                  <div>
                    <button
                      onClick={() => reportMutation.mutate()}
                      disabled={reportMutation.isPending}
                      className="rounded-sm bg-slate-800 px-3 py-1.5 text-sm font-medium text-slate-100 hover:bg-slate-700 disabled:opacity-50"
                    >
                      {reportMutation.isPending ? "Downloading…" : "Download results report"}
                    </button>
                    {reportMutation.isError && (
                      <p role="alert" className="mt-2 text-sm text-red-400">
                        {apiErrorMessage(reportMutation.error, "Could not download the report.")}
                      </p>
                    )}
                  </div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
