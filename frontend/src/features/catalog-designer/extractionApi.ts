import { apiFetch } from "@/lib/apiClient";

export interface Datasheet {
  id: string; original_filename: string; version_number: number; scan_status: string;
  sha256: string; revision_version?: number;
}
export interface ExtractionJob {
  id: string; status: string; is_current: boolean; model_resolution: string | null;
  outcome: string | null; error_message: string | null; candidate_count: number | null;
  warnings: { code?: string; message?: string }[];
}
export interface ExtractionCandidate {
  id: string; field_key: string; value_numeric: number | null; value_max: number | null;
  value_text: string | null; unit: string | null; raw_value: string; raw_unit: string;
  source_text: string; page_number: number; confidence: number; flags: string[];
  model_context: string | null; model_match: string; conflict_group_key: string | null;
  review_status: string; model_attribution_confirmed: boolean; review_note: string | null;
}
export interface ExtractionApplication {
  id: string; revision_version: number; applied_at: string; document_sha256: string;
  after_values: Record<string, string>; candidates: Record<string, unknown>[];
}

export const listDatasheets = (revisionId: string) => apiFetch<Datasheet[]>(`/catalog/revisions/${revisionId}/documents`);
export const uploadDatasheet = (revisionId: string, file: File, version: number) => {
  const body = new FormData(); body.append("file", file);
  return apiFetch<Datasheet>(`/catalog/revisions/${revisionId}/documents`, { method: "POST", body, ifMatch: version });
};
export const listModelDatasheets = (modelId: string) => apiFetch<Datasheet[]>(`/catalog/models/${modelId}/documents`);
export const attachDatasheet = (revisionId: string, documentId: string, version: number) =>
  apiFetch<Datasheet>(`/catalog/revisions/${revisionId}/documents/${documentId}`, { method: "POST", ifMatch: version });
export const listExtractionJobs = (documentId: string) => apiFetch<ExtractionJob[]>(`/catalog/documents/${documentId}/extraction-jobs`);
export const requestExtraction = (documentId: string) =>
  apiFetch<ExtractionJob>(`/catalog/documents/${documentId}/extraction-jobs`, { method: "POST" });
export const retryExtraction = (jobId: string) => apiFetch<ExtractionJob>(`/catalog/extraction-jobs/${jobId}/retry`, { method: "POST" });
export const listCandidates = async (jobId: string) => {
  const result: ExtractionCandidate[] = [];
  for (let offset = 0; ; offset += 500) {
    const page = await apiFetch<ExtractionCandidate[]>(`/catalog/extraction-jobs/${jobId}/candidates?include_other_models=true&limit=500&offset=${offset}`);
    result.push(...page);
    if (page.length < 500) return result;
  }
};
export const reviewCandidate = (id: string, decision: string, note: string, confirm: boolean) =>
  apiFetch<ExtractionCandidate>(`/catalog/extraction-candidates/${id}/review`, {
    method: "POST", body: JSON.stringify({ decision, note: note || null, confirm_model_attribution: confirm }),
  });
export const applyExtraction = (revisionId: string, documentId: string, jobId: string, ids: string[], overwrite: boolean, version: number) =>
  apiFetch<ExtractionApplication>(`/catalog/revisions/${revisionId}/extraction-applications`, {
    method: "POST", ifMatch: version,
    body: JSON.stringify({ document_id: documentId, job_id: jobId, candidate_ids: ids, overwrite_existing: overwrite }),
  });
export const listApplications = (revisionId: string) =>
  apiFetch<ExtractionApplication[]>(`/catalog/revisions/${revisionId}/extraction-applications`);
