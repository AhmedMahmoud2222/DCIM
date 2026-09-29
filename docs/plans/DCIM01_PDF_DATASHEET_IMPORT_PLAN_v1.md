# DCIM01: Asset Catalog PDF Datasheet Import, Architecture and Implementation Plan

Version: v1 (2026-09-29). Status: **proposal, awaiting approval. No code written, nothing merged or deployed.**
Baseline: `main` at `f94f220` (Excel bulk import, PR #50). Open PR #53 (product roadmap) read for context.

## 1. Inspection findings

### 1.1 What exists and will be reused

| Area | Finding | Evidence |
|---|---|---|
| Catalog aggregate | `Manufacturer` > `CatalogModel` (identity) > `CatalogModelRevision` (draft, published, retired). Published revisions are immutable through a DB trigger (migration 0017). Drafts edit under `SELECT ... FOR UPDATE` plus an `If-Match` version. | `domain/catalog/designer_models.py`, `application/catalog_designer_service.py` |
| Physical columns | `dimension_unit` (`mm`\|`in`), `width/height/depth_value`, `weight_unit` (`kg`\|`lb`), `weight_value`, `rack_unit_height`, `airflow_direction` (enum), `rated_power_w`, `typical_power_w`, `max_power_w`, `heat_dissipation_btu_hr`, `power_redundancy_mode`. All nullable; required-ness is checked at publish. | same |
| PSU model | `PowerSupplyTemplate`: quantity, redundancy mode, connector_type (NOT NULL), voltage min/max, frequency, current, hot_swappable. No wattage column. | same |
| Unit conversion | One conversion point today: `_convert_physical()` runs at publish and turns the authored unit into the legacy integer mm/kg columns, rounding to whole units. | `catalog_designer_service.py:274` |
| Legacy bridge | Publish mints `rack_model_revision` / `equipment_model_revision` (integer mm/kg). Rack elevation, placement and instantiation read these. | migration 0020 |
| Instantiation | Copies ports and PSU templates into `EquipmentPort` / `EquipmentPowerInlet` / `PowerNode`. It never reads power, weight or heat figures. `Equipment.catalog_model_revision_id` already links to the rich revision. | `equipment_instantiation_service.py` |
| Capacity | `power_capacity.py` works on `PowerCapacity.rated_capacity_kw` of power nodes and telemetry-derived load. It reads nothing from the catalog. | `power_capacity.py` |
| Thermal | No thermal module exists. CFD and heat maps are roadmap R4. | PR #53 |
| RBAC | `require_catalog_administrator("catalog:manage")` requires the permission and Administrator role. Permissions are seeded by migration (0021) and guarded by `test_migration_permission_seed_parity.py`. | `application/rbac.py` |
| Storage | `StorageBackend` protocol (`save/read/exists`), content-addressed keys, local filesystem default, `get_storage_backend()` is the single construction point. | `infrastructure/storage/` |
| Upload discipline | Graphics: content sniff, size cap (10 MB), Pillow re-parse, sha256 key. Bulk import: XLSX magic bytes, 413/422 mapping. | `catalog_designer_service.py:477`, `bulk_import/upload.py` |
| Async work | Celery worker plus beat; jobs carry status, sanitized failure reasons, lease and attempt columns. | `infrastructure/tasks/`, migrations 0026-0030 |
| Audit and outbox | `_write_child_audit` plus outbox on every catalog mutation. | `catalog_designer.py:1483` |
| Frontend | React + TanStack Query. Catalog pages under `/admin/catalog/*`. `RevisionEditorPage` (802 lines) edits scalar fields, ports, PSUs, metrics. `GraphicsEditorPage` handles uploads. | `frontend/src/features/catalog-designer/` |
| Bulk import | The Excel catalog import writes the same draft columns and is the model for job/row/preview/commit semantics. | `bulk_import/` |
| Migration head | `0030_bulk_import_attempts`. Next revision: `0031`. | `backend/migrations/versions/` |

### 1.2 What does not exist

- No PDF, OCR or malware-scan dependency in `pyproject.toml`, the Dockerfile, compose or CI.
- No compose volume for `media/catalog/graphics`. Graphics written by the API container are not visible to the Celery worker and do not survive container replacement. PDFs need a shared, persistent volume, so this is a prerequisite (the roadmap lists "media persistence" under R2).
- No document or attachment table. `CatalogGraphic` is image-only (PNG/JPEG check constraint).
- No consumer of catalog power, heat or airflow values outside the catalog API itself.

### 1.3 The database unit audit

I found **no unit-audit document, issue or approved dimensional model** in the repository or on GitHub (issue search returned nothing). The only reference is PR #53's roadmap: R1 "Audit database units and physical-value storage. Architecture approval is required before implementation or migration", and R2 "standardize dimensional units, canonical telemetry registry, raw-reading preservation, conversion boundaries, precision and backward-compatible migrations".

Consequence: the "approved dimensional model" the task refers to does not exist yet. This plan therefore follows the **conventions already in code** and isolates every audit-dependent choice behind one registry file (section 4.4). Please confirm whether an audit draft exists outside the repository.

Existing-convention observations the audit should know about (not fixed here):
1. Unit-selector columns (`dimension_unit`, `weight_unit`) store the authored unit; conversion to mm/kg happens once, at publish.
2. Legacy tables round to whole mm/kg, which loses precision (a 482.6 mm width becomes 483).
3. Power is watts, heat is BTU/hr in the revision; capacity nodes use kW. Nothing links them.
4. `monitoring_metric_template.unit` is free text.

## 2. Scope and principles

1. **Do not modify existing physical columns or their types.** No migration touches `width_value`, `weight_value`, the power columns or the legacy tables.
2. **Approved values land in existing columns using the existing unit selectors.** A source of `19 in` writes `dimension_unit='in'`, `width_value=19`, with no conversion. A source unit outside `mm/in/kg/lb` (cm, g, oz) converts through the registry to `mm` or `kg` with exact factors, and the original is kept.
3. **New attributes** (voltage, current, airflow volume, operating limits, and similar) go in one new table, keyed by spec, storing original value and unit plus a canonical value produced by the registry.
4. **Never fabricate.** A missing value stays missing. No default, no inference from another field, no cross-derivation (for example BTU/hr from watts) unless the datasheet states it. Derived cross-checks appear as warnings only.
5. **Approval gate.** Nothing writes to the revision until an administrator approves a review session.
6. **Separate three meanings of power** in storage and API: `rated` (nameplate limit), `typical` (expected draw), `maximum` (peak). Telemetry is never stored in these tables.

## 3. Architecture

```
Browser --upload--> API (validate, quarantine key, DB row: scan_status=pending)
                      |--> Celery "documents" queue: scan (ClamAV) --> pdf_inspect --> extract --> persist run/candidates/fields
Browser <--review--  API (read run, edit fields, resolve flags)
Browser --approve--> API (one transaction: lock draft revision, write columns/PSUs/specs, mark run approved, audit, outbox)
```

### 4.1 Components

| Component | Location | Notes |
|---|---|---|
| Document service | `application/catalog_documents/service.py` | validation, storage, scan orchestration, attach/detach, download authorization |
| PDF validator | `.../pdf_validation.py` | magic bytes `%PDF-`, size cap, page cap, parse with `pypdf`, reject encrypted, reject `/JavaScript`, `/JS`, `/Launch`, `/OpenAction`, `/AA`, `/EmbeddedFiles`, `/URI` actions, XFA; decompressed-size guard |
| Scanner adapter | `.../malware_scan.py` | `ClamdScanner` (INSTREAM over TCP to a `clamav` sidecar), `NullScanner` for dev/test. Setting `catalog_pdf_scan_mode`: `required` (fail closed), `optional`, `off` |
| Extraction engine | `application/catalog_documents/extraction/` | `text_layer.py` (native text and tables), `ocr.py` (fallback), `parse_units.py`, `fields.py` (field library), `variants.py` (multi-model split), `psu.py`, `confidence.py`, `flags.py` |
| Worker task | `infrastructure/tasks/datasheet_extraction.py` | dedicated queue, hard time limit, runs extraction in a resource-limited subprocess |
| Registry | `application/units/registry.py` | the only place canonical units and factors live (section 4.4) |
| API | `api/v1/catalog_documents.py` | section 4.3 |
| Frontend | `features/catalog-designer/datasheet/` | upload control, status, review screen, history |
| Consumers | `application/catalog_specs.py` | read-only spec accessor for instantiation, elevation, capacity, thermal |

### 4.2 Schema (migration `0031_catalog_documents`, additive only)

| Table | Key columns |
|---|---|
| `catalog_document` | `id`, `catalog_model_revision_id` (nullable until approval, FK RESTRICT), `storage_key` (`<sha256>.pdf`), `sha256`, `original_filename`, `file_size_bytes`, `page_count`, `mime_type` (`application/pdf` check), `scan_status` (`pending\|clean\|infected\|error\|skipped`), `scan_engine`, `scan_signature_version`, `scanned_at`, `uploaded_by_user_id`, `uploaded_at`. Unique `(catalog_model_revision_id, sha256)`. |
| `catalog_extraction_run` | `id`, `document_id`, `run_number`, `status` (`queued\|running\|needs_review\|approved\|rejected\|failed`), `engine_name`, `engine_version`, `ocr_used`, `ocr_engine`, `ocr_mean_confidence`, `parameters` JSONB, `started_at`, `finished_at`, `failure_code` (sanitized), `supersedes_run_id`, `created_by_user_id`, `base_revision_version` (draft version the review was opened against). Append-only history. |
| `catalog_extraction_candidate` | `id`, `run_id`, `variant_label`, `page_range`, `target_catalog_model_id` (nullable), `target_revision_id` (nullable), `disposition` (`pending\|accepted\|discarded`). One row per model variant found. |
| `catalog_extraction_field` | `id`, `candidate_id`, `field_key`, `group_key` (for example `psu:1`), `raw_text`, `raw_value`, `raw_unit`, `normalized_value`, `normalized_unit`, `registry_version`, `confidence` (0-1), `method` (`text_table\|text_line\|ocr_table\|ocr_line`), `page`, `bbox` JSONB, `flags` text[] (`missing\|contradictory\|ambiguous\|low_confidence\|unit_unrecognized`), `alternatives` JSONB, then review columns: `review_action` (`accepted\|edited\|rejected\|manual`), `reviewed_value`, `reviewed_unit`, `reviewed_by_user_id`, `reviewed_at`, `reviewer_note`. Extracted columns never change after insert. |
| `catalog_revision_spec` | Approved values: `id`, `catalog_model_revision_id`, `spec_key`, `group_key`, `basis` (`rated\|typical\|maximum\|nominal\|limit_min\|limit_max`), `value_numeric`, `value_text`, `original_value`, `original_unit`, `canonical_value`, `canonical_unit`, `registry_version`, `source_field_id`, `source_document_id`, `source_page`, `approved_by_user_id`, `approved_at`. Draft-only writes and post-publish immutability by trigger, same pattern as migration 0017/0019. |

Also in 0031: `catalog:document_download` permission plus grants, and `test_migration_permission_seed_parity` update. `clone_revision` re-links documents and specs by reference (content-addressed keys mean no byte copy), matching the graphics clone behavior.

Revisions: a published revision cannot receive specs. Uploading to a published model creates or reuses a draft through the existing `clone` endpoint, then the datasheet attaches to the draft.

### 4.3 API (all under `/api/v1/catalog`)

| Method and path | Permission | Purpose |
|---|---|---|
| `POST /documents` (multipart) | `catalog:manage` + Administrator | Stage a PDF (used by model creation before a revision exists). Returns 202 with `document_id`, `run_id`. |
| `POST /revisions/{id}/documents` (multipart, `If-Match`) | same | Attach to a draft revision and queue extraction. |
| `GET /documents/{id}` | `catalog:read_draft` | Metadata, scan status. |
| `GET /documents/{id}/file` | `catalog:document_download` | `attachment`, `application/pdf`, `X-Content-Type-Options: nosniff`, audit entry. Refused unless `scan_status in (clean, skipped)`. |
| `GET /documents/{id}/pages/{n}/image` | `catalog:read_draft` | Server-rendered page PNG for the evidence panel (avoids shipping the raw PDF to a JS renderer). |
| `GET /revisions/{id}/documents` | `catalog:read` | List for a revision (published included). |
| `POST /documents/{id}/extractions` | `catalog:manage` + Administrator | Re-run extraction; new run, old runs kept. |
| `GET /extractions/{run_id}` | `catalog:read_draft` | Run, candidates, fields, flags. |
| `PATCH /extractions/{run_id}/fields/{field_id}` | `catalog:manage` + Administrator | Accept, edit, reject or enter manually. |
| `POST /extractions/{run_id}/candidates/{cid}/target` | same | Map a variant to a new model or an existing draft revision. |
| `POST /extractions/{run_id}/approve` (`If-Match`) | same | Atomic write, section 4.5. |
| `POST /extractions/{run_id}/reject` | same | Close without writing. |
| `GET /revisions/{id}/specs` | `catalog:read` | Approved specs with `basis` labels and provenance. |
| `GET /equipment/{id}/specifications` | `equipment:read` | Reads through `Equipment.catalog_model_revision_id`. |

All errors reuse `ApiError` and problem-detail mapping. Extraction failure reasons are generic strings, with details only in structured logs (same rule as floor-plan import).

### 4.4 Unit registry (single audit-dependent seam)

`application/units/registry.py` holds: recognized aliases per dimension (`mm, millimetre, cm, m, in, inch, ", ft`; `kg, g, lb, lbs, oz`; `W, kW, VA, kVA`; `BTU/hr, BTU/h, kBTU/hr`; `CFM, m3/h, m³/h, L/s`; `V, A, Hz`; `°C, °F, K`; `%RH`; `dBA`; `m, ft` for altitude), exact conversion factors, decimal precision, and a `REGISTRY_VERSION`. Rules:

- Conversions use `Decimal` with exact published factors (25.4 mm/in, 0.45359237 kg/lb, 3.412141633 BTU/hr per W, 1.699010796 m³/h per CFM). No float chains.
- Conversion happens **only** here. No other module multiplies by a factor. Existing `_convert_physical` stays as is until the audit lands, then delegates to the registry.
- Every stored value keeps the original number, unit string and page, so the audit outcome can re-derive canonical values without re-reading PDFs.
- An unknown unit yields flag `unit_unrecognized` and no normalized value.

Interim canonical choices (**pending audit approval**, isolated to this file): length mm, mass kg, power W, heat BTU/hr (matches the existing column), airflow m³/h, temperature °C, voltage V, current A, humidity %RH.

### 4.5 Extraction behavior

**Pipeline.** Validate, scan, inspect, then per page: native text and table extraction (`pdfplumber`/`pdfminer.six`). A page with fewer than N characters of text, or a document flagged image-only, goes to OCR (`ocrmypdf`/Tesseract, or `pdf2image` + `pytesseract`; poppler for rendering). Text and OCR results share one field parser.

**Field library** (each with synonyms, expected units, plausible ranges, and a target column or spec key): manufacturer, model, SKU/part number, width/height/depth, rack units, weight, rated/typical/maximum power, input voltage range, frequency, current, heat dissipation, airflow (volume and direction), operating temperature, humidity, altitude, PSU count, PSU wattage, PSU redundancy, connector type, hot-swap, noise. Values outside plausible ranges get `low_confidence`, never silent drops.

**Tables.** Header row detection maps column headers to variants. A table with model names as column headers yields one candidate per variant. Spanning cells, merged headers and footnote markers are handled explicitly, and footnotes stay attached as `raw_text`.

**Multi-model PDFs.** Variant segmentation uses table column headers first, then section headings, then repeated model-number patterns. If segmentation is uncertain the whole document becomes one candidate with every variant value marked `ambiguous` and the alternatives listed. The reviewer never receives a silently merged model.

**Multiple PSU configurations.** Each configuration is a `psu:N` group (count, wattage, voltage range, current, redundancy). Configurations map to `PowerSupplyTemplate` rows. `connector_type` is NOT NULL in the schema and datasheets often omit it, so approval requires the reviewer to enter it; the system will not guess `C14`.

**Flags.**
- `missing`: required-for-category field absent (the same rules as `validate_revision_for_publish`).
- `contradictory`: two sources on different pages disagree beyond tolerance (for example 1U on page 1, 2U on page 3), or a stated total differs from the sum.
- `ambiguous`: label without a basis ("Power consumption 350 W"), several variants unresolved, several values per voltage (10 A at 100 V, 5 A at 200 V), or a unit-less number.
- `low_confidence`: below the configurable threshold (default 0.75), or OCR confidence below 0.8.

**Confidence.** Product of: method base score (table cell with header match 0.95, labelled line 0.8), OCR word confidence when used, unit-recognized factor, plausibility factor. The formula is versioned in `engine_version` and stored per field.

**Approval transaction.**
1. Verify caller, run status `needs_review`, and that no `contradictory`, `ambiguous` or `missing`-required field lacks an explicit reviewer resolution.
2. Lock the target draft revision (`lock_draft_revision_for_edit`) and compare `If-Match` and `base_revision_version`. A version drift rejects with 409 and asks for re-review.
3. Create manufacturer/model/draft revision for candidates mapped to "new" (`allocate_revision_number`, uniqueness errors surface as 409).
4. Write accepted fields to existing columns, PSU groups to `PowerSupplyTemplate`, remaining specs to `catalog_revision_spec`.
5. Attach the document, mark run `approved`, write audit rows (field-level before/after) and outbox events, commit once.
6. Publishing stays a separate, existing step. Approval never publishes.

### 4.6 Consumers (requirement 8)

Values reach consumers through one accessor, `catalog_specs.get_nameplate(revision_id)`, returning typed figures labelled `basis` (`rated`, `typical`, `maximum`) plus `source='catalog_datasheet'`. Telemetry stays in the telemetry tables and response schemas never merge the two: a capacity payload carries `nameplate_*` fields separate from `actual_*` fields.

| Consumer | Change |
|---|---|
| Asset instantiation | Response of `GET /equipment/{id}/specifications` exposes the linked revision's approved specs. No copy onto `Equipment` (no schema change, follows the "snapshot" rule only for ports/inlets). |
| Rack elevation | Already reads `rack_unit_height` through the bridge. Add weight and airflow to the tooltip payload only. No layout change. |
| Capacity planning | Add a read-only `planned_nameplate_kw` per rack, summed from `rated` power, shown beside (never added to) allocated and actual figures. **Blocked by audit** (W vs kW, rounding, redundancy double-counting). |
| Thermal analysis | No module exists. The accessor provides heat dissipation and airflow so R4 can consume it. Nothing wired in this work. |

## 5. Security risks and mitigations

| Risk | Mitigation |
|---|---|
| Malicious PDF (JavaScript, launch actions, embedded files, exploit payloads) | Structural rejection list, page and size caps, never render or execute in the API process, scan before extraction, download served as attachment with `nosniff`. |
| Parser exploits and decompression bombs | Extraction in a subprocess with CPU/memory rlimits, wall-clock timeout, page cap (default 100), decompressed-stream cap, Celery hard time limit. Worker runs non-root (already the image default). |
| Malware not detected | ClamAV sidecar, `required` mode fails closed in production; `optional`/`off` are visible in the document record (`scan_status='skipped'`) and in the review screen. |
| Path traversal / key injection | Server-generated `<sha256>.pdf` keys only. `LocalFileSystemStorageBackend._safe_path` already rejects separators. |
| Stored XSS via extracted text | Extracted strings are data, rendered as React text, length-capped, control characters stripped, never HTML. |
| Prompt-injection / exfiltration through LLM extraction | Default engine is rule-based and local. Any LLM engine stays behind a disabled feature flag pending owner decision (section 8). |
| Data leakage of licensed datasheets | Separate `catalog:document_download` permission, audit on every download, no public URLs. |
| Privilege escalation | Mutations require permission and Administrator role, same as all catalog writes. Test matrix covers all five seeded roles. |
| Storage exhaustion / DoS | Size cap (default 25 MB), per-user rate limit and pending-job cap, page cap, orphan-staging cleanup after N days via beat. |
| Cross-container storage mismatch | Shared named volume mounted in `backend` and `celery-worker`, or object storage behind the existing protocol. |
| Race on approval | Row lock plus `If-Match`, unique constraints as backstop (same pattern as existing concurrency tests). |
| Log leakage | Failure logs omit document text and filenames (SEC-07 discipline). |

## 6. Test strategy

Fixtures are generated in tests (reportlab for native PDFs; render-to-image-to-PDF for scanned ones). No vendor datasheets are committed, which avoids copyright exposure.

| Suite | Cases |
|---|---|
| Unit: parsing | unit alias recognition, `19"`, `1U = 44.45 mm`, `BTU/hr`, `m³/h`, ranges (`100-240 V`), footnote markers, locale decimals |
| Unit: conversion | exact factors, Decimal precision, round trip, unknown unit, no conversion for `mm/in/kg/lb` authored values, registry version stamped |
| Unit: extraction | native table, native text, OCR fallback (Tesseract required in CI), mixed pages, empty PDF, image-only PDF |
| Unit: multi-model | 3-variant table, section-per-model, unresolved segmentation falls back to ambiguous single candidate, duplicate variant labels |
| Unit: PSU | 1, 2, 4 PSU configs, `1+1` vs `n+1`, voltage-dependent current |
| Unit: flags | missing, contradictory across pages, ambiguous power basis, low confidence, out-of-range |
| Unit: never-fabricate | absent field stays null; no BTU derived from watts; connector type never defaulted |
| API: access control | all five roles x every endpoint; Administrator without permission; download permission; cross-revision access |
| API: file validation | wrong magic, oversize (413), zero-byte, encrypted, JS action, embedded file, truncated PDF, polyglot, extension/content-type mismatch, decompression bomb, page-cap breach |
| API: scanning | clean, infected (quarantined, download 409), scanner down (fail closed in `required`) |
| API: approval | requires all blocking flags resolved; writes existing columns with correct unit selectors; stale `If-Match` returns 409; approval on published revision rejected; audit and outbox rows written; rollback leaves nothing |
| API: version history | re-extraction keeps old runs; clone re-links documents and specs; published revision immutable (trigger) |
| Integration: concurrency | two approvals, approval vs draft edit, approval vs publish |
| Integration: schema | migration 0031 up/down, trigger guards, check constraints, permission-seed parity |
| Regression | Excel bulk import unchanged, graphics upload unchanged, publish bridge output byte-identical for revisions without datasheets, existing `_convert_physical` results unchanged |
| Frontend (vitest) | upload states, review table flags, approve disabled until resolved, variant tabs, keyboard and ARIA, error states |
| E2E (Playwright) | upload, review, edit, approve, publish, instantiate; download authorized and denied |
| CI | add Tesseract/poppler to backend CI image; ClamAV service container for scan tests (or mocked clamd protocol server for unit level) |

## 7. Delivery slices (each an independent PR, none merged without your authorization)

| Slice | Content | Depends on audit? |
|---|---|---|
| PR-A | Shared storage volume, settings, PDF validator, scanner adapter, `catalog_document` table, attach/list/download endpoints, permission migration, tests | No |
| PR-B | Unit registry (interim), extraction engine, worker task, run/candidate/field tables, fixtures and tests | Registry canonical choices only |
| PR-C | Review and approval API, `catalog_revision_spec`, audit/outbox, concurrency tests | Partly (see section 8) |
| PR-D | Frontend: upload control in model creation and editing, review screen, history | No |
| PR-E | Consumer accessors, equipment specifications endpoint, elevation tooltip payload | Capacity item blocked |

## 8. Decisions blocked by the database unit audit

| # | Decision | Interim behavior in this plan |
|---|---|---|
| B1 | Canonical unit per dimension (length, mass, power, heat, airflow, temperature) | Registry defaults in 4.4, one file, versioned |
| B2 | Precision and rounding (existing `Numeric(10,3)` and integer legacy rounding) | Store original and Decimal-exact normalized values; no rounding beyond the existing column scales |
| B3 | Whether to migrate physical columns to a single canonical unit | **Not done.** No migration of existing columns |
| B4 | Whether heat stays BTU/hr or moves to W | Keep BTU/hr; store stated unit in `catalog_revision_spec` too |
| B5 | Capacity-planning unit (W vs kW) and redundancy treatment of nameplate power | `planned_nameplate_kw` in PR-E deferred |
| B6 | Fate of legacy integer mm/kg rounding at publish | Untouched; flagged for the audit |
| B7 | Telemetry unit registry (metric template `unit`) | Out of scope; no linkage between datasheet units and telemetry units |
| B8 | Re-deriving canonical values for already-approved specs after the audit | Supported by stored originals plus `registry_version`; a backfill job is a follow-up |

## 9. Decisions needed from you

1. Confirm the unit-audit status and whether an approved model exists outside the repo.
2. Extraction engine: rule-based local only (recommended), or also an optional LLM engine behind a flag. If LLM, which provider and data-handling terms?
3. OCR stack: Tesseract in the backend image (adds roughly 100-200 MB), or a separate `ocr` worker image.
4. Malware scanning: ClamAV sidecar with fail-closed in production (recommended), or optional.
5. Storage: shared named Docker volume now, or object storage behind the existing protocol.
6. Limits: 25 MB file, 100 pages, staging retention of unapproved uploads (proposed 14 days).
7. Download permission: new `catalog:document_download` for Administrator, DCIM Manager and Engineer (proposed), versus reusing `catalog:read`.
8. Licensing: `pypdf` (BSD), `pdfplumber`/`pdfminer.six` (MIT), `pytesseract`/Tesseract (Apache-2.0). I propose to avoid PyMuPDF (AGPL). Confirm.
9. Page evidence: server-rendered page images (recommended) or a client-side PDF.js viewer.

## 10. Acceptance criteria

1. An administrator uploads a valid PDF from model creation and from a draft revision's editor; other roles cannot.
2. Invalid, oversize, encrypted, active-content or infected files are rejected or quarantined with a clean problem-detail response and no stored object usable for download.
3. Native and scanned fixtures produce candidates; tables and multi-model PDFs produce one candidate per variant; multi-PSU PDFs produce one group per configuration.
4. Every extracted field stores original text, value and unit, page, method, confidence and flags. Re-extraction adds a run and keeps history.
5. The review screen shows missing, contradictory, ambiguous and low-confidence flags; approval is impossible while any blocking flag is unresolved; no field is ever prefilled with an invented value.
6. Approval writes only to existing columns through the existing unit selectors, plus the new spec and PSU rows, in one transaction with audit and outbox rows. Publish remains a separate action.
7. Existing physical columns, legacy tables and `_convert_physical` output are unchanged; a diff shows no migration touching them.
8. The original PDF stays attached to the revision, survives clone, and downloads only for authorized users with an audit entry.
9. Specification responses label `rated`, `typical` and `maximum` distinctly and never mix them with telemetry.
10. All suites in section 6 pass in CI, ruff and mypy are clean, the frontend passes lint, typecheck and vitest, and the permission-seed parity test passes.
11. No merge and no deployment occurs without your explicit authorization.
