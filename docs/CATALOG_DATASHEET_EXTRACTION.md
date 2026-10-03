# Catalog datasheet extraction (DCIM01 PR-B)

A manufacturer PDF that PR-A stored is read, optionally OCR'd, and turned into **candidate
values with provenance**. A person reviews the candidates. Nothing in this feature writes to a
catalog revision: applying an accepted value to a draft revision is a separate, later change.

## Flow

1. `POST /api/v1/catalog/documents/{id}/extraction-jobs` records a job (idempotent) and queues it.
2. A Celery worker on the `extraction` queue claims the job, reads the stored object, checks its
   SHA-256 against the document row, and runs three sandboxed children:
   native text (`native_worker`), OCR for pages that need it (`ocr_worker`), analysis (`analysis_worker`).
3. Candidates and the job result are written in one transaction.
4. Administrators review each candidate (`accepted` or `rejected`). The decision is final and audited.

## Isolation

Every child gets: CPU and address-space rlimits, a file-size limit, no core dumps, its own session
(a timeout kills the whole process group), a bounded stdout read, a scrubbed environment (no database,
Redis or signing secrets), a blocked Python `socket` module, and two kernel policies installed without
privileges (so they work under Docker's default profile; a network or mount namespace would need `CAP_SYS_ADMIN`):

- **seccomp-bpf** denies `socket`, `socketpair`, `connect`, `bind`, `listen`, `accept`, `accept4`, `sendto`,
  `sendmsg`, `sendmmsg` and `io_uring_setup` with `EPERM`, and also `ptrace` and `process_vm_readv/writev`, so a
  child cannot inspect or patch its parent. The filter is inherited by `exec`, so the `tesseract` binary cannot
  open a connection either.
- **Landlock** is a filesystem policy. A child runs as the worker's UID, so environment scrubbing alone would
  leave `/proc/<ppid>/environ` (the worker's secrets) readable and the shared `/app/media` volume writable.
  The policy grants read and execute on the interpreter, system libraries, installed packages, the `app`
  package directory and the OCR engine's directory, and write only on the OCR child's private scratch
  directory (plus `/dev/null`). `/proc`, `/sys`, `/tmp`, the application root and the media volume are unreachable.

The children refuse to run when a policy cannot be installed: OCR reports `ocr_sandbox_unavailable`, the
native and analysis stages report `sandbox_unavailable` (the seccomp filter is required for OCR only, because
only OCR runs a native binary; Landlock is required for all three). `CATALOG_EXTRACTION_REQUIRE_LANDLOCK=false`
waives only the Landlock requirement, for a development host without it; it is not for production.

The PDF is treated as hostile again: the native and OCR children re-run the PR-A structural validation
(active content, encryption, object-stream scan, page cap) before reading anything, and count the pages
they actually iterate, because the page tree's declared count is attacker-controlled. OCR never renders
the page, so no content stream or action runs. It decodes only the largest page-level image, after
checking the declared pixel count against `CATALOG_OCR_MAX_IMAGE_PIXELS`. `/URI` links are not followed
and nothing remote is loaded. No LLM or external service is called.

OCR is used only for a page with fewer than `CATALOG_OCR_MIN_NATIVE_CHARS` (25) alphanumeric native
characters that carries an image. At most `CATALOG_OCR_MAX_PAGES` (20) pages per job are recognised; the
rest are reported as `ocr_page_limit` and the job is `partial`.

Operational tool: Tesseract 5 (Apache-2.0) from the Debian package, installed in the backend image.
Python dependencies are unchanged (pypdf, Pillow).

## Candidate model

Each row of `catalog_extraction_candidate` keeps:

| Column | Meaning |
|---|---|
| `field_key` | what was read, for example `power_typical_w`, `power_max_w`, `power_rated_w`, `power_idle_w`, `power_unspecified_w`, `psu_capacity_w`, `psu_quantity`, `heat_dissipation`, `airflow_volume`, `airflow_direction`, `width`, `height`, `depth`, `dimensions_unordered`, `weight`, `shipping_weight`, `rack_units`, `input_voltage`, `input_frequency`, `current_*`, `operating_temperature`, `storage_temperature`, `operating_humidity` |
| `value_numeric`, `value_max`, `value_text`, `unit` | the parsed value (a range uses both numbers) and the registry label of the unit |
| `raw_value`, `raw_unit` | exactly what the manufacturer wrote |
| `source_text`, `page_number` | the supporting line and its PDF page |
| `method` | `native`, `table` or `ocr` |
| `confidence` | 0 to 1; a heuristic, capped at 0.75 for OCR and lowered by each flag |
| `flags` | `conflict`, `unit_missing`, `unit_unrecognized`, `unit_dimension_mismatch`, `number_format_ambiguous`, `decimal_comma`, `axis_order_unspecified`, `semantics_unspecified`, `multi_value_line`, `range_value`, `list_of_values`, `ocr_low_confidence`, model-evidence flags |
| `model_context`, `model_match` | the model identifier the evidence names, and `target`, `other` or `unattributed` |
| `conflict_group_key` | candidates that disagree about the same field and model |
| review columns | `review_status`, reviewer, time, note, `model_attribution_confirmed` |

Rules the extractor follows:

- Missing stays missing: no value, unit or axis order is ever supplied. A number with no unit gets
  `unit_missing`. Three dimensions without a stated axis order become one `dimensions_unordered` candidate.
- No conversion: `1.2 kW` stays `1.2 kW`; `50-95 °F` stays Fahrenheit. The unit registry
  (`UNIT_REGISTRY_VERSION`) is extraction metadata only. It changes no database column and no capacity
  calculation. Everything architecture-dependent is deferred to the dimensional/unit decision.
- Power variants, PSU capacity, heat dissipation, airflow, net and shipping weight are separate fields,
  and a matching unit never merges them. "Standby power" and similar labels produce nothing.
  A bare "Power consumption" is kept as `power_unspecified_w`.
- Conflicts are flagged on every member of the group and never resolved. The same value repeated on
  two pages is evidence, not a conflict.

## Multi-model datasheets

Model evidence comes from table header rows (model names as columns), "Model: X" lines, "X Technical
Specifications" headings, and lines that are only a model token. Values are attributed to the column or
section they appear in. The target is matched against the catalog model's name and number (each word that
looks like a model token also counts), by exact normalised equality, so `CX-100` never matches `CX-1000`.

`model_resolution` on the job: `single_model_matched`, `multi_model_matched`, `target_not_found`,
`ambiguous_target`, `no_model_evidence`. Only `single_model_matched` and `multi_model_matched` produce
`target` candidates. Everything else is `unattributed` (or `other`) and cannot be accepted without a
person confirming the attribution. A value attributed to another model can never be accepted.
The candidate list hides `other` values unless `include_other_models=true` or `model_match=other` is given.

## Jobs, concurrency, retries

- One job per `(document, extractor_version)` (unique). A repeated request returns the existing job.
  A new extractor version adds a job; earlier jobs and candidates stay. The newest completed job is `is_current`.
- The lease is renewed between stages and every third of a lease *while a child runs*, so no stage outlives
  it whatever the timeouts are. A worker whose renewal finds the claim gone kills its child and writes nothing.
- Claim: one compare-and-set `UPDATE` (queued, or running with an expired lease) writes a fresh
  `claim_token`. Completion, failure and lease renewal are fenced on that token; a worker that lost its
  claim writes nothing (its candidate inserts are in the same transaction as the fenced update).
- `attempt_count` is bounded by `CATALOG_EXTRACTION_MAX_ATTEMPTS` (3); then the job fails with
  `max_attempts_exceeded`. `POST .../retry` re-queues a failed job with a fresh budget (once per failure).
- Dispatch is best effort after commit. A beat task (`requeue-stuck-catalog-extraction-jobs`, every 60 s)
  re-dispatches jobs queued for over two minutes or whose lease expired. Duplicate delivery is harmless.
- A completed job is immutable (trigger). A candidate is immutable apart from one review decision (trigger).
  Deleting an unattached document (retention purge) deletes its jobs and candidates.

## Failure handling

A job ends `completed` (`outcome` `complete` or `partial`) or `failed` with a fixed `error_code`
(`document_rejected`, `native_timeout`, `native_crashed`, `ocr_timeout`, `ocr_failed`,
`ocr_sandbox_unavailable`, `stored_object_missing`, `stored_object_mismatch`, ...). Responses carry a fixed
message per code. Exception text, child output, paths and document content are never stored in an error
or written to a log (logs carry ids, codes and counts). A page counts as OCR'd only if the engine read some text (a blank or illegible page is `ocr_no_text`). A document
that needed OCR, where no OCR page succeeded and nothing else was read, fails instead of reporting an empty
"completed" result.
The stored PDF and the catalog revisions are only ever read.

## Authorization

- Request, retry and review: `catalog:manage` plus the Administrator role.
- Reads: `catalog:document_download`, plus `catalog:read_draft` while the document is not linked to a
  published or retired revision (the file download rule, now one shared function). Jobs and candidates are
  reached through their document, so a direct ID never bypasses it. Unauthorised callers get 403 for real
  and made-up IDs alike.
- Users who hold permissions only through groups (restricted users) never hold `catalog:document_download`,
  so they cannot reach these routes.

## Operations

- Image: `tesseract-ocr` and `tesseract-ocr-eng` are installed in the runtime stage.
- Compose: the existing `celery-worker` also consumes the `extraction` queue. Extraction shares worker
  slots with the outbox dispatcher; on a single-CPU host, run a dedicated worker with `-Q extraction -c 1`.
- Deployment validation: `compose_smoke.py verify_ocr_sandbox` runs inside the worker container and checks
  the engine, the seccomp filter and the Landlock policy under the container's runtime profile, that a sandboxed
  child can neither create a socket nor read its parent's process environment, and one real OCR run through
  the pipeline.
- Settings: `CATALOG_EXTRACTION_*` and `CATALOG_OCR_*` in `app/core/config.py`.
