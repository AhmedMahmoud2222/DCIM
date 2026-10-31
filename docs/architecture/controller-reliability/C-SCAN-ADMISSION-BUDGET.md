# Controller Reliability Specification, Document C: Scan, Admission, Deadlines and Execution Budget

Status: **DRAFT specification. Not reviewed. Not accepted.** No implementation, test or CI result is asserted by this document.

- Authored by: Claude (patch delivery, DX12). Applied by: the orchestrator.
- Reference baseline: `main@11f46a2bd08e3c8d059497b437579440a93658d5`, which contains merged Document A (`A-JOURNAL-DELIVERY.md`, PR #243).
- Subject: controller reliability work in PR #219 (frozen). Source material: parked PR #228 and issue #220. This document does not inherit the contradictions of that material.
- Authority: owner decision D5 "specification first" and A7 split execution (issue #220). The partition plan is a drafting approach only and not technical acceptance of any invariant.
- Production execution, controller activation and any POST-issuing mode are **not** authorized by this document. The hold of Document A section 8 (D-8, D-11) applies unchanged.

The keywords MUST, MUST NOT and SHOULD are used in the RFC 2119 sense. Names such as "journal" or "Store" denote roles, not code identifiers.

## 1. Provides, Requires and vocabulary

**Requires** (Document A, cited by ID only; this document never restates or edits them): INV-S2, INV-S4 (through the typed errors of A:P7), INV-C3, INV-C4, INV-A1, INV-A3, INV-A5, INV-B1, INV-D1, INV-D3, transitions T3, T4r, T9, T10, and contracts P2, P3, P4, P5, P6, P7, P8.

**Provides** (single authoritative home here): INV-B2, INV-B3, INV-B3a, INV-B4, INV-B5, INV-F1..INV-F4, DL-1, ING-1, SC-1..SC-8, transitions T1, T1d, T2, T6, T7 and the C-extension of A:T3, BUD-1..BUD-7, ERR-1..ERR-4, the error taxonomy (section 6), the unit interface (section 2) and the `scan_complete` predicate (SC-7), decisions D-3, D-4, D-5, D-7.

This document refers to no other specification document. Where a policy belongs to a consumer, it appears as a named input in section 2.

| Term | Meaning |
| --- | --- |
| Unit | One ordered item discovered by a scan. It has a stable, opaque `unit_key` and a `unit_rev`, an opaque revision stamp supplied by the consumer that changes whenever the unit's observable state changes. `unit_key` values form a total order used only to compare keys with other keys. They are never compared with, or converted to, a time (SC-7). |
| Resolved unit | A unit whose evaluation is finished according to the consumer (Q4), with everything the consumer must keep durably recorded in the journal. |
| Open unit | A unit that is not resolved: incomplete, or its fetch failed retryably. |
| Scan state | The per-task record defined in SC-1. |
| Non-terminal task | A task in `ingested`, `pending`, `reserved` or `delivered_unconfirmed`. |
| Admission | Any mutation that adds a task record to the journal. |
| Run window | `RUN_WINDOW`, the platform's wall-clock limit for one execution (8 minutes in the reference environment). |
| Listing pass | One complete, ordered enumeration of the interval's unit listing (Q3) from its first page through a page flagged `exhausted`. |
| Listing epoch | An opaque token from the source (Q3a) that is equal across two reads only if no unit of the interval was added, removed or revised between them. |
| Coverage witness | The durable record in the scan state that a listing pass finished, bracketed by two equal listing-epoch reads, without observing any change (SC-7). |

## 2. Consumer-supplied inputs (named parameters)

| ID | Input | Contract |
| --- | --- | --- |
| Q1 | Inbound event functions | `event_authentic(event)`, a deterministic `task_id(event)`, a `deadline(event)` computed from event-intrinsic time, and an `interval(event)` `[interval_start, interval_end]` of wall-clock times with `interval_end <= deadline`. Policy and meaning belong to the consumer. |
| Q2 | `identity_valid(record)` | Re-verifies a stored task's identity tuple from the stored record alone. |
| Q3 | Unit listing | A paged listing of the units that belong to an interval by the consumer's own membership rule. It returns `(unit_key, unit_rev)` pairs in a stable `unit_key` order, with at most `PAGE_SIZE` per page, an opaque `page_token` that resumes the listing, the `listing_epoch` of Q3a, and an `exhausted` flag. `exhausted` is true only on a page after which the listing, as seen by that call, holds no further unit for the interval; an empty listing returns an empty page with `exhausted` true. If the source cannot state this reliably the consumer MUST NOT set the flag, and then `scan_complete` is never true (fail closed, SC-7). Q3 relates `unit_key` to the interval only through the consumer's membership rule; no order-preserving map from `unit_key` to time is required or assumed. |
| Q3a | Listing consistency capability | The consumer MUST declare exactly one `coverage_capability`. `epoch`: every page carries a `listing_epoch`, and `read_epoch(interval)` returns the current epoch; the epoch changes whenever any unit of the interval is added, removed or has its `unit_rev` changed, and does not change otherwise. `snapshot`: the first page request of a pass pins a snapshot, every `page_token` of that pass reads the same snapshot, the pinned snapshot identifier is returned as the page epoch, and `read_epoch(interval)` returns the live epoch of the source (equal to the pinned epoch only while the source is unchanged since the pin). `none`. This specification provides none of these primitives and invents no external atomic operation; `epoch` and `snapshot` are required consumer capabilities. With `none`, with an unreliable epoch, or when any epoch call fails, no witness is written and `scan_complete` is false (fail closed, SC-7). |
| Q4 | Unit evaluation | Returns `resolved`, `unresolved(reason)` or `unavailable` for a unit. Only the consumer may decide that a unit is resolved. This document never infers it. |
| Q5 | Retained items | Opaque per-task items the consumer needs to keep for resolved units, bounded by `RETAINED_BOUND`. |
| Q6 | Response facts | For every external read: status class and, when present, remaining quota, reset time and any retry-after hint, each recorded as a fact separate from the status class. |
| Q7 | Inbound source | Events remain re-listable for at least the longest task interval, and are never consumed destructively before T1 is confirmed (ING-1). |

## 3. Invariants

### 3.1 Journal size, admission and reserve (INV-B)
- **INV-B2** For every PUT this document supplies the `size_limit` of A:INV-B1 (contract A:P3), measured on the final signed bytes: `ADMISSION_LIMIT = JOURNAL_MAX_BYTES - JOURNAL_RESERVE_BYTES` for any mutation that adds a task record (including a T1 folded with T2 or any batch that includes an admission), and `MUTATION_LIMIT = JOURNAL_MAX_BYTES` for every other mutation.
- **INV-B3** `JOURNAL_RESERVE_BYTES` is reserved for mutations of already admitted tasks (state advancement, escalation, scan state, retained items). Admission MUST be refused (`JournalFull`, INV-F1) when its final signed bytes would exceed `ADMISSION_LIMIT`. Mutations of admitted tasks MAY consume the reserve, up to `MUTATION_LIMIT`.
- **INV-B3a** An existing-task mutation that returns `SizeLimitExceeded` at `MUTATION_LIMIT` is a reserve-sizing fault. The failing unit of work stops, an alert is raised, the task keeps its durable state, and no other task is blocked (INV-F4). It is never converted into a task rejection.
- **INV-B4** Every task record has a bounded maximum growth after admission (`TASK_GROWTH_BOUND`: bounded events, scan state, open units and retained items). Admission is also refused when the count of non-terminal tasks has reached `NONTERMINAL_CAP`. Configuration MUST satisfy `JOURNAL_RESERVE_BYTES >= NONTERMINAL_CAP * TASK_GROWTH_BOUND`; if it does not, the controller MUST refuse all admissions (fail closed) while still processing existing tasks. Hence every admitted task can reach a terminal state within the reserve.
- **INV-B5** No deletion or compaction of terminal tasks is specified or authorized (D-4). Terminal records do not grow (A:T9). When the journal fills with terminal records, admission fails closed with `JournalFull`.

### 3.2 Admission without starvation and task independence (INV-F, DL, ING)
- **INV-F1** An admission refused by INV-B2/B3/B4 returns the typed error `JournalFull`: no journal write, no external effect, no task record. It is auditable through a non-journal channel (workflow log and job summary).
- **INV-F2** `JournalFull`, `SizeLimitExceeded` for another task, and any ingestion failure MUST NOT block progress, reconciliation, deadline escalation or terminal transition of tasks already in the journal.
- **INV-F3** Admission MUST NOT consume execution budget reserved for reconciliation (BUD-1, BUD-3).
- **INV-F4** Tasks are independent. Any failure inside one task's unit of work (a retryable fetch error, a rate limit, a size fault, an invalid identity) is recorded for that task and the run continues with the next. Only the journal-level failures of A:P7 (`JournalInvalid`, `RollbackRefused`, `CheckpointUnavailable`) end the run, because no task may be acted on without a validated journal. Interaction between tasks happens only through CAS retries (A:INV-C3).
- **DL-1** (deadline sweep) Deadline escalation (T6, T7) is decided from the validated journal and the wall clock alone. It MUST be evaluated for every non-terminal task at the start of reconciliation, before and independently of any external read, and no retryable external failure, rate limit, retry-budget exhaustion or ingestion failure may skip, delay or replace it. Outages never extend a deadline (A:INV-D3).
- **ING-1** Ingestion is idempotent: `task_id` is deterministic (Q1), a duplicate never writes, and an inbound event is not acknowledged or consumed before its T1 write is confirmed (A:INV-A5). After an ambiguous or lost T1 write the next run re-lists the event and decides from the durable journal alone (A:INV-A3).

### 3.3 Resumable scan state (SC)
- **SC-1** Each task carries a bounded scan state `{interval_start, interval_end, resolved_cursor, frontier, open_units, chunk_digests, retained, change_seq, pass, witness}`. The interval is fixed at admission and never changed. `resolved_cursor` is the greatest `unit_key` such that every unit at or before it is resolved. `frontier` is the greatest `unit_key` listed so far, and `frontier >= resolved_cursor` in `unit_key` order (key against key only). `change_seq` is a persisted counter (SC-7). `pass` and `witness` are defined in SC-7. Unqualified "the cursor" means `resolved_cursor`.
- **SC-2** Coverage: every unit with `resolved_cursor < unit_key <= frontier` is either in `open_units` or resolved. Resolved units beyond `resolved_cursor` are recorded by absence from `open_units`.
- **SC-3** (never skip) `resolved_cursor` MUST NOT pass an open unit. A unit whose fetch failed retryably, or that is incomplete, becomes open and stays open until the consumer reports it resolved (Q4) or the task reaches a terminal state. Lack of progress is never read as absence of work.
- **SC-4** (frontier and open cap) `frontier` MAY advance beyond open units so that later pages are still discovered, but only while `open_units` is below `OPEN_UNITS_CAP`. At the cap `frontier` stops (fail closed; nothing is skipped).
- **SC-5** (revisit) Every execution re-evaluates open units, ordered by oldest `last_checked` first so a budget-limited run cannot starve any of them. A unit not revisited for lack of budget stays open. A changed `unit_rev` on an open unit triggers re-evaluation whether or not any attempt counter changed. When a unit resolves, `resolved_cursor` advances past it and past every following resolved unit in the same write.
- **SC-6** (retain before advance) A write that advances `resolved_cursor` or `frontier` MUST persist in the same write every retained item (Q5) for the units it passes. If that write fails, ambiguously or definitely, the durable journal keeps the earlier positions and the units are scanned again; no retained item is lost, and no unit is passed without its items.
- **SC-7** (change detection, coverage witness and completeness) A scan never decides completeness by comparing a `unit_key` with a time, and never infers that nothing changed from the absence of an observed change: an unobserved insertion leaves `change_seq` and every digest unchanged. Completeness rests on a durable witness bracketed by two reads of the source's listing epoch (Q3a).
  - *Change.* A change is any of: a unit never seen before (whatever its key order, including one that sorts before `resolved_cursor`), a changed `unit_rev` on a known unit, a chunk-digest mismatch (below), a unit becoming open, or a listing epoch that differs from the one recorded by the pass or the witness. Every change observed by any means, in any pass, revisit or check, MUST increment `change_seq` and clear `witness` in the same write that records it (SC-6), and any pass in progress is restarted from its first page.
  - *Chunk digests.* `chunk_digests` holds one digest over `(unit_key, unit_rev)` for each fixed-size chunk of resolved units in `unit_key` order; its length is bounded by `MAX_UNITS_PER_INTERVAL / CHUNK_SIZE`. A pass compares what it enumerates with the chunk digests. A mismatch (a changed `unit_rev` behind the cursor, or a unit inserted or removed behind it) is a change; it also moves `resolved_cursor` back to the start of the first mismatching chunk and reopens its units.
  - *Pass.* `pass = {change_seq_at_start, epoch_at_start, page_token, pages_done, running_digest}`. A pass MAY begin only when `coverage_capability` is not `none`, the wall clock is past `interval_end + SETTLE_SECONDS` (a time compared with a time; D-5) and `open_units` is empty. It begins by recording `epoch_at_start = read_epoch(interval)`. Each execution advances the pass within budget, bounded by `MAX_PAGES_PER_RUN`, persisting `page_token` and `running_digest` so that it is resumable. Every page's epoch MUST equal `epoch_at_start`; a different value is a change and restarts the pass. The pass enumerates the listing page by page through Q3 until a page with `exhausted` true.
  - *Witness.* The controller writes `witness = {change_seq, epoch, completed_at, unit_count, listing_digest = running_digest}` only when all hold: the pass has reached the `exhausted` page; `change_seq` equals `pass.change_seq_at_start`; `open_units` is empty; and a *closing read* `read_epoch(interval)`, taken after the `exhausted` page was read, in the same execution and immediately before the witness write, equals `epoch_at_start`. The closing read is the only check that detects a unit added after the last page was read. A mismatch is a change: no witness, `change_seq` incremented, the pass restarts. If the closing read fails, or the pass ended in an earlier execution, no witness is written and the closing read is repeated. A pass that does not reach an `exhausted` page (flag never set, listing error, budget exit) leaves no witness. An insertion after the closing read but before the witness commits is not excluded by any primitive; it is caught by the live check in the Completeness bullet.
  - *Completeness.* `scan_complete(task)` is true only when all hold, evaluated on the validated journal: `witness` is present and `witness.change_seq == change_seq`; `witness.completed_at` is past `interval_end + SETTLE_SECONDS`; `open_units` is empty; `resolved_cursor` equals `frontier`; and a *live check*, made in the evaluating execution after the journal was read, returns `read_epoch(interval)` equal to `witness.epoch`. A live-check mismatch is a change (increment `change_seq`, clear `witness`); if the write recording it cannot be confirmed, `scan_complete` is false for this execution. `scan_complete` is a point-in-time claim as of the live check; a unit added after it is a later change, handled by the consumer's own rules, which this document does not define. This document gives `scan_complete` no consequence.
  - *Fail closed.* With `coverage_capability` `none`, an unreliable or failing epoch, no `exhausted` flag from Q3, or `SETTLE_SECONDS` unset, no witness is written and `scan_complete` is false. A false value only delays the consumer and never skips work (ERR-2).
- **SC-8** (bounds) Per task: at most `OPEN_UNITS_CAP` open units, `RETAINED_BOUND` retained items, a bounded chunk list, and constant-size `change_seq`, `pass`, `witness` and cursor fields. On overflow the oldest diagnostic goes first; a cursor, an open unit, a `delivery_id`, the witness or a terminal record is never evicted. Scan pages are bounded by `PAGE_SIZE`, and a run stops scanning at `MAX_PAGES_PER_RUN` or when BUD-1 is reached, persisting progress per SC-6. More than one page of units (the reference case is over 100) is covered over several executions in the stable `unit_key` order without skipping or double evaluation.

## 4. State transitions (owned)

Every "Persisted write" is a single section-4 write of Document A with the `size_limit` of INV-B2. Escalation notice, if any, is a non-journal alert; this document defines no external POST (U-2).

| # | Transition | Trigger | Preconditions | Persisted write | External effects | Retry / recovery | Audit |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T1 | (none) -> `ingested` | Inbound event | `event_authentic` (Q1); `deadline(event)` is after now; `task_id` absent; final signed bytes <= `ADMISSION_LIMIT`; non-terminal count < `NONTERMINAL_CAP`; admissions this run < `MAX_ADMISSIONS_PER_RUN`; ingestion budget left (BUD-2); journal valid | New task with `deadline`, identity tuple, fixed interval, empty scan state. MAY be folded with T2 | None | CAS conflict: re-read and re-evaluate admission. Ambiguous PUT: ING-1. Refusal: `JournalFull`. An expired event is refused with the typed error `EventExpired` and audited | Event `ingested` |
| T1d | `ingested` duplicate | Same `task_id` seen again | Task exists | None, ever | None | Idempotent no-op | Workflow log |
| T2 | `ingested` -> `pending` | Same run after T1, or any later run reading a task still `ingested` (for example after a crash between T1 and T2) | Journal valid (A:INV-S4); `identity_valid` (Q2) true on the stored record; deadline not passed | `pending` (MAY be folded into T1) | None | A later run applies T2 under fresh validation and never re-ingests. If `identity_valid` is false the task stays `ingested`, an alert is raised, and T7 escalates it at its deadline (this document defines no other exit) | Event |
| T6 | `delivered_unconfirmed` -> `escalated` | Stored deadline passed (DL-1) | Deadline passed | `escalated` with reason `deadline` | None | Evaluated before any external read; retried every run until written | Event `deadline` |
| T7 | `ingested`, `pending` or `reserved` -> `escalated` | Stored deadline passed (DL-1) | Deadline passed. For `reserved`: `reserved_age > reserved_grace`, so the run that confirmed the T3 cannot be mid-POST. When the deadline has passed, T7 is applied to a `reserved` task directly instead of A:T4r, with `post_status=unknown` recorded | `escalated` with reason `deadline` | None | As T6. A racing T4 by a live run fails its state precondition (A:T4 recovery rules, conflict row) and writes nothing | Event `deadline` |

**C-extension of A:T3** (A:P8: only narrows when T3 fires). T3 additionally requires: (i) `pre_post_budget_ok` is computed by BUD-6; (ii) the time left to the task's stored deadline is at least the BUD-6 sum; (iii) no applicable `not_before` (BUD-5) lies in the future. Otherwise the task stays `pending` and the deadline still governs. This document supplies `reserved_grace` as a parameter (D-5) and requires it to satisfy A:P2.

This document defines no exception to A:T9. T6 and T7 satisfy A:P4. T6 is one resolution of `delivered_unconfirmed` under A:P5, issues no POST, and leaves every other resolution to the consumer.

## 5. Execution budget (BUD)

`RUN_WINDOW` is the platform limit. `R_reconcile` (time reserved for read, validation, deadline sweep and escalation writes) and `R_exit` (final write and logging margin) are parameters (D-5).
- **BUD-1** Ingestion and discovery may use at most `RUN_WINDOW - R_reconcile - R_exit` of monotonic time since the run started. Every external call has a timeout no greater than the remaining usable time (for reconciliation: the time left before `R_exit`).
- **BUD-2** Budget is checked before each task, page, retry and call. When it is exhausted the run stops admitting and scanning, persists progress (SC-6), and ends with status `budget_exhausted_resume`. It MUST NOT fail the run only because work remains.
- **BUD-3** Reconciliation of existing tasks comes before ingestion. Order: DL-1 sweep first, then tasks by nearest deadline, then open-unit revisits (SC-5), then listing passes (SC-7), then ingestion.
- **BUD-4** Retry sleeps count against the budget. A sleep that would cross the usable budget is replaced by exit-and-resume. Ingestion retries are bounded per call by `MAX_INGEST_RETRIES` and in total by BUD-1; when they are exhausted ingestion ends for this run as `ingestion_deferred`, reconciliation is unaffected (INV-F2), and the run still completes with that status shown in the job summary.
- **BUD-5** Quota-exhaustion delays follow section 6. A persisted `not_before` is honored; if the wait exceeds the usable budget the run persists `not_before` and exits. A `not_before` is a constant-size journal field per scope; the scope set (per task or global) is D-7. Only the quota-exhausted class of section 6 may persist a `not_before`.
- **BUD-6** `pre_post_budget_ok` is true only when remaining usable time is at least `2 * T_write + T_checkpoint + T_reread + T_post_timeout + T_recovery`, using the timeouts in force (A:P2, A:INV-D1).
- **BUD-7** The CAS retry limit `MAX_CAS_RETRIES`, the recovery-read limit `MAX_PUT_RECOVERY_READS` and the per-call fetch retry limit are explicit parameters (D-5). They satisfy A:P6.

## 6. Error taxonomy (reads, lookups and fetches)

Store PUT outcomes and POST outcomes are classified by Document A (ambiguous writes, conflicts, T4n) and are not restated here. Classification below uses only Q6 facts and never infers a quota state that the response does not show. Precedence: a 429, or a response whose quota facts explicitly show remaining quota 0, is quota exhausted; every other 5xx or timeout is transient, whatever hints it carries.

| Class | Examples | Treatment | Permanent? |
| --- | --- | --- | --- |
| Quota exhausted | 429; any response (including a 403 or 5xx) whose quota facts explicitly show remaining quota 0. A retry-after or reset hint on such a response states the wait | Wait until the stated time only if it fits the budget, else persist `not_before` and exit-and-resume. Backoff counter resets only after a success, never on quota refresh | No |
| Transient | 5xx, timeout, connection error, truncated body, whose quota facts show remaining quota above 0 or are absent. This holds even when the response carries a retry-after hint or a distant reset time | Bounded jittered exponential backoff inside the budget, then exit-and-resume. A retry-after hint on such a response is a transient backoff hint only: it may raise the next sleep to at most `MAX_TRANSIENT_HINT`, and only if that fits the budget; it is never persisted as `not_before`. **Never waits for a quota reset** | No |
| Auth or forbidden | 401; 403 without quota signal | The dependent step stops, no state change, alert. The DL-1 sweep is unaffected | No |
| Unavailable | 404 or 410 on a unit or artifact | Reported to the consumer as `unavailable` (Q4). The unit stays open unless the consumer resolves it by its own rule; it is never skipped by the scan | Not by this document |
| Invalid request | 422 on a read | The step stops with an alert; no task is rejected because of it | No |
| Admission | `JournalFull`, `EventExpired` | INV-F1 | n/a |
| Budget | Time or retry budget exhausted | Persist progress, exit-and-resume | No |
| Journal-level | A:P7 errors | Run ends per INV-F4 and A:T10 | n/a |

- **ERR-1** A retryable failure (any class above except where the consumer resolves the unit) MUST NOT produce a permanent outcome and MUST NOT advance `resolved_cursor` past the unit (SC-3). A permanent outcome for a unit requires a successful fetch and the consumer's decision.
- **ERR-2** Exhausting a retry or time budget is never converted into a rejection. The unit and task remain pending until the task deadline, after which T6 or T7 applies.
- **ERR-3** Failures of the journal Store itself while handling a unit follow Document A (INV-A3, INV-A5) and never reject a unit or a task.
- **ERR-4** A 5xx or timeout whose quota facts show remaining quota above 0, or no quota facts, is never treated as quota exhaustion, and no retry-after hint or reset time on it changes that class (section 6 precedence).

## 7. Fault-injection harness (applies to every FC test)

- **Seeded fake Store and anchor** as in Document A section 5, extended with size accounting of the final signed bytes and a settable `JOURNAL_MAX_BYTES`.
- **Fake clock** (wall and monotonic) with a settable run window; the harness uses `RUN_WINDOW` = 480 s as a fact of the reference platform and symbolic values for every other parameter. Harness values are test settings, not policy.
- **Fake external API:** paged unit listing with `unit_rev`, an `exhausted` flag, a `listing_epoch` per page and a callable `read_epoch` that changes on any add, remove or revision (a mode with capability `none` returns no epoch), hooks that mutate the source between any two calls, injectable status codes, and quota facts and retry-after hints as independent fields, counters per call kind, and a record of every wait requested. Unit keys are generated from the seed and may be deliberately non-time-sortable (random identifiers whose order bears no relation to creation time).
- **Fake inbound source** with re-listable events; **kill points** between any two journal writes.
- Assertions use only observable fake state. Tests are specified here; none has been run.

## 8. Fault-injection test matrix

| ID | Case | Setup and injection | Expected journal transition | Observable assertion |
| --- | --- | --- | --- | --- |
| FC-06 | Duplicate callbacks | The same event delivered 3 times concurrently | One `ingested` task, then `pending`; two no-ops | One task record; ingestion writes a task once; T1d performs no PUT (ING-1, T1d) |
| FC-10 | Prolonged Store outage | Store returns 503 for N runs spanning a deadline | No change until reachable; then T6/T7 for expired tasks | Deadlines not extended; expired tasks `escalated`, never re-posted (DL-1, A:INV-D3) |
| FC-11 | Prolonged external API outage | API 5xx for all runs, tasks `pending` and `delivered_unconfirmed` | Tasks unchanged until deadline; then T7 and T6 | Exactly 0 POSTs while T3 preconditions fail; escalation written at the deadline; no retry storm |
| FC-12 | Quota-exhausted read | 429 with retry-after longer than the budget | `not_before` persisted | Run ends `budget_exhausted_resume` quickly; next run makes no call before `not_before` (BUD-5) |
| FC-13 | Quota refresh mid-backoff | Quota refreshes during backoff after repeated failures | No journal change | Retry spacing stays non-decreasing until a success; counter not reset by refresh (section 6) |
| FC-16 | More than 100 units | 250 units, relevant unit at position 237, `MAX_PAGES_PER_RUN` small | Cursor advances across 3 executions | No unit skipped or evaluated twice with different results; journal size constant (SC-8) |
| FC-17 | Scan budget exhausted | Slow API | Progress persisted, run ends `budget_exhausted_resume` | Positions persisted; reconcile reserve intact; run not failed (BUD-1, BUD-2) |
| FC-23 | Ingestion exceeds budget | Many events, slow Store | Ingestion stops early; remaining events deferred | Existing tasks reconciled first; run succeeds; remaining events admitted by later runs (BUD-2, BUD-3) |
| FC-24 | Near-cap admission | Journal at `ADMISSION_LIMIT`, 20 non-terminal tasks | New event refused (`JournalFull`); existing tasks progress to terminal | Zero PUT for the refused admission; all 20 tasks reach a terminal state; size never above `JOURNAL_MAX_BYTES` (INV-F2) |
| FC-26 | Growth flood | Unauthenticated and over-quota events flood ingestion | Admissions capped | Unauthenticated events write nothing; `MAX_ADMISSIONS_PER_RUN` and `NONTERMINAL_CAP` hold; reads stay under the decoder cap |
| FC-31 | Admission versus mutation boundary | Journal exactly at `ADMISSION_LIMIT`; terminal transitions that grow it above that limit but not above `MUTATION_LIMIT` | Admission refused; existing transitions proceed | Final signed size never above `JOURNAL_MAX_BYTES`; the admission used the lower limit and the transitions the higher (INV-B2, INV-B3); variants at one byte below and above each limit |
| FC-32 | Existing mutation above `MUTATION_LIMIT` | An admitted task's mutation would exceed `JOURNAL_MAX_BYTES` | No change for that task | Zero PUT; alert; other tasks still progress; task not rejected (INV-B3a, INV-F4) |
| FC-33 | Same-attempt revisit | Unit U `in_progress` when scanned and later pages listed in the same run (variant: `open_units` already at the cap); U later completes with no attempt change | Execution 1: U in `open_units`, `resolved_cursor` before U, `frontier` advances (variant: frontier stops). Execution 2: U resolved, `resolved_cursor` jumps past U and every resolved unit after it in one write | `resolved_cursor` never passes U while open; change detected through `unit_rev`; at the cap frontier stops with no unit skipped (SC-3, SC-4, SC-5) |
| FC-43 | Crash between T1 and T2 | T1 committed, process killed | Next run reads `ingested`, validates, applies T2 | One task record, no duplicate; generation rises once per write (T2, ING-1); variant: `identity_valid` false leaves the task `ingested` with an alert |
| FC-44 | Stuck `ingested` at deadline | T2 cannot complete until the deadline | `escalated` through T7 | Escalated at the deadline; 0 POSTs; not stuck forever (T7) |
| FC-50 | Quota classification (G-5) | Variants: (a) a read returns 500 with remaining quota 4000 and a far reset time; (b) 500 with remaining quota 4000 and Retry-After 3600; (c) 500 with no quota facts and Retry-After 3600; (d) 429 with Retry-After longer than the budget; (e) 500 with remaining quota 0; (f) 429 with remaining quota 4000 | (a)-(c): no journal change. (d), (e), (f): `not_before` persisted | (a)-(c): the recorded wait follows ordinary backoff, with the hint capped at `MAX_TRANSIENT_HINT`; no wait toward the reset time or 3600 s; no `not_before` persisted. (d)-(f): quota class, the stated wait honored or `not_before` persisted, run ends `budget_exhausted_resume` (ERR-4, section 6 precedence, BUD-5) |
| FC-51 | Retryable failure cannot bypass the deadline (G-7) | Every evidence or unit fetch fails retryably; deadline passes mid-run | `escalated` through T6/T7 in that run | The sweep runs before any fetch; escalation written even though every fetch failed (DL-1, ERR-2) |
| FC-52 | Ingestion lookup failure (G-8) | The inbound listing fails transiently at run start with 3 existing tasks needing work | Existing tasks reconciled | Reconciliation completes; ingestion ends `ingestion_deferred`; run succeeds (INV-F2, INV-F4) |
| FC-53 | Bounded ingestion retries (G-9) | Ingestion calls fail until retries exhaust | No journal change from ingestion | Total retries and sleeps stay within `MAX_INGEST_RETRIES` and BUD-1; reconcile time reserve untouched; no permanent rejection (BUD-4, ERR-2) |
| FC-54 | Rerun behind the cursor | Unit behind `resolved_cursor` changes `unit_rev` after resolution, after a witness was written | `change_seq` incremented and `witness` cleared in the write that records it; `resolved_cursor` moves back to the mismatching chunk | The unit is re-evaluated; `scan_complete` is false until a new full pass finishes with no further change (SC-7) |
| FC-55 | Retained items survive a cap exit | Cap or budget exit mid-scan; variant: the final write fails | Positions and retained items persist together; failed write leaves earlier positions | No unit passed without its retained items; after restart nothing is lost or double counted (SC-6) |
| FC-56 | `reserved` at deadline | `reserved` task past its deadline; variants: age below and above `reserved_grace` | Below grace: no T7. Above: T7 with `post_status=unknown` | 0 POSTs; a racing live T4 writes nothing (T7, A:T4 recovery rules) |
| FC-57 | Open-unit cap | `open_units` reaches `OPEN_UNITS_CAP` | Frontier stops; open units revisited oldest first | No unit dropped or skipped; budget-limited runs revisit every open unit in turn (SC-4, SC-5) |
| FC-58 | Parameter sweep (G-10) | Boundary tests FC-24, FC-31 and FC-32 repeated over at least three parameter sets, including `JOURNAL_RESERVE_BYTES` exactly equal to `NONTERMINAL_CAP * TASK_GROWTH_BOUND` and one byte below | At the bound: normal. Below: all admissions refused, existing tasks still processed | No numeric value is pinned by the spec; the invalid configuration fails closed (INV-B4) |
| FC-59 | Opaque keys, late insertions and the epoch bracket | Unit keys are random identifiers with no time order; capability `epoch` unless stated. Base: a pass records epoch E1, reads 3 pages each carrying E1, reaches the `exhausted` page after `interval_end + SETTLE_SECONDS`, the closing read returns E1, the witness is written, and the live check returns E1 so `scan_complete` is true. Then a unit whose key sorts before `resolved_cursor` is inserted at the source (epoch E2). Variants: (a) inserted mid-pass; (b) inserted by a hook after the `exhausted` page was returned and before the closing read; (b2) inserted after the closing read and before the witness commit; (b3) capability `none`, unit inserted after the `exhausted` page; (b4) the closing read fails with a 5xx; (c) the source never sets `exhausted`; (d) `SETTLE_SECONDS` unset; (e) an empty listing with `exhausted` true on the first page; (f) capability `snapshot`, unit inserted after the pin | Base: the next live check returns E2, so `change_seq` is incremented and `witness` cleared in one write; a new pass records E2; the unit is evaluated; a clean pass writes a new witness. (a) A page carries E2, the pass restarts, no witness. (b) The closing read returns E2, no witness, `change_seq` incremented, pass restarts. (b2) The witness is written for E1; the live check in the same or next execution returns E2, so the witness is cleared. (b3) No witness is ever written. (b4) No witness; the closing read is retried in a later execution. (c), (d) No witness is ever written. (e) A witness with `unit_count` 0 after the settle time. (f) The pass reads the pinned snapshot, the closing read returns the live epoch, which differs, so no witness | No comparison between a key and a time occurs anywhere (the harness fails if keys are given a time order). In (b) and (b2) the harness asserts the race is caught by the epoch reads, not by `change_seq` or a digest, which stay unchanged until the epoch read is made. In (b3) no journal field changes at all and the test asserts that `scan_complete` stays false across many executions, so correctness comes from fail-closed behaviour and not from detection. `scan_complete` is never true on a witness whose `change_seq` or epoch differs from the current one. In (c), (d), (b3) and (b4) it stays false without any rejection or skipped unit. In (e) it is true only after `interval_end + SETTLE_SECONDS` (SC-7, Q3a, ERR-2) |

## 9. Unresolved owner decisions and risks

- **D-3** Values of `JOURNAL_MAX_BYTES`, `JOURNAL_RESERVE_BYTES`, `NONTERMINAL_CAP`, `TASK_GROWTH_BOUND` and `MAX_ADMISSIONS_PER_RUN`. G-10 remains open until they are set. No value is invented here.
- **D-4** Archival or retention for terminal tasks (INV-B5). No deletion is authorized.
- **D-5** Budget and scan parameters: `R_reconcile`, `R_exit`, `reserved_grace`, retry limits (BUD-7), `MAX_TRANSIENT_HINT` (section 6), `PAGE_SIZE`, `MAX_PAGES_PER_RUN`, `OPEN_UNITS_CAP`, `RETAINED_BOUND`, `CHUNK_SIZE`, `MAX_UNITS_PER_INTERVAL`, `SETTLE_SECONDS` (SC-7), and the task deadline duration behind Q1.
- **D-7** Scope of `not_before`: per task or global (BUD-5). Both conform to this document.
- **U-1** Wall-clock source and skew for stored deadlines (DL-1) and for `SETTLE_SECONDS` (SC-7). A skewed runner clock could escalate or complete early or late. No mitigation is specified.
- **U-2** Whether an escalation notice may be a POST. If so, it needs its own reservation under Document A:T3 and is out of scope here.
- **U-3** Whether persistent `ingestion_deferred` or `budget_exhausted_resume` should eventually fail the run for visibility.
- **U-4** Whether an artifact or unit that is permanently `unavailable` may ever be closed. This document keeps it open until the task deadline; closing it is a consumer decision (Q4).
- **U-5** Inbound events that are consume-once at the source would violate Q7 and risk loss at an ambiguous T1.
- **U-6** Whether a source can provide the Q3a `epoch` or `snapshot` capability and set the `exhausted` flag reliably, and bound how late a unit may appear after `interval_end`. If it cannot (`none`), `scan_complete` stays false and the consumer relies on the task deadline. This is a consumer property, not defined here. Even with the capability, `scan_complete` is only a point-in-time claim (SC-7).
- Risk: the pass, the epoch reads and the digest comparison (SC-7) cost listing calls on every execution until a witness exists; their bounds are parameters (D-5). The seam with the consumer is Q3/Q3a/Q4 and `scan_complete`.
- G-6 (journal storage failures) is Document A's; ERR-3 only states that C adds no rejection for them. This document selects no credential, secret, token, ref protection or permission.

## 10. Ownership and relocation ledger

Each ID has exactly one authoritative home: this document. IDs not listed belong to other documents.

| Old ID (parked #228 text) | Home here | Change |
| --- | --- | --- |
| INV-B2, INV-B3, INV-B3a | same | Limits expressed as the caller-supplied `size_limit` of A:INV-B1 |
| INV-B4 | same | Adds `NONTERMINAL_CAP` and the fail-closed configuration check |
| INV-B5 | same | Unchanged meaning |
| INV-F1..F4 | same | F4 names the journal-level errors that end a run |
| (new) DL-1, ING-1 | here | Added to close deadline bypass and ingestion idempotence |
| T1, T1d, T2 | same | T1d never writes; T2 invalid identity stays `ingested` |
| T6, T7 | same | Evaluated by DL-1; T7 gains the `reserved_grace` rule |
| T3 (extension only) | A:T3 plus C-extension | Adds BUD-6 and `not_before` preconditions |
| BUD-1..BUD-6 | same | BUD-7 added for retry limits (old 6.4) |
| ERR-1, ERR-2 | ERR-1, ERR-2 | Generalized from artifacts to units |
| error taxonomy | section 6 | Quota class only for 429 or explicit remaining quota 0; a 5xx retry-after hint is a capped transient hint (ERR-4) |
| EV-1..EV-4 | SC-8, SC-1..SC-3 | Cursor split into `resolved_cursor` and `frontier` |
| EV-13 (mechanism) | SC-3, SC-4, SC-5 | Evidence meaning is not defined here |
| EV-12 (scan bounds) | SC-8 | Retained-item bound is a parameter (Q5) |
| scan completeness | SC-7, Q3a | Key-to-time comparison removed; durable coverage witness bracketed by opening and closing listing-epoch reads (Q3a capability, fail closed when absent), live check at evaluation, `change_seq` invalidation, `exhausted` flag and `SETTLE_SECONDS` |
| D-3, D-4, D-5, D-7 | section 9 | Open owner decisions; D-5 gains `SETTLE_SECONDS` and `MAX_TRANSIENT_HINT` |
| G-2, G-5, G-7, G-8, G-9, G-10 | SC-6, ERR-4, DL-1, INV-F4, BUD-4, INV-B4 | Review pending; G-10 open |
| FI-06, 10, 11, 12, 13, 16, 17, 23, 24, 26, 31, 32, 33, 43, 44 | FC-same number | FC-33 rewritten for the two scan positions |
| new tests | FC-50..FC-59 | G-5, G-7, G-8, G-9, rerun, retain, `reserved` deadline, cap, parameter sweep, opaque-key and epoch-bracketed witness |

## 11. Mapping of findings to this document

| Thread | Finding | Rules and sections | Tests |
| --- | --- | --- | --- |
| #228 `PRRT_kwDOUdC8Ds6yBbwW` | Admission reserve versus existing-task limit | INV-B2, B3, B3a, B4 | FC-24, FC-31, FC-32, FC-58 |
| #228 `PRRT_kwDOUdC8Ds6yBbw5` | Same-attempt revisit | SC-3, SC-4, SC-5 | FC-33, FC-57 |
| #228 `PRRT_kwDOUdC8Ds6yGzNV` | Crash between T1 and T2, escalation of `ingested` | T2, T7, ING-1 | FC-43, FC-44 |
| #228 `PRRT_kwDOUdC8Ds6yHAKC` | Cursor consistency with open units | SC-1..SC-5 (only `frontier` passes an open unit) | FC-33 |
| #219 `PRRT_kwDOUdC8Ds6xmLxk` | Capped scans lose cursor and retained evidence | SC-6, SC-8 | FC-16, FC-17, FC-55 |
| #219 `PRRT_kwDOUdC8Ds6xtjVE` | Journal outgrows the decoder cap | INV-B2..B5 | FC-24, FC-26, FC-31, FC-32 |
| #219 `PRRT_kwDOUdC8Ds6xtjVH` | Quota-reset delay applied to HTTP 500 | Section 6, ERR-4, BUD-5 | FC-12, FC-13, FC-50 |
| #219 `PRRT_kwDOUdC8Ds6x0PNt` | Transient failure bypasses the deadline | DL-1, T6, T7, ERR-2 | FC-10, FC-44, FC-51 |
| #219 `PRRT_kwDOUdC8Ds6x0phT` | Transient lookup aborts existing-task work | INV-F2, INV-F4, BUD-3 | FC-52 |
| #219 `PRRT_kwDOUdC8Ds6x7kSO` | Ingestion retries exhaust the budget | BUD-1, BUD-2, BUD-4 | FC-23, FC-53 |
| #219 `PRRT_kwDOUdC8Ds6x7kSU` | `JournalFull` aborts existing-task work | INV-F1, INV-F2, INV-B3 | FC-24, FC-31 |
| #219 `PRRT_kwDOUdC8Ds6xrV8T` (cursor part only) | Interval misses rerun and status changes | SC-5, SC-7, Q3a | FC-33, FC-54, FC-59 |

All 12 threads on #219 and all 12 on #228 stay open and unresolved on their original PRs; this document resolves none of them and asserts no acceptance. Evidence interpretation and all other primary clauses of these threads belong to a later document.

## 12. Acceptance criteria and non-goals

Acceptable for the next step only when all hold: (1) exact-head required CI is green; (2) independent Codex review has accepted the exact head, plus an owner-run OpenCode or Jules review (DX20); (3) every C-owned thread in section 11 is dispositioned with evidence; (4) every FC row names a rule of this document or of Document A and every cited ID exists there; (5) D-3, D-4, D-5 and D-7 are answered by the owner in writing; (6) production execution, activation and any POST-issuing mode remain separately owner-authorized.

This document changes no workflow, script, application code, secret, token or permission, and does not authorize merging PR #219, activating the controller, or any production action. Migration 0057 production execution remains prohibited.
