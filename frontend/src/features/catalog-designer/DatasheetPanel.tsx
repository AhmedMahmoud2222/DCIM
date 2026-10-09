import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { useHasPermission } from "@/features/auth/useAuthorization";
import * as api from "@/features/catalog-designer/extractionApi";
import { ApiError } from "@/lib/apiClient";
import { CatalogModelRevisionDetail } from "@/types";

const SUPPORTED = new Set(["width", "height", "depth", "weight", "power_rated_w", "power_typical_w", "power_max_w", "rack_units", "heat_dissipation", "airflow_direction"]);
const BLOCKING = new Set(["number_format_ambiguous", "unit_missing", "unit_unrecognized", "unit_dimension_mismatch", "dimensions_order_unknown", "multi_value_line"]);

export function DatasheetPanel({ revision, readOnly, onChanged }: {
  revision: CatalogModelRevisionDetail; readOnly: boolean; onChanged: () => void;
}) {
  const canDownload = useHasPermission("catalog:document_download");
  const canReadDraft = useHasPermission("catalog:read_draft");
  const canWrite = !readOnly && canDownload && canReadDraft;
  const cache = useQueryClient();
  const [documentId, setDocumentId] = useState("");
  const [jobId, setJobId] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [selectionVersion, setSelectionVersion] = useState<number | null>(null);
  const [overwrite, setOverwrite] = useState(false);
  const [message, setMessage] = useState("");
  const documents = useQuery({ queryKey: ["datasheets", revision.id], queryFn: () => api.listDatasheets(revision.id), enabled: canDownload });
  const modelDocuments = useQuery({ queryKey: ["modelDatasheets", revision.catalog_model_id],
    queryFn: () => api.listModelDatasheets(revision.catalog_model_id), enabled: canWrite });
  const jobs = useQuery({ queryKey: ["extractionJobs", documentId], queryFn: () => api.listExtractionJobs(documentId), enabled: canDownload && !!documentId,
    refetchInterval: (query) => query.state.data?.some(j => ["queued", "running"].includes(j.status)) ? 2000 : false });
  const job = jobs.data?.find(j => j.id === jobId) ?? jobs.data?.find(j => j.is_current) ?? jobs.data?.[0];
  const candidates = useQuery({ queryKey: ["extractionCandidates", job?.id], queryFn: () => api.listCandidates(job!.id), enabled: canDownload && job?.status === "completed" });
  const history = useQuery({ queryKey: ["extractionApplications", revision.id], queryFn: () => api.listApplications(revision.id), enabled: canDownload });

  function resetSelection() { setSelected([]); setSelectionVersion(null); setOverwrite(false); }
  function changed() {
    cache.invalidateQueries({ queryKey: ["datasheets", revision.id] });
    cache.invalidateQueries({ queryKey: ["modelDatasheets", revision.catalog_model_id] });
    cache.invalidateQueries({ queryKey: ["extractionJobs", documentId] });
    cache.invalidateQueries({ queryKey: ["extractionCandidates"] });
    cache.invalidateQueries({ queryKey: ["extractionApplications", revision.id] });
    onChanged();
  }
  const action = useMutation({
    mutationFn: async (operation: () => Promise<unknown>) => operation(),
    onSuccess: () => { changed(); setMessage("Operation completed."); },
    onError: (error) => {
      setMessage(error instanceof ApiError && error.status === 409 ? `${error.message} Reload the draft and select values again.` : (error as Error).message);
      if (error instanceof ApiError && error.status === 409) { resetSelection(); onChanged(); }
    },
  });
  function run(operation: () => Promise<unknown>) { setMessage(""); action.mutate(operation); }
  if (!canDownload) return <section aria-label="Datasheets"><h2>Datasheets</h2><p>Document download permission is required to review extraction evidence.</p></section>;

  const attached = new Set(documents.data?.map(d => d.id));
  return <section aria-label="Datasheets" className="my-6 space-y-4 rounded border border-slate-700 p-4">
    <h2 className="font-semibold">Datasheets and extraction</h2>
    <p className="text-sm text-slate-300">Review source evidence, then explicitly apply selected accepted values to this draft. Unsupported fields, ranges and unresolved units remain available for manual review.</p>
    {[documents.error, modelDocuments.error, jobs.error, candidates.error, history.error].filter(Boolean).map((error, i) => <p role="alert" key={i}>{(error as Error).message}</p>)}
    {message && <p role={action.isError ? "alert" : "status"}>{message}</p>}
    {canWrite && <>
      <label className="block">Upload PDF datasheet
        <input type="file" accept="application/pdf,.pdf" disabled={action.isPending} onChange={event => {
          const file = event.target.files?.[0]; event.target.value = "";
          if (file) run(async () => { const doc = await api.uploadDatasheet(revision.id, file, revision.version); setDocumentId(doc.id); setJobId(""); resetSelection(); });
        }} />
      </label>
      {modelDocuments.data?.filter(d => !attached.has(d.id)).map(doc => <button key={doc.id} disabled={action.isPending}
        onClick={() => run(async () => { await api.attachDatasheet(revision.id, doc.id, revision.version); setDocumentId(doc.id); setJobId(""); resetSelection(); })}>
        Attach {doc.original_filename} (v{doc.version_number})
      </button>)}
    </>}
    <label className="block">Attached datasheet
      <select className="ml-2 bg-slate-800" value={documentId} onChange={e => { setDocumentId(e.target.value); setJobId(""); resetSelection(); }}>
        <option value="">Choose a datasheet</option>
        {documents.data?.map(doc => <option key={doc.id} value={doc.id}>{doc.original_filename} · v{doc.version_number} · scan: {doc.scan_status}</option>)}
      </select>
    </label>
    {canWrite && documentId && <button disabled={action.isPending} onClick={() => run(async () => { const next = await api.requestExtraction(documentId); setJobId(next.id); resetSelection(); })}>Extract datasheet</button>}
    {jobs.data && jobs.data.length > 0 && <label className="block">Extraction run
      <select className="ml-2 bg-slate-800" value={job?.id ?? ""} onChange={e => { setJobId(e.target.value); resetSelection(); }}>
        {jobs.data.map(j => <option key={j.id} value={j.id}>{j.status} · {j.is_current ? "current" : "history"} · {j.id.slice(0, 8)}</option>)}
      </select>
    </label>}
    {job && <div role="status">
      <p>Extraction status: {job.status} · {job.outcome} · Model resolution: {job.model_resolution}</p>
      {job.error_message && <p>{job.error_message}</p>}
      {job.warnings.map((warning, i) => <p key={i}>{warning.message ?? warning.code ?? JSON.stringify(warning)}</p>)}
      {canWrite && job.status === "failed" && <button disabled={action.isPending} onClick={() => run(() => api.retryExtraction(job.id))}>Retry extraction</button>}
    </div>}
    <div className="space-y-3">
      {job?.status === "completed" && candidates.data?.map(candidate => <CandidateCard key={candidate.id} candidate={candidate}
        busy={action.isPending} canReview={canWrite} canApply={canWrite && job.is_current && !["target_not_found", "ambiguous_target"].includes(job.model_resolution ?? "")}
        selected={selected.includes(candidate.id)} onSelect={(checked) => {
          if (selectionVersion === null) setSelectionVersion(revision.version);
          setSelected(ids => checked ? [...ids, candidate.id] : ids.filter(id => id !== candidate.id));
        }} onReview={(decision, note, confirmed) => run(() => api.reviewCandidate(candidate.id, decision, note, confirmed))} />)}
    </div>
    {canWrite && job?.is_current && <div className="space-y-2">
      <label className="block"><input type="checkbox" checked={overwrite} onChange={e => setOverwrite(e.target.checked)} /> I confirm replacing existing values in the selected fields.</label>
      <button disabled={action.isPending || selected.length === 0 || selected.length > 100 || selectionVersion !== revision.version} onClick={() => run(async () => {
        const result = await api.applyExtraction(revision.id, documentId, job.id, selected, overwrite, selectionVersion!);
        resetSelection(); setMessage(`Applied to draft version ${result.revision_version}.`);
      })}>Apply selected accepted values to draft</button>
      {selectionVersion !== null && selectionVersion !== revision.version && <p role="alert">The draft changed. <button onClick={() => { resetSelection(); onChanged(); }}>Reload draft and clear selection</button></p>}
    </div>}
    <details><summary>Application provenance ({history.data?.length ?? 0})</summary>
      {history.data?.map(entry => <div key={entry.id} className="my-2"><p>Draft version {entry.revision_version} · {entry.applied_at} · SHA256 {entry.document_sha256}</p>
        <pre className="overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(entry.candidates, null, 2)}</pre>
      </div>)}
    </details>
  </section>;
}

function CandidateCard({ candidate: c, busy, canReview, canApply, selected, onSelect, onReview }: {
  candidate: api.ExtractionCandidate; busy: boolean; canReview: boolean; canApply: boolean; selected: boolean;
  onSelect: (checked: boolean) => void; onReview: (decision: string, note: string, confirmed: boolean) => void;
}) {
  const [note, setNote] = useState(""); const [confirmed, setConfirmed] = useState(false);
  const airflow = c.field_key === "airflow_direction" && ["front-to-back", "side-to-side"].includes(c.value_text ?? "");
  const scalar = c.value_numeric !== null && c.value_text === null && !!c.unit && (c.field_key !== "heat_dissipation" || c.unit === "BTU/hr");
  const supported = SUPPORTED.has(c.field_key) && c.value_max === null && (airflow || scalar) && !c.flags.some(f => BLOCKING.has(f));
  return <article aria-label={`${c.field_key} candidate`} className="rounded border border-slate-600 p-3">
    <h3>{c.field_key} · {c.raw_value} {c.raw_unit} · {c.review_status}</h3>
    <p>Parsed: {c.value_text ?? c.value_numeric}{c.value_max !== null ? ` – ${c.value_max}` : ""} {c.unit} · Confidence: {Math.round(c.confidence * 100)}%</p>
    <p>Model: {c.model_context ?? "No model evidence"} · Attribution: {c.model_match}{c.model_attribution_confirmed ? " (confirmed)" : ""}</p>
    <p>Flags: {c.flags.join(", ") || "none"} · Conflict group: {c.conflict_group_key ?? "none"}</p>
    <blockquote>Page {c.page_number}: {c.source_text}</blockquote>
    {!supported && <p>Manual entry required: this candidate has an unsupported field, range, ambiguity or unresolved unit.</p>}
    {canReview && c.review_status === "pending" && <div className="space-y-2">
      <label className="block">Review note for {c.field_key}<input className="ml-2 bg-slate-800" value={note} maxLength={500} onChange={e => setNote(e.target.value)} /></label>
      {c.model_match === "unattributed" && <label className="block"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /> I confirm this value belongs to the target model.</label>}
      <button disabled={busy || c.model_match === "other" || (c.model_match === "unattributed" && !confirmed)} onClick={() => onReview("accepted", note, confirmed)}>Accept {c.field_key}</button>{" "}
      <button disabled={busy} onClick={() => onReview("rejected", note, false)}>Reject {c.field_key}</button>
    </div>}
    {c.review_note && <p>Review note: {c.review_note}</p>}
    {canApply && c.review_status === "accepted" && supported && <label className="block"><input type="checkbox" checked={selected} disabled={busy} onChange={e => onSelect(e.target.checked)} /> Select {c.field_key} for apply</label>}
  </article>;
}
