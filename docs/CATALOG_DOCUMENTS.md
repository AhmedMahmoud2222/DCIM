# Catalog datasheet documents (DCIM01 PDF import, PR-A)

Version: v2, 2026-09-30. Scope: secure upload, storage, versioning, attachment and download of
manufacturer datasheet PDFs. Extraction, OCR and the review screen arrive in later PRs
(see `docs/plans/DCIM01_PDF_DATASHEET_IMPORT_PLAN_v2.md`). Nothing here reads or converts a
physical measurement, so the database unit audit does not block it.

## Behavior

| Action | Endpoint | Permission |
|---|---|---|
| Stage or version a PDF | `POST /api/v1/catalog/documents[?catalog_model_id=]` | `catalog:manage` + Administrator |
| Upload and attach to a draft | `POST /api/v1/catalog/revisions/{id}/documents` (`If-Match`) | same |
| Attach / detach an existing document | `POST` / `DELETE /api/v1/catalog/revisions/{id}/documents/{doc}` (`If-Match`) | same |
| Version history of a model | `GET /api/v1/catalog/models/{id}/documents` | `catalog:read_draft` |
| Documents of a revision | `GET /api/v1/catalog/revisions/{id}/documents` | `catalog:read` (published), `catalog:read_draft` (draft) |
| Download the original | `GET /api/v1/catalog/documents/{id}/file` | `catalog:document_download`; documents not linked to a published revision also need `catalog:read_draft` |

`catalog:document_download` is granted to Administrator, DCIM Manager and Engineer by migration 0032.

## Versioning rule

A different PDF uploaded for the same model becomes `version_number + 1` and points at its
predecessor (`supersedes_document_id`). Identical bytes return the existing document. Links between
a document and a revision live in `catalog_revision_document`; a database trigger rejects any insert,
update or delete of a link on a published or retired revision. A newer datasheet therefore never
changes a published revision, its attached documents, or equipment instantiated from it. A revision
whose document has a successor reports `newer_version_id` (advisory only). Cloning a revision links the
same document rows; no bytes are copied.

## Upload pipeline

1. Read at most `CATALOG_DOCUMENT_MAX_BYTES + 1` bytes (default 25 MB); larger returns 413.
2. Validate in a child interpreter with CPU/memory limits and a 20 s timeout: `%PDF-` magic, end-of-file
   marker, parseable structure, not encrypted, at most `CATALOG_DOCUMENT_MAX_PAGES` (default 100), and no
   JavaScript, launch/submit/import actions, embedded files, XFA forms or rich media (byte scan with
   hex-escape normalization plus a structural walk). `/URI` links are allowed and stay inert: the child
   process has its sockets and DNS disabled, pypdf contains no network code, and clamd only scans the
   byte stream, so nothing dereferences a URL (tests bind a real listening socket to a `/URI` target and
   assert it never receives a connection).
3. Scan with ClamAV (`clamd` INSTREAM). `CATALOG_PDF_SCAN_MODE`:
   `required` (default) rejects the upload with 503 if the scanner is unreachable; `optional` records
   `scan_status=skipped`; `off` never scans. `ENVIRONMENT=production` refuses to start unless the mode is
   `required`. A detection is rejected with 422, written to the audit log, and nothing is stored.
   Reply handling is strict: the scanner accepts only one NUL-terminated line (`stream: OK` or
   `stream: <signature> FOUND`) followed by the daemon closing the connection, within 1024 bytes and the
   configured timeout in total. An `ERROR` line, an empty, truncated, oversized or multi-verdict reply, a reply
   that merely ends in `OK`, or a peer that never closes is "no verdict": `required` rejects the upload (503),
   `optional` records `skipped`, and no mode records it as clean.
4. Store as `<sha256>.pdf` via `StorageBackend` (`CATALOG_DOCUMENTS_STORAGE_ROOT`, default
   `media/catalog/documents`). The original filename is sanitized display text only.

Concurrency: uploads for one model serialize on the model row (version numbers stay consecutive and
identical bytes collapse to one document). Every upload also takes a transaction-level advisory lock on
the object's content hash, and the purge takes the same lock before deleting an object, so a purge can
never delete a file that a concurrent upload of the same bytes is about to reference. Each purge
candidate is re-checked for links and successors after it is locked, and the RESTRICT foreign keys are
the backstop.

Staged uploads that nobody attaches are deleted after `CATALOG_DOCUMENT_STAGING_RETENTION_DAYS` (default 14)
by a daily maintenance task; attached documents and predecessors of a newer version are never deleted.

## Operations

- `docker-compose.yml` adds a `clamav` service and a `catalog_media` volume mounted at `/app/media` in
  `backend` and `celery-worker`. Before this change the catalog graphics directory had no volume, so the
  files did not survive container replacement.
- The first ClamAV start downloads signatures (several minutes). Uploads return 503 until it is healthy.
- The Dockerfile creates `/app/media` owned by the `dcim` user. Existing deployments that already mounted
  another volume there need `chown -R dcim:dcim` on it.
- Object storage: implement `StorageBackend` (`save/read/exists/delete`) and return it from
  `get_document_storage_backend()`.

## Known limits

- The page count comes from the PDF page tree; PR-B re-checks the cap while rendering pages.
- ClamAV `StreamMaxLength` defaults to 25 MB, equal to the default upload cap. Raise both together.
- Storage writes happen before the request commits. A failed commit leaves an unreferenced,
  content-addressed object; it is harmless and is not yet swept.
- A PDF/ZIP-style polyglot whose foreign data sits within 1 KB after `%%EOF` is accepted. It is scanned,
  served only as `application/pdf` with `attachment` and `nosniff`, and never parsed as anything else.
- Retention deletions are logged (`catalog_document_purged`) but not written to the audit table, which
  requires an async session.
- The child process limits (15 s CPU, 1 GB address space, 20 s wall clock) apply to validation only.
  Extraction in PR-B must set its own limits and re-check the page cap while iterating pages.
