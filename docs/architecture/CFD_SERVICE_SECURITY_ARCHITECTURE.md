# CFD service, data and security architecture

Document version: 0.1.0
Status: PROPOSED design. No code, migration or API exists. CFD stays disabled behind a flag until M5 is accepted.

## 1. Fit with the existing platform

| Existing piece | Evidence | Reuse |
|---|---|---|
| FastAPI, async SQLAlchemy, PostgreSQL | `backend/app/api/v1/*`, `backend/app/db` | API and system of record |
| Celery on Redis with named queues | `backend/app/infrastructure/celery_app.py` (queues: default, maintenance, polling, telemetry, alarms, imports, reports, notifications, high_priority, extraction) | add a `cfd` queue and routing |
| Transactional outbox | `backend/app/domain/outbox/models.py`, `infrastructure/tasks/outbox_dispatcher.py` (SKIP LOCKED claim, stale `processing` reclaim after 5 minutes, max 5 attempts). The handler today only logs and marks `dispatched` | reuse the table and claim pattern; add a CFD handler that enqueues to the `cfd` queue |
| Lease and attempt pattern | `bulk_import` models: `commit_lease_id`, `commit_lease_expires_at`, `commit_attempt_count`; requeue sweeper in the Celery beat schedule | copy the pattern for run attempts |
| Parser sandbox | `application/spatial_import/runner.py`, `limits.py`, `worker.py`: scrubbed environment, rlimits, seccomp, Landlock, own process group, fail-closed | design reference only. It runs a Python parser, not a native solver. The solver runs in a container |
| Storage abstraction | `infrastructure/storage/backend.py` (`save/read/exists/delete/iter_keys`), `local.py` | artifact bytes, content-addressed keys |
| Audit | `application/audit_service.py`, `domain/audit` | every state change and download |
| RBAC | `application/rbac.py`, `access_control.py`, SEC-ARCH-02 scope enforcement (#170 on `main`) | new `cfd:*` permissions |

## 2. Entities (additive; to be created in M4, no migration now)

All ids are UUIDs. All tables are insert-mostly with immutable columns guarded by triggers, as `floor_plan_calibration` is.

| Entity | Purpose | Key columns |
|---|---|---|
| `cfd_model_revision` | Immutable snapshot of the baseline contract inputs for a room | `room_id`, `revision_number`, `input_document_key` (storage), `content_hash`, `contract_version`, `assembled_by`, `assembled_at`, `source_snapshot` (calibration id, plan revision, placement refs) |
| `cfd_scenario_revision` | Immutable delta set over a model revision | `model_revision_id`, `deltas` JSONB, `content_hash`, `created_by`. Contains no foreign key that lets it write inventory |
| `cfd_run` | A requested simulation. Holds the four state machines | `model_revision_id`, `scenario_revision_id`, `requested_by`, `site_id`, `room_id`, `execution_state`, `numerical_state`, `validation_state`, `acceptance_state`, `idempotency_key`, `policy_versions`, `created_at` |
| `cfd_run_attempt` | One execution try | `run_id`, `attempt_number`, `lease_id`, `lease_expires_at`, `heartbeat_at`, `worker_id`, `image_digest`, `started_at`, `ended_at`, `exit_reason`, `resource_limits`, `resource_usage` |
| `cfd_artifact` | Checksummed output or log | `run_id`, `attempt_id`, `kind`, `storage_key`, `sha256`, `size_bytes`, `created_at`, `retention_until`, `scope_site_id` |
| `cfd_sensor_validation` | One validation assessment of a run | `run_id`, `dataset_hash`, `window`, `split`, `metrics` JSONB, `coverage`, `verdict` (`not_run`, `insufficient_data`, `passed`, `failed`), `assessed_by`, `policy_version` |
| `cfd_engineering_acceptance` | Human decision | `run_id`, `decision` (`accepted`, `rejected`, `revoked`), `reason`, `decided_by`, `decided_at`, `scope_text`, `expires_at` |

Reuse of the existing `outbox_event` for dispatch: `aggregate_type = "cfd_run"`, `event_type = "cfd.run.queued"`.

## 3. The four independent states

The values below are separate columns with separate transition rules. A change in one never implies a change in another.

| State | Values | Set by |
|---|---|---|
| Execution | `queued`, `leased`, `meshing`, `solving`, `postprocessing`, `succeeded`, `failed`, `cancel_requested`, `cancelled`, `timed_out`, `lost` | worker, sweeper, user cancel |
| Numerical | `not_assessed`, `converged`, `not_converged`, `diverged`, `rejected_mesh`, `rejected_balance`, `rejected_other` | postprocessing checks defined in the V&V policy |
| Measurement validation | `not_run`, `insufficient_data`, `passed`, `failed` | validation service (`cfd_sensor_validation`) |
| Engineering acceptance | `none`, `pending`, `accepted`, `rejected`, `revoked` | a person with `cfd:accept` |

Rules: `numerical_state = converged` requires `execution_state = succeeded`. `validation_state = passed` requires
`numerical_state = converged` and a stored assessment. `acceptance_state = accepted` requires `validation_state = passed`
unless the acceptance carries the explicit scope "not measurement validated" and the UI repeats that text. A report may use the
word "validated" only when `validation_state = passed` and `acceptance_state = accepted`. These rules are CHECK constraints
plus service checks. Every other combination is displayed as its own labelled status.

## 4. Atomic dispatch

1. The API request validates authorization and scope, assembles or loads the contract, and in one transaction inserts
   `cfd_run` (execution `queued`) and an `outbox_event` row. Either both exist or neither.
2. The outbox dispatcher claims events with `FOR UPDATE SKIP LOCKED` as today and enqueues `run_cfd_attempt(run_id)` on queue
   `cfd` (name proposed: `cfd`). Redis messages carry the run id only; the database holds all inputs.
3. The worker claims the run in one statement: `UPDATE cfd_run_attempt ... WHERE run_id = :id AND execution_state = 'queued'`
   inserting an attempt row with a fresh `lease_id` and `lease_expires_at`. Zero rows updated means another worker or
   a duplicate delivery owns it; the task exits without side effects.
4. The worker renews the lease with a heartbeat at most every 30 seconds (value to be set in M4). Each state write includes
   `WHERE lease_id = :lease_id` (fencing); a worker that lost its lease cannot write results.
5. A sweeper (as `requeue-stuck-bulk-import-commits` does) marks attempts with an expired lease as `lost` after killing the
   container by name, and requeues up to the attempt cap; after the cap the run is `failed` with reason `worker_lost`.
6. Duplicate delivery, redelivery after a Redis loss, and re-dispatch after an outbox reclaim all pass through step 3, so they
   cannot run the same attempt twice. Artifacts are written to attempt-scoped keys and registered only by the lease holder.

## 5. Compute isolation

A separate queue only stops CFD tasks from sharing a worker pool. It does not stop a solver from taking memory or CPU on
the host. The design requires all of the following.

| Control | Requirement |
|---|---|
| Worker placement | Dedicated CFD worker nodes or a dedicated container runtime pool with its own cgroup parent; no co-location with API, database, Redis or ingest workers. If hardware is shared, hard cgroup quotas apply |
| CPU | `--cpus` and a fixed thread count in the case; MPI rank count fixed by policy |
| Memory | cgroup memory limit plus swap disabled (`memory-swap` equal to `memory`); OOM kill is an execution failure `resource_limit` |
| Processes | pids limit; no process outside the container's PID namespace |
| Disk | per-attempt writable scratch on a size-limited volume or quota; read-only root filesystem; bounded artifact total; checks during meshing and during postprocessing, not only at the end |
| Mesh size | `max_cells` checked after mesh generation and before solving; a refinement level above the cap is rejected |
| Wall clock | per phase caps (meshing, solving, postprocessing) and an overall cap; a watchdog outside the container enforces them |
| Output | log size cap; field write interval and number of written time directories capped |
| Concurrency | per-site and per-user limits on `queued + running` runs and a global CFD worker slot count; rejected submissions return 429 with the limit named |
| Queue limits | maximum queue depth; submissions beyond it are refused, not buffered |
| Container | non-root user with no capabilities (`--cap-drop=ALL`), `no-new-privileges`, seccomp profile, read-only root, no host mounts, no Docker socket, no devices, no privileged mode, no host PID/IPC/network |
| Network | none (`--network none`); the worker copies the case in and the results out through the orchestrating process |
| Filesystem paths | the case directory is created by the server under a per-attempt path; user input never selects a path; artifact names are generated |
| Environment | scrubbed; no database, Redis, JWT or integration secrets reach the container |
| Image | digest-pinned, from the project registry, scanned before use |

Cancellation: the orchestrator holds the container id. Cancel sends a termination to the container's whole process group,
waits a grace period, then removes the container. The same code path serves timeout, lease loss and user cancel. A test starts a
child that forks and verifies no process survives.

## 6. Case templates and unsafe directives

1. Templates live in the repository and the image, versioned and hashed. Users supply values only through the validated input
   document. No field of the input document is a string that is spliced into a dictionary without escaping against a closed
   grammar.
2. The generator emits only dictionary entries from an allow-list. Rejected tokens in any generated or template file:
   `codeStream`, `#codeStream`, `#calc`, `#include` and `#includeEtc` with paths outside the image tree, `#inputMode` changes,
   `libs` entries outside the allow-list, `functions` entries outside the allow-list, `systemCall`/`exec`/`system` function
   objects, `sysFs`, absolute or `..` paths, symbolic links, shell metacharacters in names.
3. The executable list is fixed (ADR-0013). The orchestrator calls each by absolute path with arguments built from constants
   plus validated integers.
4. A pre-flight scanner re-reads the generated case tree and refuses to run if any rejected token appears.
5. Tests: a corpus of hostile identifiers (names containing newlines, braces, `#calc`, path traversal) must produce a refusal
   or a safely escaped name, never a changed dictionary.

## 7. Authorization and scope

New permission codes (proposed): `cfd:read`, `cfd:run`, `cfd:manage_model`, `cfd:accept`, `cfd:download`.

1. Every endpoint checks the code and then applies site and room scope. Existing precedents: `cooling:*` is in the scope-aware
   set and filters in SQL; `spatial:read` is not, and a site-restricted user receives 403 (see `ISSUE_104_SPATIAL_DIGITAL_TWIN.md`
   "Authorization"). CFD follows the stricter result.
2. A run, model revision or scenario concerns a whole room. A caller who cannot see every rack, cooling unit and sensor in the
   room gets a refusal (`scope_partial_visibility`), never a model built from the visible subset. The assembler runs with the
   caller's scope and compares the count of visible placed assets with an unscoped count computed server-side without
   returning it. If the counts differ, the response is the same 403/404 shape as an unknown room, which leaks neither the
   existence nor the number of hidden assets.
3. Results (fields, probes, sensor comparisons, reports) only expose ids the caller may see. Aggregates (maximum
   temperature, heat removed) cover the whole room, which is allowed only because the caller has whole-room visibility.
4. Download of artifacts needs `cfd:download` and whole-room visibility at download time (not only at run time). Signed URLs
   are not used; the API streams bytes after checking authorization, verifies the SHA-256, and writes an audit record.
5. Authorization revocation: running attempts re-check the requester's permission at phase boundaries (before meshing, before
   solving, before artifact registration). If the permission or scope is gone, the attempt is cancelled with reason
   `authorization_revoked`, and its artifacts are retained under the retention rule but are not downloadable by that user.
6. Scenarios and runs never write to inventory, placement, calibration, catalog, cooling or alarm tables. The CFD service role
   has SELECT on the source tables and INSERT/UPDATE only on `cfd_*` tables. A test enumerates foreign keys from `cfd_*` tables
   and asserts that none point from an authoritative table to a CFD table.
7. Audit records (existing audit service): submission, dispatch, lease acquire/loss, cancel, phase transitions, validation
   assessment, acceptance, revocation, every artifact download, every denied request.

## 8. Retention and cleanup

1. Scratch directories are deleted when an attempt ends, success or not. A sweeper deletes orphan scratch older than a bound.
2. Artifacts have `retention_until`. Defaults are policy values to be set by the owner: inputs and reports are kept with the
   run; large field files are kept for a shorter, configurable period; logs are capped. The orphan sweep uses
   `iter_keys` as the existing catalog document purge does.
3. A run row is never deleted while an acceptance record references it. Deleting a site or room is refused while CFD runs
   exist (RESTRICT foreign keys).
4. Disk accounting per site is part of the concurrency limit.

## 9. Observability

Metrics: queue depth, run durations by phase, kill reasons, mesh cells, memory high-water, rejected runs by reason. Alerts for
lost workers and for lease-expiry rates. Logs are stored as artifacts with size caps and never contain secrets.

## 10. Tests required before M4 acceptance

Atomic dispatch under a crash between insert and enqueue; duplicate delivery; lease expiry with a live worker (fencing);
worker death during each phase; cancel of a process tree; resource-cap rejections for cells, disk, memory, wall clock;
per-user and per-site concurrency; hostile template identifiers; hidden-asset and partial-scope refusal; download after
revocation; artifact checksum mismatch; state-machine illegal combinations rejected by the database.
