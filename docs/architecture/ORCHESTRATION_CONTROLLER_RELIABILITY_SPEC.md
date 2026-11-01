# Orchestration Controller Reliability Specification

Status: **DRAFT specification. Not reviewed. Not accepted.** No implementation, test or CI result is asserted by this document.

- Authored by: Claude (patch delivery, DX12). Applied by: the orchestrator.
- Reference baseline: `main@69c60af3360fcfea4401080c1b9276ffd418e4d5`.
- Subject: controller reliability work in PR #219 (frozen reviewed head `ab3f9641d379bfe52eaaaf8bc55c3fb49c897acc`).
- Authority: owner decision D5 "specification first" (issue #220). PR #219 stays review-only until Codex and owner-run Jules have reviewed this specification.
- Production execution and activation are **not** authorized by this document.

The specification is deliberately independent of the current implementation. It uses the keywords MUST, MUST NOT and SHOULD in the RFC 2119 sense. Names such as "journal" or "store" denote roles, not code identifiers.

## 1. Scope and vocabulary

| Term | Meaning |
| --- | --- |
| Journal | The single signed, versioned document that is the controller's only durable state. |
| Task | One unit of orchestration work, identified by a stable `task_id`. |
| Store | The remote object holding the journal. It supports read (returning content and a version token) and conditional write (PUT with expected version token). |
| Version token | The Store's opaque revision identifier (blob SHA or equivalent). |
| Delivery | A single non-idempotent POST (for example a comment or dispatch) to an external system. |
| Evidence | Workflow-run artifacts or API facts proving a delivery outcome or a review verdict. |
| Run window | One workflow execution, limited to 8 minutes wall-clock. |
| Reconciliation | Reading the Store and external systems to decide whether an ambiguous operation took effect. |

## 2. Invariants

Each invariant has an ID used by the test matrix (section 7).

### 2.1 Signed journal persistence
- **INV-S1** Every journal write carries a signature over the canonical serialization, bound to the journal schema version and a monotonic `generation` counter. The signature covers all task records.
- **INV-S2** A journal that fails signature, schema or generation validation MUST be rejected whole. The controller MUST NOT act on partially valid content and MUST NOT overwrite it (fail closed, escalate to a human).
- **INV-S3** `generation` MUST strictly increase by exactly one per successful write. A read returning a lower generation than one previously observed in this run MUST be treated as stale (see INV-C3).
- **INV-S4** (cross-run non-rewindable history) The controller MUST verify every journal read against an independently trusted monotonic checkpoint `(generation, journal_digest)` held outside the Store's writable path, in a medium that the Store write credential cannot rewrite (candidate media and the safe default are in D-8). Each journal records `prev_digest` (the digest of the journal at `generation - 1`). A journal with `generation` lower than the checkpoint's, or equal to it with a different digest, or whose `prev_digest` chain does not reach the checkpoint, MUST be refused before any PUT, any POST, and any mutation. This holds across processes and restarts; run-local memory (INV-S3) is not sufficient.
- **INV-S5** (checkpoint initialization, advance and recovery) The checkpoint is advance-only and is advanced only after a journal write is confirmed (INV-A2). A checkpoint lagging the journal by exactly one generation, with a matching `prev_digest`, is accepted and advanced (crash between journal write and checkpoint write); any larger gap is refused. A missing checkpoint with a non-empty journal fails closed. Initialization requires an owner-attested bootstrap record of the first `(generation, digest)`. Recovery from checkpoint loss or an intentional rollback requires a separately owner-authorized, audited checkpoint reset; the controller MUST NOT perform it automatically.
- **INV-S6** (checkpoint before effect) The checkpoint advance for a journal write that authorizes an external effect (T3 `reserved`, or the once-only mark of an escalation notice) MUST be confirmed before that effect is issued, so that a rolled-back journal that re-opens the authorization is refused (INV-S4) instead of permitting a second POST. A failed or ambiguous checkpoint advance issues no effect and leaves the task unchanged; the next run applies INV-S5. This requires an anchor that the controller can advance on every authorizing write; the consequence for the anchor choice is D-11.

### 2.2 CAS and version ownership
- **INV-C1** Every write is a conditional PUT carrying the version token of the last journal this run read or successfully wrote. An unconditional write MUST NOT exist.
- **INV-C2** A run owns a version token only until any write attempt using it returns anything other than a definite success or a definite 409/422 conflict (see 2.3).
- **INV-C3** A run MUST NOT write based on a journal older than the newest one it has observed. After a conflict the run MUST re-read, re-validate, re-derive its intended mutation from the fresh journal, and retry only if the mutation is still valid.
- **INV-C4** A mutation MUST be expressed as a function of the current journal (read-modify-write), never as a replay of a previously computed full blob.

### 2.3 Ambiguity after PUT
A PUT is *ambiguous* when the caller cannot tell whether it applied: timeout, connection reset, 5xx, or a response that cannot be parsed.
- **INV-A1** After an ambiguous PUT the run MUST NOT retry the PUT blindly and MUST NOT perform any external side effect that depends on that write.
- **INV-A2** The run MUST perform read-after-write recovery: read the Store, validate it, and classify as exactly one of:
  - *applied*: the journal contains the intended mutation (identified by a mutation id and generation, not by blob equality alone);
  - *not applied*: the journal equals the pre-write journal;
  - *superseded*: the journal contains neither (another writer advanced it).
- **INV-A3** If recovery reads also fail, the run MUST end without any external side effect that depends on the write, and MUST NOT assume the write did not apply: the durable state is *unknown* (prior or intended) until a later recovery read succeeds. The next run MUST start with read, validation (INV-S4) and classification (INV-A2) before any new mutation or side effect. Because mutations are functions of the journal (INV-C4) and are guarded by state preconditions, an already-applied mutation is simply no longer applicable on the fresh read; an applied `reserved` write is handled by T4r and never leads to a POST.
- **INV-A4** Each mutation carries a unique `mutation_id` persisted in the journal so that recovery and duplicate callbacks are idempotent at the journal level.
- **INV-A5** (last confirmed journal) At every point the run's view of durable state is the newest journal it has confirmed, by a definite success or by a successful INV-A2 classification, together with its version token. Error and abort handlers (transient errors, CAS exhaustion, budget exit, or timeouts after earlier committed writes in the same run) MUST report and act from that view, never from the journal or blob read before the run's first write, and MUST NOT re-write a pre-save blob (INV-N1). An unclassified ambiguous write leaves the affected task's state *unknown* (INV-A3); other tasks continue from the last confirmed journal after a fresh read.

### 2.4 No stale blob overwrite
- **INV-N1** The controller MUST NOT write a journal whose content was derived from a version token that is not the Store's current token (enforced by INV-C1, verified by the stale-CAS tests).
- **INV-N2** A restored or replayed older journal MUST be refused even if its signature is valid (INV-S4).

### 2.5 Bounded journal size
- **INV-B1** The serialized journal MUST NOT exceed `JOURNAL_MAX_BYTES`. The reader's decoder cap and the writer's pre-PUT guard MUST use the same constant (current baseline value: 524288 bytes; the value is a design parameter, see D-3).
- **INV-B2** The size guard MUST run on the final serialized, signed bytes before any PUT and MUST apply one of two distinct thresholds: `ADMISSION_LIMIT = JOURNAL_MAX_BYTES - JOURNAL_RESERVE_BYTES` for any mutation that adds a task, and `MUTATION_LIMIT = JOURNAL_MAX_BYTES` for every mutation of an already admitted task.
- **INV-B3** `JOURNAL_RESERVE_BYTES` is reserved exclusively for mutations to already admitted tasks (state advancement, terminal and escalation transitions, evidence, cursor). Admission of a new task MUST be refused (`JournalFull`) when the final signed bytes would exceed `ADMISSION_LIMIT`. Existing-task mutations MAY consume the reserve, up to `MUTATION_LIMIT`, so that every admitted task can reach a terminal state.
- **INV-B3a** An existing-task mutation whose final signed bytes would exceed `MUTATION_LIMIT` MUST NOT be sent. It is a reserve-sizing fault: the run fails loudly with a typed error and alerts, and no other task is blocked (INV-F4).
- **INV-B4** Per-task size MUST be bounded (evidence is stored as digests and bounded-length identifiers, never raw artifacts) so that reserve sizing is computable: `reserve >= admitted_task_cap * max_transition_growth`.
- **INV-B5** Terminal tasks older than the retention window MAY be compacted only via the archival procedure in D-4. Until that is designed and reviewed, there MUST be no deletion; the journal fails closed for admission instead.

### 2.6 Fail-closed admission without starvation
- **INV-F1** New-task admission failure returns the typed error `JournalFull` (no side effects, no journal write, no POST) and is itself auditable through a non-journal channel (workflow log and job summary).
- **INV-F2** `JournalFull` MUST NOT block progress, reconciliation, expiry, escalation or terminal transition of tasks already in the journal.
- **INV-F3** Admission MUST NOT consume execution budget needed for reconciliation of existing tasks (section 6).
- **INV-F4** Tasks are independent: the failure, ambiguity or deadline expiry of one task MUST NOT prevent processing of another within the same run, except through the shared journal CAS (which is retried per INV-C3).

### 2.7 Delivery safety
- **INV-D1** At-most-once: a non-idempotent POST for a `(task_id, delivery_id)` is issued at most once ever. Before the POST the journal MUST durably record `reserved` with a unique `delivery_id` (write confirmed per 2.3, and its checkpoint advance confirmed per INV-S6). Immediately before the POST the run MUST re-read and validate the journal and confirm that the task is still `reserved` with the same `delivery_id`. A version-token change caused by an unrelated writer does not revoke the authorization (INV-F4); a changed state of this task does.
- **INV-D2** An ambiguous POST (timeout, reset, 5xx after send) MUST NOT be retried. The task moves to `delivered_unconfirmed` and is resolved only by evidence (section 5) or by deadline escalation.
- **INV-D3** Deadlines are wall-clock values stored in the journal at admission. Outages never extend a deadline. At expiry an unconfirmed task escalates; it is never silently dropped or re-posted.

## 3. State-transition matrix

States: `ingested`, `pending`, `reserved`, `delivered_unconfirmed`, `confirmed` (terminal), `escalated` (terminal for automation, requires human), `blocked` (terminal, rejected by policy or evidence). Expiry is not a state: expiry produces `escalated`.

All "Persisted write" entries are single CAS journal writes obeying section 2. "Audit" is the evidence record kept in the journal entry (`events[]`: bounded, append-only, each with run id, run attempt, timestamp, mutation id).

| # | Transition | Trigger | Preconditions | Persisted write | Allowed external side effects | Retry / recovery | Audit evidence |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | (none) -> `ingested` | Authenticated inbound event (PR, SHA, workflow, delivery) | Signature and identity binding valid (5.5); not a duplicate `task_id`; final signed journal at or below `ADMISSION_LIMIT` (INV-B2, INV-B3) | New task record with deadline and identity tuple | None | On CAS conflict re-read and re-evaluate admission; on ambiguous PUT follow 2.3. If admission is refused return `JournalFull` and do nothing else | Event `ingested` with identity tuple |
| T1d | `ingested` duplicate | Duplicate callback with same `task_id` | Existing task | None (or an `events[]` append only if headroom permits; otherwise none) | None | Idempotent no-op | Workflow log entry |
| T2 | `ingested` -> `pending` | Same run after T1, or any later run that reads a task still in `ingested` (for example after a crash between T1 and T2) | T1 confirmed or durably observed; journal passes INV-S4 validation; identity tuple (5.5) re-verified from the stored record in the current run; deadline not passed | Task state `pending` (MAY be folded into T1's write) | None | As T1; a later run applies T2 under fresh validation and never re-ingests; if validation fails the task goes to T8, if the deadline has passed it goes to T7 | Event |
| T3 | `pending` -> `reserved` | Controller selects task for delivery | Deadline not passed; budget left for POST plus reconciliation (6.3); no existing `delivery_id` | `reserved` with new `delivery_id`, `reserved_at` | None | Ambiguous PUT -> recovery. Only proceed to POST when the write is confirmed applied, its checkpoint advance is confirmed (INV-S6) and the pre-POST re-read passes (INV-D1) | Event with `delivery_id` |
| T4 | `reserved` -> `delivered_unconfirmed` | POST attempted or response lost | T3 and its checkpoint advance confirmed (INV-S6); pre-POST re-read shows the task still `reserved` with the same `delivery_id` (INV-D1); first and only POST for this `delivery_id` | `delivered_unconfirmed`, `posted_at`, HTTP outcome class | Exactly one POST | Definite 2xx still goes to `delivered_unconfirmed` first, then T5 on evidence. If the journal write after POST fails or is ambiguous, no re-POST; the durable state is unknown (INV-A3): if the T4 write had applied, the next run reads `delivered_unconfirmed` and continues; if it had not, the next run reads `reserved` and applies T4r | Event with outcome class, no response bodies |
| T4r | `reserved` observed at start of a later run (crash between T3 and T4) | Crash/restart | `reserved` older than `reserved_grace` | `delivered_unconfirmed` marked `post_status=unknown` | None | MUST NOT POST again. Resolution by evidence or deadline only | Event `recovered_reserved` |
| T4n | `reserved` -> `pending` | Definite pre-send failure proven (for example a local failure before any socket write, or a 4xx proving rejection) | Proof recorded | `pending` with `delivery_id` retired and attempt counter incremented | None | Only for definite non-send. A new `delivery_id` is minted on next T3 | Event with proof class |
| T5 | `delivered_unconfirmed` -> `confirmed` | Evidence discovery succeeds (section 5) | Evidence passes identity binding, fresh reauthentication, no contradiction, and the evidence-closure condition EV-15 holds | `confirmed` with evidence digest and closure record | None | Retryable evidence failures (6.2) leave state unchanged; next run resumes from persisted cursor; if closure cannot be reached before the deadline the task goes to T6, never to `confirmed` | Event with run id, attempt, artifact digest |
| T6 | `delivered_unconfirmed` -> `escalated` | Deadline expiry or evidence budget exhausted past deadline | Deadline passed | `escalated` with reason | One notification (idempotent form) permitted only if marked once in journal (own `delivery_id`) | Escalation notice failure does not revert state | Event reason `deadline` |
| T7 | `ingested`/`pending`/`reserved` -> `escalated` | Deadline expiry | As above; for `ingested` and `pending` no POST was ever sent; for `reserved` no POST was sent proven, or sent unknown | `escalated` | As T6 | As T6 | Event |
| T8 | any non-terminal -> `blocked` | Contradictory or forged evidence; identity mismatch; policy refusal | The rejection is **permanent** by taxonomy (6.2), never from a retryable fetch failure | `blocked` with rejection class and digests | None | Terminal. Human review via escalation channel | Event with rejection class |
| T9 | terminal -> terminal | Any except T11 | - | None | None | Terminal states are immutable except the single guarded T11 transition; any other attempt is logged and ignored | Workflow log |
| T10 | any | Journal validation failure (INV-S2) | - | None | None | Run exits failed; no side effects | Workflow log |
| T11 | `confirmed` -> `escalated` | An authenticated verdict with the same logical outcome key (EV-6) but a different outcome is discovered inside the post-confirmation watch window (EV-15) | Task is `confirmed`; the new evidence passes authentication (EV-10, EV-11, EV-14); watch window not elapsed | `escalated` with reason `post_confirmation_contradiction`, both evidence digests and provenance; the original `confirmed` record is retained, not erased | One escalation notice permitted only if marked once in the journal (own `delivery_id`); never a POST of the original delivery | Retryable evidence failures leave state unchanged; the write uses the reserve (INV-B3) | Event with both provenance records |

Rules that hold for all rows:
- A row's external side effect is permitted only after the journal write that *authorizes* it is confirmed. For T3/T4 the confirmed T3 `reserved` write authorizes the single POST; the T4 write records the POST outcome after the POST and does not authorize it. No row requires the write that records an effect to precede that effect (INV-A1).
- Terminal and escalation writes for existing tasks always use the reserved headroom (INV-B3).

## 4. Journal write procedure (normative sequence)

1. Read journal; validate (INV-S1/S2/S3); remember `(version_token, generation)`.
2. Compute mutation `m` with fresh `mutation_id` as a function of the journal (INV-C4).
3. Serialize and sign; check the size of the final signed bytes (INV-B1, INV-B2). An admission above `ADMISSION_LIMIT` returns `JournalFull`. An existing-task mutation above `MUTATION_LIMIT` is not sent and fails the run loudly (INV-B3a, reserve mis-sizing, see D-3); an existing-task mutation between the two limits proceeds and consumes the reserve (INV-B3).
4. PUT with the version token.
5. Outcome: definite success -> adopt the new token (the journal is now *confirmed*, INV-A5) and advance the checkpoint (INV-S5, INV-S6); 409/422 conflict -> go to 1 (bounded by 6.4); ambiguous -> recovery per INV-A2; other 4xx/5xx -> taxonomy 6.2. On any abort the run reports from the last confirmed journal (INV-A5).

## 5. Discovery and evidence contract

### 5.1 Pagination
- **EV-1** Workflow-run discovery uses bounded pages (`PAGE_SIZE`, `MAX_PAGES_PER_RUN`). A run MUST stop scanning when the page budget or time budget is reached and persist progress. Lack of progress is never interpreted as absence of evidence.
- **EV-2** Scans MUST be resumable: the journal stores for each task a scan state `{interval_start, interval_end, resolved_cursor, frontier, attempt_floor, open_runs}`. `resolved_cursor` is the position such that every run at or before it has been fully evaluated (EV-3). `frontier` (at or after `resolved_cursor`) is the furthest position listed; every run between the two is either fully evaluated, with its matches retained (EV-7), or tracked in `open_runs` (EV-13). Unqualified "the cursor" in this specification means `resolved_cursor`.
- **EV-3** The cursor MUST be advanced only past runs that have been **fully evaluated** (all attempts enumerated and every retryable fetch of that run resolved). A run whose artifact fetch failed retryably MUST NOT be passed over.
- **EV-4** Tasks with more than 100 candidate runs (more than one page) MUST be fully covered over multiple runs without skipping or double-counting, in a stable ordering (created_at, id).

### 5.2 Reruns and attempts
- **EV-5** Evidence identity includes `run_id` and `run_attempt`. A rerun (a higher `run_attempt` of an already-scanned `run_id`) inside a retained interval MUST be discovered. The cursor therefore tracks `(run_id, max_attempt_seen)`, not only `run_id`.
- **EV-6** Each authenticated evidence record has two parts: a *logical outcome key* (repository, PR number, head SHA, workflow path and ref, `delivery_id`; explicitly excluding `run_id` and `run_attempt`), which is the only key used to compare outcomes, and *per-attempt provenance* (`run_id`, `run_attempt`, artifact digest, timestamps), which is used for authentication, audit and freshness. A newer attempt never silently supersedes an older authenticated attempt for the same logical key; all are compared under EV-9.
- **EV-13** (same-attempt revisit) `resolved_cursor` MUST NOT pass a run whose `status` is not `completed` or whose conclusion is not final. Such a run is recorded in a bounded `open_runs` set `{run_id, run_attempt, last_status, last_checked, artifact_digest_set}` and re-evaluated on every execution until it completes, the deadline passes, or the set cap is reached. `frontier` MAY advance beyond an open run so that later pages are still discovered, but only while `open_runs` is below its cap; when the cap is reached `frontier` stops advancing (fail closed; no skipping). A change of status, conclusion, completion or artifact digest set at an unchanged `run_attempt` MUST be re-evaluated without incrementing the attempt, and a contradiction found there follows EV-9. When the run completes and is fully evaluated, `resolved_cursor` advances past it, and past any later fully evaluated runs, in one write.

### 5.3 Retention and reauthentication
- **EV-7** Retained matches (candidate evidence kept across runs) MUST be stored as references plus digests. Before a retained match can drive `confirmed` it MUST be freshly re-fetched and re-authenticated (signature/witness, identity binding, artifact digest) in the current run. A stored match is never trusted by itself.
- **EV-8** If re-fetch of a retained match fails retryably, the match stays retained and the task stays unchanged. If re-authentication fails permanently (digest or witness mismatch), the match is discarded and the failure recorded (and may lead to T8 under EV-9).
- **EV-14** (live witness authentication) A *witness* is a mutable external reference (for example a comment, check run or status) that attests an artifact or verdict. A retained match backed by a witness MUST be re-fetched from the provider in the current run and accepted only if all hold: provider identity (expected app or account by immutable numeric id, not login string alone, on an allowlisted API host); stable reference id; binding to PR number, head SHA, `run_id`/`run_attempt` and `delivery_id`; content constraints (expected schema, size bound, digest equal to the retained digest); time constraints (creation inside the task interval, and any `updated_at` later than the retained snapshot forces re-authentication of the changed content as a new candidate, never as the old one). A mutated witness is a new candidate; a definitively deleted witness (404/410 on an existing reference) discards the match and records the failure; a retryable fetch failure leaves the match retained and the task unchanged (EV-8).
- **EV-15** (evidence closure and post-confirmation watch) T5 MAY fire only when the evidence-closure condition holds for the task's evidence interval: (a) the persisted cursor has reached `interval_end` with every page fully evaluated (EV-3); (b) `open_runs` (EV-13) holds no run inside the interval; (c) no retained match or witness awaits a retryable re-fetch (EV-8, EV-14); and (d) a quiet period `EVIDENCE_QUIET_SECONDS` (design parameter, D-10) has elapsed since the newest in-interval run, attempt or witness change was observed. Closure that cannot be reached before the task deadline is not confirmation: the task escalates under T6 and is never confirmed on partial evidence. A confirmed task remains under a bounded post-confirmation watch of `POST_CONFIRM_WATCH_SECONDS` (D-10); discovery of an authenticated opposing verdict for the same logical outcome key (EV-6) inside that window triggers T11, not a silent ignore. Before confirmation an opposing authenticated verdict follows EV-9 (T8). The watch neither extends any deadline nor permits any POST (INV-D1, INV-D3), and ends without further writes.

### 5.4 Contradictory evidence
- **EV-9** Two authenticated pieces of evidence with the same logical outcome key (EV-6) and different outcomes block the task (T8 with `contradiction`), including when they come from different `run_attempt` or `run_id` values. Unauthenticated or wrong-identity artifacts are ignored and counted (bounded) but do not by themselves block, unless they impersonate a valid identity tuple, in which case they block as `forged`. Whether a later attempt may supersede an earlier contradictory one is a policy decision (D-9); the fail-closed default is to block.

### 5.5 Identity binding
- **EV-10** Accepted evidence MUST match all of: repository, PR number, head SHA, workflow identity (path and ref) and `delivery_id` (the logical outcome key, EV-6); and the `run_id` and `run_attempt` asserted by the artifact MUST equal those of the run that produced it (provenance only; they are never part of the comparison key). Any mismatch is a rejection with the specific mismatching field recorded.
- **EV-11** Evidence MUST be produced by the expected workflow identity on the expected ref; artifact name and contents alone do not establish identity.

### 5.6 Bounds
- **EV-12** Bounded memory and journal growth: per task at most `MAX_RETAINED_MATCHES` references, `MAX_EVENTS` events, and a cursor of constant size. Overflow evicts the oldest *non-load-bearing* diagnostic first and never evicts the cursor, the active `delivery_id`, or terminal records.

## 6. Execution budget and error taxonomy

### 6.1 Budget
Run window `W = 480 s`. The controller computes a monotonic deadline at start.
- `R_reconcile` (reserved): time for journal read, recovery and terminal/escalation writes for existing tasks, **default 90 s** (design parameter, D-5).
- **BUD-1** Ingestion and discovery MAY use at most `W - R_reconcile - R_exit` (`R_exit` = final write and logging margin, default 15 s). Every network call is issued with a timeout no greater than the remaining usable budget.
- **BUD-2** Ingestion MUST check remaining budget before each task and each page. When the budget is exhausted it MUST stop admitting or scanning, persist progress, and exit successfully with a "budget exhausted, resume" status. It MUST NOT fail the run solely because work remains.
- **BUD-3** Reconciliation for existing tasks is processed before new-task ingestion, ordered by nearest deadline.
- **BUD-4** Retry sleeps count against the budget. A sleep that would cross the usable budget is replaced by exit-and-resume.
- **BUD-5** Rate-limit headers (`Retry-After`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`) are honored: if the wait exceeds remaining usable budget, persist the next-eligible time and exit.

### 6.2 Error taxonomy
| Class | Examples | Treatment | Permanent rejection? |
| --- | --- | --- | --- |
| Rate limited | 429; 403 with rate-limit headers exhausted | Honor headers (BUD-5). Persist `not_before`. The backoff counter resets only after a success, not on quota refresh | No |
| Transient server | 5xx, timeouts, connection errors, truncated bodies | Bounded retries with jittered exponential backoff inside budget; then exit-and-resume. For PUT: ambiguous (2.3). For POST: ambiguous (INV-D2) | No |
| Auth | 401 | Token refresh once if the mechanism exists; otherwise fail run, no state change, alert | No (operational failure) |
| Forbidden | 403 without rate-limit signal | Fail run with alert; no state change | No |
| Not found | 404 on artifact/run | If the run exists and the artifact is expired or not yet uploaded: retryable until the artifact deadline; 404 on a resource that definitively cannot exist for that identity: permanent for that candidate only | Candidate only, never the task unless EV-9 applies |
| Conflict | 409, 422 on conditional PUT | Re-read, re-derive (INV-C3). Bounded by `MAX_CAS_RETRIES`; exhaustion exits without side effects | No |
| Validation | 422 on POST payload | Definite non-send -> T4n if proven; else treat as ambiguous | Task-level only if payload is invariably invalid |
| Admission | `JournalFull` | INV-F1..F3 | Not applicable |
| Budget | Retry budget or time budget exhausted | Persist progress, exit-and-resume | No |

- **ERR-1** Failure to download or read a temporary artifact (network error, 5xx, timeout, partial read, expired signed URL) MUST NOT produce a `blocked`/rejected outcome. Only a successfully fetched artifact that fails authentication, identity or digest checks is a permanent candidate rejection.
- **ERR-2** Retry-budget exhaustion is never converted into a rejection; the evidence remains pending until the task deadline, after which T6 applies.

### 6.3 Pre-POST budget gate
- **BUD-6** T3/T4 only start when remaining time >= `2 * T_write + T_checkpoint + T_reread + T_post_timeout + T_recovery`. Otherwise the task stays `pending` for the next run (the deadline still governs).

### 6.4 Bounds on retries
`MAX_CAS_RETRIES`, `MAX_PUT_RECOVERY_READS` and `MAX_ARTIFACT_RETRIES_PER_RUN` are explicit constants. Values are design parameters (D-5).

## 7. Fault-injection test matrix

Conventions: "POSTs" is the number of non-idempotent POSTs to the external system for the task under test. "Budget" is the test's simulated run window. Each test uses a fake Store (versioned, with injectable faults), a fake clock, and a fake external API. Assertions are on observable fake state only.

| ID | Case | Setup | Injection point | Expected journal transition | POSTs | Budget | Observable assertion |
| --- | --- | --- | --- | --- | --- | --- | --- |
| FI-01 | Ambiguous PUT, applied | 1 `pending` task | Store applies T3 then drops the response | `pending` -> `reserved` (found via recovery) | 1 only after recovery confirms applied | 480 s | Journal contains the `mutation_id`; exactly one POST; no second PUT of the same blob |
| FI-02 | Ambiguous PUT, not applied | as FI-01 | Store drops request | Stays `pending`, then T3 retried via fresh read-modify-write | 1 (after confirmed T3) | 480 s | Generation increased by exactly 1; POST issued only after confirmed write |
| FI-03 | Ambiguous PUT, superseded | Other writer advances journal in between | Response lost and external write | Mutation re-derived from fresh journal; dropped if invalid | 0 or 1 per validity | 480 s | No overwrite of the other writer's change (INV-N1) |
| FI-04a | Recovery reads fail, PUT had applied | as FI-01 (T3 `pending` -> `reserved` applied) | Store applies the PUT, drops the response, then all reads fail | Unknown until a read succeeds; durable journal holds `reserved` | 0 | 480 s | Run ends with no POST and no assumption of rollback; next run reads, validates, finds `reserved`, applies T4r (`delivered_unconfirmed`, `post_status=unknown`), still 0 POSTs; the task resolves by evidence or escalates at its deadline |
| FI-04b | Recovery reads fail, PUT had not applied | as FI-01 | Store drops the request, then all reads fail | Unknown until a read succeeds; durable journal still `pending` | 0 | 480 s | Run ends with no POST; next run reads, finds `pending`, and performs a fresh T3 read-modify-write, then exactly 1 POST |
| FI-05 | Stale CAS | Two runs read the same token | Run B writes first | Run A gets 409, re-reads, re-derives | Total across runs: 1 | 480 s | Final journal contains both mutations or A's is dropped validly; never A's stale blob |
| FI-06 | Duplicate callbacks | Same `task_id` delivered 3 times concurrently | Concurrent ingestion | One `ingested`; two no-ops | 0 | 480 s | One task record; journal generation advances once for ingestion |
| FI-07 | Crash after T3 before POST | `reserved` task | Process kill | On restart: T4r -> `delivered_unconfirmed(post_status=unknown)` | 0 on restart | 480 s | Zero POSTs after restart; deadline escalation still occurs |
| FI-08 | Crash after POST before T4 write | POST sent, journal write not made | Kill after POST | Same as FI-07 | Total 1 | 480 s | Total POST count across both runs is 1 |
| FI-09 | Ambiguous POST | POST times out | Timeout after send | `delivered_unconfirmed` | 1, no retry | 480 s | No second POST in same or later run |
| FI-09b-applied | Ambiguous T4 PUT that applied (amends FI-07..FI-09) | `reserved` task, T3 and its checkpoint advance confirmed | POST succeeds or is ambiguous; the Store applies the T4 PUT but the response is lost (variant: recovery reads also fail) | Durable `delivered_unconfirmed` (found by INV-A2 classification, or by the next run's read) | 1 total | 480 s | The next run reads `delivered_unconfirmed`, does not apply T4r and does not write T4 again; it resolves by evidence or deadline; zero further POSTs |
| FI-09b-unapplied | Ambiguous T4 PUT that did not apply (amends FI-07..FI-09) | as above | POST succeeds or is ambiguous; the Store drops the T4 request (variant: 409 conflict, where T4 is re-derived from the fresh journal and written with no extra POST) | Durable `reserved` until a read succeeds; then T4r gives `delivered_unconfirmed(post_status=unknown)` | 1 total | 480 s | The next run reads `reserved`, applies T4r, zero further POSTs in any later run; T3 alone authorized the POST; no pre-write of T4 is required |
| FI-09c | Reservation authorizes POST (amends FI-07..FI-09) | `pending` task | Observe journal at the moment of POST | `reserved` durable at POST time; `delivered_unconfirmed` written only afterwards | 1 | 480 s | At the POST instant the Store already holds `reserved` with the `delivery_id`; the T4 record appears only after the POST; a POST attempted without a confirmed `reserved` write fails the test |
| FI-10 | Prolonged Store outage | Store returns 503 for N runs spanning the deadline | Store | Tasks unchanged until reachable; then T6 for expired | 0 extra | N x 480 s | Deadlines not extended; after recovery expired tasks are `escalated`, not re-posted |
| FI-11 | Prolonged external API outage | API 5xx for all runs | External | `reserved`/`pending` kept; T7 at deadline (T6 once `delivered_unconfirmed`) | At most 1 per `delivery_id` | N x 480 s | No POST storm; escalation recorded |
| FI-12 | Rate-limit headers | 429 with `Retry-After` beyond remaining budget | External read | `not_before` persisted | 0 | 480 s | Run exits success quickly; next run does not call before `not_before` |
| FI-13 | Quota reset behavior | Quota refreshes mid-backoff | External | Backoff counter not reset until a success | n/a | 480 s | Retry spacing stays monotonic non-decreasing until success |
| FI-14 | Artifact download failure | Artifact fetch returns 5xx/timeouts for all attempts | Artifact download | `delivered_unconfirmed` unchanged; cursor not advanced past the run | 0 | 480 s | No `blocked`; a later run with healthy fetch confirms (ERR-1, EV-3) |
| FI-15 | Expired artifact URL | Signed URL expires mid-read | Download | Re-list and retry | 0 | 480 s | No rejection recorded |
| FI-16 | >100 runs scan | 250 candidate runs, evidence on run 237 | Pagination | Cursor advances page by page over multiple runs, then `confirmed` | 0 | 3 runs of 480 s | No skipped run; no run evaluated twice with differing result; journal size constant |
| FI-17 | Scan budget exhausted | Slow API | Time | Progress persisted, run exits success | 0 | 480 s | Cursor persisted; reconcile reserve intact (BUD-1) |
| FI-18 | Rerun in retained interval | Run R attempt 1 scanned without evidence, attempt 2 carries it | Attempt tracking | `confirmed` from attempt 2 | 0 | 480 s | Cursor `max_attempt_seen` raised; evidence identity cites attempt 2 |
| FI-19 | Retained match tampered | Retained reference whose artifact was replaced | Reauthentication | Match discarded, task unchanged or `blocked` if forging | 0 | 480 s | No `confirmed` from stored data alone (EV-7) |
| FI-20 | Retained match fetch fails | Retryable fetch error on retained match | Reauthentication | Unchanged | 0 | 480 s | Match remains retained |
| FI-21 | Contradictory evidence | Two authenticated artifacts with the same logical outcome key and opposite outcomes, in variants: same run and attempt, different `run_attempt`, different `run_id` | Evidence | `blocked(contradiction)` in every variant | 0 | 480 s | Terminal, immutable on later runs (T9); per-attempt provenance of both pieces recorded; no variant is resolved by attempt ordering (EV-6, EV-9, D-9) |
| FI-22 | Forged artifact | Wrong workflow/ref/SHA/attempt, valid shape | Evidence | Ignored; if impersonating identity then `blocked(forged)` | 0 | 480 s | Each mismatching field recorded individually (EV-10) |
| FI-23 | Ingestion exceeds execution budget | Many inbound events, slow Store | Time | Ingestion stops early, remaining events deferred, reconciliation done | 0 | 480 s | Run succeeds; existing tasks reconciled; reserve respected (BUD-2/3) |
| FI-24 | Near-cap admission | Journal at `MAX - RESERVE` with 20 active tasks | Admission | New task refused (`JournalFull`); existing tasks progress to terminal | Existing tasks' POSTs per normal | 480 s | No journal write for the refused task; existing tasks reach `confirmed`/`escalated`; size never exceeds MAX (INV-F2) |
| FI-25 | Over-cap write guard | Mutation would exceed MAX | Pre-PUT | No PUT sent | 0 | 480 s | Zero PUT requests; typed error; Store and decoder caps equal (INV-B1) |
| FI-26 | Journal growth DoS | Attacker floods events with distinct ids | Ingestion | Admission capped; per-task event bound | 0 | 480 s | Journal size bounded; reads stay under decoder cap |
| FI-27 | Concurrent independent tasks | 10 tasks, one with ambiguous PUT, one with outage | Mixed | Others progress independently | 1 per task max | 480 s | No task blocked by another's fault (INV-F4) |
| FI-28 | Validly signed older journal restored between processes | Process 1 advances to generation N and the checkpoint; the Store is then restored to a validly signed generation N-k (variant: generation N with a different digest); process 2 starts fresh with no in-memory state | Store read | Rejected whole before any mutation (INV-S4, INV-N2) | 0 | 480 s | Zero PUT and zero POST requests; checkpoint unchanged; escalation alert recorded |
| FI-29 | Corrupted or unsigned journal | Bad signature | Store read | Rejected whole | 0 | 480 s | No write; run fails closed |
| FI-30 | CAS retry exhaustion | Persistent 409 | Store | No change | 0 | 480 s | Bounded retry count; run exits without side effects |
| FI-31 | Admission versus mutation boundary | Journal at `ADMISSION_LIMIT` with 20 active tasks; one new admission and existing-task terminal transitions that grow the journal above `ADMISSION_LIMIT` but not above `MUTATION_LIMIT` | Pre-PUT size guard on final signed bytes | New task refused (`JournalFull`); each existing task reaches `confirmed`/`escalated` using the reserve | Existing tasks' POSTs per normal; 0 for the refused task | 480 s | Zero PUT for the refused admission; all 20 tasks terminal; final signed size never exceeds `JOURNAL_MAX_BYTES` (INV-B2, INV-B3) |
| FI-32 | Existing mutation above `MUTATION_LIMIT` | Mutation of an admitted task whose final signed bytes exceed `JOURNAL_MAX_BYTES` | Pre-PUT size guard | No change for that task | 0 | 480 s | Zero PUT; typed reserve-sizing fault and alert; other tasks still progress (INV-B3a, INV-F4) |
| FI-33 | Same-attempt in-progress run revisit | Run R attempt 1 is `in_progress` when scanned; later pages are also listed in the same execution (variant: `open_runs` already at its cap) | Run status and artifact change at an unchanged attempt | Execution 1: `resolved_cursor` stays before R, R is recorded in `open_runs`, `frontier` MAY advance. Execution 2 (R completed with evidence at attempt 1): `resolved_cursor` advances past R, then `confirmed` once EV-15 closure holds (variant: a contradicting authenticated artifact appears, `blocked(contradiction)`) | 0 | 2 runs of 480 s | `resolved_cursor` never passes R while R is incomplete; `run_attempt` unchanged; the change is detected from status, conclusion or artifact digest; with `open_runs` at its cap `frontier` stops without skipping (EV-2, EV-13) |
| FI-34 | Witness mutation after retention | Retained match backed by a witness that is later edited (content or `updated_at` changes) | Live reauthentication | Old match not reused; changed content treated as a new candidate and authenticated from scratch | 0 | 480 s | No `confirmed` from the stale snapshot (EV-14) |
| FI-35 | Witness deletion | Retained witness definitively deleted (404/410 on an existing reference) | Live reauthentication | Match discarded and failure recorded; task unchanged | 0 | 480 s | No `confirmed` from stored data; discard is recorded (EV-14) |
| FI-36 | Witness retryable fetch failure | Witness fetch returns 5xx or times out | Live reauthentication | Unchanged; match retained | 0 | 480 s | No rejection and no `blocked`; a later healthy fetch resolves (EV-8, EV-14) |
| FI-37 | Missing checkpoint, non-empty journal | Checkpoint absent while the journal has generation greater than 0 | Checkpoint read | Run fails closed | 0 | 480 s | Zero PUT and zero POST; owner alert; no automatic bootstrap (INV-S5) |
| FI-38 | Checkpoint lags by one generation | Crash after a journal write but before the checkpoint advance | Restart | Checkpoint advanced after `prev_digest` verification; run proceeds | per normal | 480 s | Checkpoint equals journal generation; no rewind accepted (INV-S5) |
| FI-39 | Checkpoint gap larger than one | Journal generation exceeds checkpoint by 2 or more, or `prev_digest` chain does not reach it | Checkpoint read | Rejected whole | 0 | 480 s | Zero PUT and zero POST; escalation alert (INV-S4) |
| FI-40 | Late opposing verdict on a later page | `delivered_unconfirmed` task; run A on page 1 carries an authenticated success; run B on a later page (variants: a later attempt, or a later controller execution) carries an authenticated opposing verdict | Sequential scan across two controller executions | Execution 1: closure unmet (cursor short of `interval_end`), state unchanged; execution 2 sees B and goes to `blocked(contradiction)` | 0 | 2 runs of 480 s | Never `confirmed`; both provenance records stored (EV-9, EV-15) |
| FI-41 | Opposing verdict after confirmation | Closure met (cursor at `interval_end`, quiet period elapsed) so T5 gives `confirmed`; in a later execution an authenticated opposing verdict appears inside the watch window | Evidence | `confirmed` -> `escalated(post_confirmation_contradiction)` via T11; variant: the same verdict after the watch window ends causes no write | 0 | 2 runs of 480 s | Original `confirmed` record retained; zero POSTs; both provenance records present (T11, EV-15) |
| FI-42 | Closure unmet at deadline | `delivered_unconfirmed` task with an unfinished scan or an `open_runs` entry when the deadline passes | Time | `escalated` via T6 | 0 | N x 480 s | No `confirmed` on partial evidence; deadline not extended (EV-15, INV-D3) |
| FI-43 | Crash between T1 and T2 | T1 committed, process killed before T2 | Kill | Next run reads `ingested`, re-validates under INV-S4 and re-verifies identity, then T2 gives `pending`; variant: invalid stored identity gives T8 | per normal | 480 s | One task record, no duplicate ingestion; generation advances once for T1 and once for T2 (T2) |
| FI-44 | Stuck `ingested` at deadline | `ingested` task whose T2 cannot complete (retryable Store failures) until the deadline | Time | `escalated` via T7 | 0 | N x 480 s | Escalated at the deadline; no POST; not permanently stuck (T7) |
| FI-45 | Transient error after earlier committed writes | In one run: T2 for task X is committed, then T3 for task Y is committed, then a transient Store error (5xx on a later read) aborts the run | Store, after two committed writes | The run reports and exits from the last confirmed journal (X `pending`, Y `reserved`); no pre-first-write blob is re-written | 0 in this run | 480 s | The state reported by the error handler equals the Store's journal; generation equals the last committed write; no PUT carries the run's initial blob or version token after the first write (INV-A5, INV-N1) |
| FI-46 | CAS abort after partial writes | Two writes are committed, then the third gets persistent 409 until `MAX_CAS_RETRIES` is exhausted | Store | The two committed writes are retained; the run exits from the last confirmed journal | 0 | 480 s | No PUT derived from the original blob; bounded retries; final state equals the last confirmed journal (INV-A5, INV-C3, FI-30) |
| FI-47 | Checkpoint advance fails before POST | T3 `reserved` is confirmed, then the checkpoint advance fails or is ambiguous (variant: the Store is restored to the pre-T3 journal after a confirmed advance and a POST) | Anchor | Failed advance: task stays `reserved`, no POST; the next run applies INV-S5 and then T4r. Variant: the restored journal is refused whole (INV-S4) | 0 additional | 480 s | A POST is never issued before the checkpoint covers the authorizing write (INV-S6); the restore variant causes zero PUT and zero second POST; the failed-advance task resolves by evidence or deadline escalation |

## 8. Mapping of PR #219 review findings to this specification

The 12 open, unresolved review threads on PR #219 below were verified by the orchestrator against the live PR #219 review threads as the authoritative inventory. Each is mapped once. A gap names a requirement or test the current specification does not yet state; no design decision is invented for it. Proposed tests for gaps are not yet added to section 7. Coverage has not been assessed by any reviewer, and no test or implementation result is asserted.

| # | Thread (`PRRT_kwDOUdC8Ds6...`) and finding | Invariants | Transitions / sections | FI tests | Gap |
| --- | --- | --- | --- | --- | --- |
| 1 | `xg8Vi`: TransientError handler returns stale pre-save journal state/blob after committed writes; ambiguous PUT recovery | INV-A1..A5, INV-C2..C4, INV-N1 | Section 4 step 5, T3 | FI-01..FI-04, FI-45 | G-1 |
| 2 | `xmLxk`: capped discovery scans restart, lose cursor and retained evidence | INV-B3 | EV-1..EV-4, EV-12 | FI-16, FI-17 | G-2 |
| 3 | `xn4us`: stale original_blob after partial journal writes, CAS conflict and abort | INV-A5, INV-C1, INV-C3, INV-C4, INV-N1 | Section 4, 6.4 | FI-05, FI-30, FI-46 | G-1 |
| 4 | `xrV8T` (P1): persisted scan interval misses rerun attempt/status changes and contradictory evidence | none | EV-5, EV-6, EV-9, T8 | FI-18, FI-21 | G-3 |
| 5 | `xrV8f` (P2): retained match not reauthenticated after mutable witness changes | none | EV-7, EV-8, EV-10, EV-11 | FI-19, FI-20 | G-4 |
| 6 | `xtjVE` (P1): shared journal grows past 524288-byte decoder limit and blocks all work | INV-B1..B5, INV-F1 | EV-12 | FI-24, FI-25, FI-26 | G-10 |
| 7 | `xtjVH` (P2): quota-reset delay applied to HTTP 500 even when quota remains | none | 6.2, BUD-4, BUD-5 | FI-12, FI-13 | G-5 |
| 8 | `xyuAg` (P1): retryable artifact download/storage failure treated as permanent Rejected and skipped | none | ERR-1, ERR-2, EV-3, T5, T8 | FI-14, FI-15 | G-6 |
| 9 | `x0PNt` (P2): transient evidence-read failure bypasses persisted task deadline | INV-D3 | T6, T7, ERR-2 | FI-10, FI-14 | G-7 |
| 10 | `x0phT` (P2): transient ingestion lookup aborts reconciliation of existing tasks | INV-F4 | BUD-2, BUD-3 | FI-23, FI-27 | G-8 |
| 11 | `x7kSO` (P2): ingestion retries exhaust 8-minute controller execution budget | none | BUD-1..BUD-4, BUD-6 | FI-17, FI-23 | G-9 |
| 12 | `x7kSU` (P2): near-cap JournalFull admission aborts existing-task reconciliation | INV-B3, INV-F1..F3 | BUD-3 | FI-24 | G-10 |

Gaps (each needs owner or reviewer disposition):

- **G-1** (threads 1, 3) Addressed in the specification by INV-A5 (act only on the last confirmed journal) and Section 4 step 5; coverage by FI-45 and FI-46 (multi-write runs) awaits independent review. FI-01..FI-05 each exercise one write.
- **G-2** (thread 2) FI-16 and FI-17 assert the cursor only; nothing asserts retained matches survive a cap-triggered exit.
- **G-3** (thread 4) Addressed in the specification by EV-6, EV-9 and EV-13 (same-attempt revisit, cross-attempt contradiction); coverage by FI-21 and FI-33 awaits independent review. Open policy question D-9.
- **G-4** (thread 5) Addressed by EV-14 (live witness authentication); coverage by FI-34..FI-36 awaits independent review. Exact provider, reference and time constraints remain a design parameter (D-5, D-8).
- **G-5** (thread 7) The taxonomy separates 429 from 5xx, but no test asserts an HTTP 500 with quota remaining gets no quota-reset delay; FI-13 covers backoff reset only.
- **G-6** (thread 8) The thread says "download/storage failure"; ERR-1 covers artifact download only. A reviewer must say whether journal storage failures are meant (those follow section 2.3).
- **G-7** (thread 9) No test asserts that a transient evidence-read failure still lets the deadline escalation (T6/T7) run for that task.
- **G-8** (thread 10) No explicit rule or test that a transient ingestion lookup error leaves reconciliation of existing tasks running; BUD-3 ordering only implies it.
- **G-9** (thread 11) FI-23 uses a slow Store only; no test covers ingestion retry exhaustion, and the constants are open (D-5).
- **G-10** (threads 6, 12) Capacity numbers (D-3) and retention (D-4) are unanswered, so FI-24..FI-26 pin no numbers.

## 9. Acceptance criteria

The specification is acceptable for implementation to resume only when all hold:
1. Codex and owner-run Jules have each reviewed the exact head of the spec PR and published a verdict (`Verdict: Accepted`).
2. Every open thread on PR #219 maps to at least one spec section and one FI test (section 8 has verified IDs).
3. Design decisions D-1..D-11 are answered by the owner.
4. The implementation PR contains a test for every FI row, each asserting on the observables listed, with exact-head CI green. (This is a future criterion; nothing here claims it is met.)
5. No `JournalFull` path can fail an existing-task mutation within the reserve (test FI-24 plus a property/boundary test at `MAX - RESERVE`).
6. Production execution and activation remain separately owner-authorized.

## 10. Unresolved design decisions

- **D-1** Thread inventory is now verified (12 threads, section 8). Open: assess coverage of gaps G-1..G-10 and confirm whether any thread needs a requirement not captured here.
- **D-2** Is a single shared journal acceptable, or should journals be sharded per task or per time window? Sharding removes global CAS contention and the growth cap but increases complexity and cross-journal consistency work.
- **D-3** Values for `JOURNAL_MAX_BYTES`, `JOURNAL_RESERVE_BYTES`, the admitted-task cap and the per-transition growth bound.
- **D-4** Archival/retention policy for terminal tasks (preserve immutable audit evidence, safe restart). No deletion is authorized by this spec.
- **D-5** Budget constants: `R_reconcile`, `R_exit`, retry counts, page size, `MAX_PAGES_PER_RUN`, `reserved_grace`.
- **D-6** Whether `delivered_unconfirmed` with `post_status=unknown` should escalate immediately (safer, more human work) or wait for the deadline (current spec).
- **D-7** Whether a rate-limit `not_before` is stored per task or globally in the journal.
- **D-8** Trust anchor for the cross-run checkpoint (INV-S4/S5). Options: (a) a checkpoint ledger in a separate protected location written with a distinct credential and signing key that the controller's Store credential cannot overwrite; (b) a signed append-only git ref or tag in this repository, protected by a ruleset that forbids deletion and force-push; (c) an owner-held external anchor (for example a signed release or ledger entry updated by the owner). Safe default until decided: (c) with controller activation held; no activation without a decided anchor.
- **D-9** Whether a later `run_attempt` with an authenticated, different verdict may supersede an earlier authenticated verdict for the same logical outcome key. Fail-closed default: block as contradiction (EV-9).
- **D-10** Values for `EVIDENCE_QUIET_SECONDS` and `POST_CONFIRM_WATCH_SECONDS` (EV-15), and whether a post-confirmation contradiction (T11) should escalate (safe default, as specified) or be handled some other way. No value is chosen by this specification.
- **D-11** Consequence of INV-S6 for the D-8 anchor. INV-S6 requires an anchor that the controller can advance on every authorizing write. An owner-held anchor (D-8 option c, the safe default) cannot satisfy it, so under (c) POST-issuing activation stays held and only read and reconcile paths may run, until the owner chooses (a) or (b), or explicitly accepts a weaker per-window anchor with a documented duplicate-POST risk. This is a design proposal requiring owner authorization; this specification introduces no new credential, key or permission.

## 11. Non-goals

This document does not change workflows, scripts, application code, secrets, permissions or AGENTS.md. It does not authorize merging PR #219, activating the controller, or any production action.
